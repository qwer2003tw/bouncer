"""
Bouncer Deployer Notifier Lambda
發送部署通知到 Telegram
"""
import json
import os
import re
import time
import urllib.request
import urllib.parse
import boto3

# 環境變數
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
HISTORY_TABLE = os.environ.get('HISTORY_TABLE', 'bouncer-deploy-history')
LOCKS_TABLE = os.environ.get('LOCKS_TABLE', 'bouncer-deploy-locks')

# DynamoDB
dynamodb = boto3.resource('dynamodb')
history_table = dynamodb.Table(HISTORY_TABLE)
locks_table = dynamodb.Table(LOCKS_TABLE)

# ---------------------------------------------------------------------------
# DeployErrorExtractor — clean encapsulation of deploy failure diagnosis
# ---------------------------------------------------------------------------

# CodeBuild log lines that indicate the root cause (ordered by signal strength)
_ERROR_LINE_PATTERNS = [
    re.compile(r"^Error:\s+.+", re.IGNORECASE),
    re.compile(r"^\s*\[ERROR\]\s+.+", re.IGNORECASE),
    re.compile(r"An error occurred .+", re.IGNORECASE),
    re.compile(r"ROLLBACK_COMPLETE|UPDATE_ROLLBACK_COMPLETE", re.IGNORECASE),
    re.compile(r"ResourceStatus.*FAILED", re.IGNORECASE),
    re.compile(r"BUILD_FAILED", re.IGNORECASE),
]

# Noisy lines we want to skip even if they match the above
_NOISE_RE = re.compile(
    r"^\s*(#|\$|echo|export|cd\s|pip\s|npm\s|node_modules)",
    re.IGNORECASE,
)


class DeployErrorExtractor:
    """Extract and format actionable error lines from deploy failure data.

    Usage::

        extractor = DeployErrorExtractor(error_payload, max_lines=5)
        summary = extractor.summary()         # str — human-readable
        lines   = extractor.error_lines()     # list[str] — raw extracted lines
        record  = extractor.to_ddb_record()   # dict — ready for DynamoDB update
    """

    def __init__(self, error, *, max_lines: int = 5, max_chars: int = 800) -> None:
        self._error = error
        self._max_lines = max_lines
        self._max_chars = max_chars
        self._parsed: str | None = None  # cached full message

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """Return a human-readable error summary (truncated to max_chars)."""
        msg = self._full_message()
        if len(msg) > self._max_chars:
            msg = msg[: self._max_chars] + "…"
        return msg

    def error_lines(self) -> list:
        """Return the extracted signal-rich error lines (up to max_lines)."""
        raw = self._full_message()
        lines = raw.splitlines()
        extracted = []
        for line in lines:
            if _NOISE_RE.search(line):
                continue
            for pattern in _ERROR_LINE_PATTERNS:
                if pattern.search(line):
                    clean = line.strip()
                    if clean and clean not in extracted:
                        extracted.append(clean)
                    break
            if len(extracted) >= self._max_lines:
                break

        # Fallback: return first non-empty lines if nothing matched
        if not extracted:
            extracted = [line.strip() for line in lines if line.strip()][: self._max_lines]

        return extracted

    def to_ddb_record(self) -> dict:
        """Return a dict suitable for DynamoDB update_item ExpressionAttributeValues."""
        return {
            "error_message": self.summary()[:1000],
            "error_lines": self.error_lines(),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _full_message(self) -> str:
        if self._parsed is not None:
            return self._parsed
        self._parsed = self._parse(self._error)
        return self._parsed

    @staticmethod
    def _parse(error) -> str:
        """Parse the raw Step Functions / CodeBuild error payload into a string."""
        if not error:
            return "Unknown error"

        if isinstance(error, str):
            return error

        if not isinstance(error, dict):
            return str(error)[:500]

        cause = error.get("Cause", "")
        error_type = error.get("Error", "")

        if cause:
            try:
                cause_obj = json.loads(cause) if isinstance(cause, str) else cause
                if isinstance(cause_obj, dict):
                    build = cause_obj.get("Build", {})
                    if build:
                        phases = build.get("Phases", [])
                        for phase in phases:
                            if phase.get("PhaseStatus") == "FAILED":
                                phase_type = phase.get("PhaseType", "")
                                contexts = phase.get("Contexts", [])
                                if contexts:
                                    msg = contexts[0].get("Message", "")
                                    return f"[{phase_type}] {msg}"
                        build_status = build.get("BuildStatus", "")
                        return f"Build {build_status}"
                    return str(cause_obj)[:500]
            except (json.JSONDecodeError, TypeError):
                pass
            return cause[:500] if len(cause) > 500 else cause

        if error_type:
            return f"Error: {error_type}"

        return str(error)[:500]


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def lambda_handler(event, context):
    """處理通知請求"""
    action = event.get('action', '')

    if action == 'start':
        return handle_start(event)
    elif action == 'progress':
        return handle_progress(event)
    elif action == 'success':
        return handle_success(event)
    elif action == 'failure':
        return handle_failure(event)
    else:
        return {'error': f'Unknown action: {action}'}


def handle_start(event):
    """部署開始通知"""
    deploy_id = event.get('deploy_id', '')
    project_id = event.get('project_id', '')
    branch = event.get('branch', 'master')

    text = (
        f"⏳ *部署開始*\n\n"
        f"📦 *專案：* {project_id}\n"
        f"🌿 *分支：* {branch}\n"
        f"🆔 *ID：* `{deploy_id}`\n\n"
        f"📊 *進度：*\n"
        f"├── 🔄 初始化中...\n"
        f"├── ⏳ Template 掃描\n"
        f"├── ⏳ sam build\n"
        f"└── ⏳ sam deploy"
    )

    message_id = send_telegram_message(text)

    # 更新歷史記錄
    update_history(deploy_id, {
        'status': 'RUNNING',
        'telegram_message_id': message_id,
        'phase': 'INITIALIZING'
    })

    return {'message_id': message_id}


def handle_progress(event):
    """部署進度更新"""
    deploy_id = event.get('deploy_id', '')
    project_id = event.get('project_id', '')
    branch = event.get('branch', 'master')
    phase = event.get('phase', '')
    elapsed_seconds = event.get('elapsed_seconds', 0)

    # 取得之前的 message_id
    history = get_history(deploy_id)
    message_id = history.get('telegram_message_id') if history else None

    # 根據 phase 建立進度顯示
    phases = {
        'INITIALIZING': ('🔄', '⏳', '⏳', '⏳'),
        'SCANNING': ('✅', '🔄', '⏳', '⏳'),
        'BUILDING': ('✅', '✅', '🔄', '⏳'),
        'DEPLOYING': ('✅', '✅', '✅', '🔄'),
    }

    icons = phases.get(phase, ('⏳', '⏳', '⏳', '⏳'))

    text = (
        f"⏳ *部署進行中*\n\n"
        f"📦 *專案：* {project_id}\n"
        f"🌿 *分支：* {branch}\n"
        f"🆔 *ID：* `{deploy_id}`\n\n"
        f"📊 *進度：*\n"
        f"├── {icons[0]} 初始化\n"
        f"├── {icons[1]} Template 掃描\n"
        f"├── {icons[2]} sam build\n"
        f"└── {icons[3]} sam deploy\n\n"
        f"⏱️ *已執行：* {format_duration(elapsed_seconds)}"
    )

    if message_id:
        update_telegram_message(message_id, text)
    else:
        message_id = send_telegram_message(text)

    # 更新歷史
    update_history(deploy_id, {
        'phase': phase,
        'telegram_message_id': message_id
    })

    return {'message_id': message_id}


def handle_success(event):
    """部署成功通知"""
    deploy_id = event.get('deploy_id', '')
    project_id = event.get('project_id', '')
    build_id = event.get('build_id', '')

    # 取得歷史記錄
    history = get_history(deploy_id)
    message_id = history.get('telegram_message_id') if history else None
    started_at = history.get('started_at', 0) if history else 0
    branch = history.get('branch', 'master') if history else 'master'

    # 計算時間
    duration = int(time.time()) - int(started_at) if started_at else 0

    text = (
        f"✅ *部署成功！*\n\n"
        f"📦 *專案：* {project_id}\n"
        f"🌿 *分支：* {branch}\n"
        f"🆔 *ID：* `{deploy_id}`\n\n"
        f"📊 *進度：*\n"
        f"├── ✅ 初始化\n"
        f"├── ✅ Template 掃描\n"
        f"├── ✅ sam build\n"
        f"└── ✅ sam deploy\n\n"
        f"⏱️ *總時間：* {format_duration(duration)}"
    )

    if message_id:
        update_telegram_message(message_id, text)
    else:
        send_telegram_message(text)

    # 更新歷史
    update_history(deploy_id, {
        'status': 'SUCCESS',
        'finished_at': int(time.time()),
        'duration_seconds': duration,
        'build_id': build_id
    })

    # 釋放部署鎖
    release_lock(project_id)

    return {'status': 'success'}


def handle_failure(event):
    """部署失敗通知"""
    deploy_id = event.get('deploy_id', '')
    project_id = event.get('project_id', '')
    error = event.get('error', {})

    # 取得歷史記錄
    history = get_history(deploy_id)
    message_id = history.get('telegram_message_id') if history else None
    started_at = history.get('started_at', 0) if history else 0
    branch = history.get('branch', 'master') if history else 'master'
    phase = history.get('phase', 'UNKNOWN') if history else 'UNKNOWN'

    # 計算時間
    duration = int(time.time()) - int(started_at) if started_at else 0

    # 使用 DeployErrorExtractor 解析錯誤
    extractor = DeployErrorExtractor(error, max_lines=5, max_chars=500)
    error_summary = extractor.summary()
    error_lines = extractor.error_lines()

    # 組裝 Telegram 訊息：若有 error_lines 就顯示條列清單
    if error_lines:
        lines_block = "\n".join(f"  • {line}" for line in error_lines)
        error_block = f"📋 *關鍵錯誤行：*\n```\n{lines_block}\n```\n\n"
    else:
        error_block = f"📄 *錯誤：*\n```\n{error_summary}\n```\n\n"

    text = (
        f"❌ *部署失敗*\n\n"
        f"📦 *專案：* {project_id}\n"
        f"🌿 *分支：* {branch}\n"
        f"🆔 *ID：* `{deploy_id}`\n\n"
        f"❗ *失敗階段：* {phase}\n"
        f"{error_block}"
        f"⏱️ *執行時間：* {format_duration(duration)}"
    )

    if message_id:
        update_telegram_message(message_id, text)
    else:
        send_telegram_message(text)

    # 更新歷史（包含 error_lines）
    ddb_record = extractor.to_ddb_record()
    update_history(deploy_id, {
        'status': 'FAILED',
        'finished_at': int(time.time()),
        'duration_seconds': duration,
        'error_message': ddb_record['error_message'],
        'error_lines': ddb_record['error_lines'],
        'error_phase': phase,
    })

    # 釋放部署鎖
    release_lock(project_id)

    return {'status': 'failed'}


def send_telegram_message(text: str) -> int:
    """發送 Telegram 訊息，返回 message_id"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured")
        return 0

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': 'Markdown'
    }

    try:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(data).encode(),
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            return result.get('result', {}).get('message_id', 0)
    except Exception as e:
        print(f"Telegram send error: {e}")
        return 0


def update_telegram_message(message_id: int, text: str):
    """更新 Telegram 訊息"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not message_id:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    data = {
        'chat_id': TELEGRAM_CHAT_ID,
        'message_id': message_id,
        'text': text,
        'parse_mode': 'Markdown'
    }

    try:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(data).encode(),
            method='POST'
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Telegram update error: {e}")


def get_history(deploy_id: str) -> dict:
    """取得部署歷史"""
    try:
        result = history_table.get_item(Key={'deploy_id': deploy_id})
        return result.get('Item', {})
    except Exception as e:
        print(f"Error getting history: {e}")
        return {}


def update_history(deploy_id: str, updates: dict):
    """更新部署歷史"""
    try:
        update_expr = 'SET ' + ', '.join(f'#{k} = :{k}' for k in updates.keys())
        expr_names = {f'#{k}': k for k in updates.keys()}
        expr_values = {f':{k}': v for k, v in updates.items()}

        history_table.update_item(
            Key={'deploy_id': deploy_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values
        )
    except Exception as e:
        print(f"Error updating history: {e}")


def release_lock(project_id: str):
    """釋放部署鎖"""
    if not project_id:
        return
    try:
        locks_table.delete_item(Key={'project_id': project_id})
        print(f"Released lock for {project_id}")
    except Exception as e:
        print(f"Error releasing lock for {project_id}: {e}")


def format_duration(seconds: int) -> str:
    """格式化時間"""
    if seconds < 60:
        return f"{seconds} 秒"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes} 分 {secs} 秒"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours} 小時 {minutes} 分"


def extract_error_message(error) -> str:
    """從 Step Functions 錯誤中提取可讀訊息"""
    if not error:
        return 'Unknown error'

    # 如果是字串，直接返回
    if isinstance(error, str):
        return error

    # 如果是 dict，嘗試解析
    if isinstance(error, dict):
        # Step Functions 錯誤結構
        cause = error.get('Cause', '')
        error_type = error.get('Error', '')

        # 嘗試解析 Cause（可能是 JSON 字串）
        if cause:
            try:
                cause_obj = json.loads(cause) if isinstance(cause, str) else cause

                # CodeBuild 錯誤
                if isinstance(cause_obj, dict):
                    build = cause_obj.get('Build', {})
                    if build:
                        status = build.get('BuildStatus', '')
                        phases = build.get('Phases', [])

                        # 找到失敗的 phase
                        for phase in phases:
                            if phase.get('PhaseStatus') == 'FAILED':
                                phase_type = phase.get('PhaseType', '')
                                contexts = phase.get('Contexts', [])
                                if contexts:
                                    msg = contexts[0].get('Message', '')
                                    return f"[{phase_type}] {msg}"

                        return f"Build {status}"

                    # 其他錯誤
                    return str(cause_obj)[:500]
            except (json.JSONDecodeError, TypeError):
                pass

            # 無法解析，返回原始 cause（截斷）
            return cause[:500] if len(cause) > 500 else cause

        # 沒有 Cause，返回 Error type
        if error_type:
            return f"Error: {error_type}"

    # 兜底
    return str(error)[:500]
