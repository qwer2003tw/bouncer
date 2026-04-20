"""
Bouncer Help Command
使用 botocore 提供 AWS CLI 命令說明
"""
import re
from typing import Optional

# ---------------------------------------------------------------------------
# Bouncer Built-in Commands
# ---------------------------------------------------------------------------

# 內建 Bouncer 命令說明（不依賴 botocore）
_BOUNCER_BUILTIN_HELP: dict[str, dict] = {
    "batch-deploy": {
        "name": "batch-deploy",
        "description": (
            "批次部署完整流程：透過 presigned_batch → confirm_upload → "
            "trust session → grant session，以最小審批次數完成多檔案上傳與部署。"
        ),
        "steps": [
            "1. presigned_batch  — 取得多個 S3 presigned URLs",
            "2. confirm_upload   — 確認上傳完成，建立 DynamoDB 記錄",
            "3. bouncer_trust    — 開啟信任時段（減少逐一審批）",
            "4. bouncer_execute (grant) — 在信任/grant 下執行部署命令",
        ],
        "example": (
            "# Step 1: 取得 presigned URLs\n"
            "mcporter call bouncer bouncer_presigned_batch \\\n"
            "  files='[{\"filename\":\"app.zip\",\"content_type\":\"application/zip\"}]' \\\n"
            "  reason='部署 app' source='Bot'\n\n"
            "# Step 2: 上傳後確認\n"
            "mcporter call bouncer bouncer_confirm_upload \\\n"
            "  batch_id='<batch_id>' source='Bot'\n\n"
            "# Step 3: 申請 grant session\n"
            "mcporter call bouncer bouncer_request_grant \\\n"
            "  commands='[\"aws s3 cp ...\",\"aws lambda update-function-code ...\"]' \\\n"
            "  reason='部署 app' source='Bot' account_id='123456789012'\n\n"
            "# Step 4: 在 grant 下執行命令\n"
            "mcporter call bouncer bouncer_grant_execute \\\n"
            "  grant_id='<grant_id>' \\\n"
            "  command='aws lambda update-function-code --function-name MyFunc --zip-file fileb://app.zip'"
        ),
        "see_also": ["bouncer_presigned_batch", "bouncer_confirm_upload",
                     "bouncer_request_grant", "bouncer_grant_execute"],
    },
}


def get_bouncer_command_help(command: str) -> dict | None:
    """Return built-in Bouncer command help, or None if not found."""
    key = command.strip().lower().lstrip("/")
    # Support both "batch-deploy" and "bouncer batch-deploy"
    if key.startswith("bouncer "):
        key = key[len("bouncer "):]
    return _BOUNCER_BUILTIN_HELP.get(key)


def format_bouncer_help_text(help_data: dict) -> str:
    """Format a built-in Bouncer command help entry as readable text."""
    lines = [
        f"📖 /bouncer help {help_data['name']}",
        "",
        help_data.get("description", ""),
        "",
        "流程步驟:",
    ]
    for step in help_data.get("steps", []):
        lines.append(f"  {step}")

    example = help_data.get("example", "")
    if example:
        lines.append("")
        lines.append("範例:")
        lines.append(example)

    see_also = help_data.get("see_also", [])
    if see_also:
        lines.append("")
        lines.append(f"相關工具: {', '.join(see_also)}")

    return '\n'.join(lines)



def get_command_help(command: str) -> dict:
    """
    取得 AWS CLI 命令的參數說明

    Args:
        command: AWS CLI 命令（例如：ec2 modify-instance-attribute）

    Returns:
        包含參數說明的 dict
    """
    try:
        import botocore.session
    except ImportError:
        return {'error': 'botocore not available'}

    # 解析命令
    parts = command.strip().split()

    # 移除 'aws' 前綴
    if parts and parts[0] == 'aws':
        parts = parts[1:]

    if len(parts) < 2:
        return {'error': f'無效命令格式，需要: aws <service> <action>，收到: {command}'}

    service_name = parts[0].lower()
    action_raw = parts[1]

    # 轉換 CLI action 格式為 API 格式
    # 例如: modify-instance-attribute -> ModifyInstanceAttribute
    action_name = ''.join(word.capitalize() for word in action_raw.split('-'))

    try:
        session = botocore.session.get_session()
        service_model = session.get_service_model(service_name)
    except Exception as e:  # noqa: BLE001
        return {'error': f'找不到服務: {service_name}', 'detail': str(e)}

    # 尋找操作
    try:
        operation_model = service_model.operation_model(action_name)
    except Exception:  # noqa: BLE001
        # 列出可用的操作
        available_ops = list(service_model.operation_names)

        # 找相似的
        similar = find_similar_operations(action_name, available_ops)

        return {
            'error': f'找不到操作: {action_name}',
            'service': service_name,
            'similar_operations': similar[:5],
            'hint': f'試試: aws {service_name} help 查看所有操作'
        }

    # 建立參數說明
    result = {
        'service': service_name,
        'operation': action_raw,
        'api_name': action_name,
        'description': operation_model.documentation or 'No description',
        'parameters': {},
        'required': [],
    }

    # 解析輸入參數
    input_shape = operation_model.input_shape
    if input_shape:
        for param_name, param_shape in input_shape.members.items():
            cli_name = camel_to_kebab(param_name)
            param_info = {
                'cli_name': f'--{cli_name}',
                'type': get_type_name(param_shape),
                'description': clean_description(param_shape.documentation),
            }

            # 檢查是否必填
            if hasattr(input_shape, 'required_members') and param_name in input_shape.required_members:
                result['required'].append(cli_name)
                param_info['required'] = True

            result['parameters'][cli_name] = param_info

    return result


def get_service_operations(service_name: str) -> dict:
    service_name = service_name.lower().strip()
    """
    列出服務的所有操作
    """
    try:
        import botocore.session
        session = botocore.session.get_session()
        service_model = session.get_service_model(service_name)

        operations = []
        for op_name in sorted(service_model.operation_names):
            cli_name = camel_to_kebab(op_name)
            operations.append(cli_name)

        return {
            'service': service_name,
            'operation_count': len(operations),
            'operations': operations
        }
    except Exception as e:  # noqa: BLE001
        return {'error': f'找不到服務: {service_name}', 'detail': str(e)}


def find_similar_operations(target: str, operations: list) -> list:
    """找相似的操作名稱"""
    target_lower = target.lower()
    scored = []

    for op in operations:
        op_lower = op.lower()
        # 簡單的相似度計算
        score = 0

        # 包含關鍵字
        target_words = set(re.findall(r'[A-Z][a-z]+', target))
        op_words = set(re.findall(r'[A-Z][a-z]+', op))
        common = target_words & op_words
        score += len(common) * 10

        # 開頭相同
        if op_lower.startswith(target_lower[:3]):
            score += 5

        if score > 0:
            scored.append((score, camel_to_kebab(op)))

    scored.sort(reverse=True)
    return [op for _, op in scored]


def camel_to_kebab(name: str) -> str:
    """CamelCase -> kebab-case"""
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1-\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1-\2', s1).lower()


def get_type_name(shape) -> str:
    """取得參數類型名稱"""
    type_name = shape.type_name

    if type_name == 'structure':
        return 'JSON object'
    elif type_name == 'list':
        member = getattr(shape, 'member', None)
        if member:
            return f'list of {get_type_name(member)}'
        return 'list'
    elif type_name == 'map':
        return 'JSON map'
    elif type_name == 'boolean':
        return 'boolean'
    elif type_name == 'integer':
        return 'integer'
    elif type_name == 'timestamp':
        return 'timestamp'
    else:
        return type_name


def clean_description(doc: Optional[str]) -> str:
    """清理文檔字串"""
    if not doc:
        return ''

    # 移除 HTML tags
    doc = re.sub(r'<[^>]+>', '', doc)
    # 移除多餘空白
    doc = ' '.join(doc.split())
    # 截斷過長的描述
    if len(doc) > 200:
        doc = doc[:197] + '...'

    return doc


def _format_error_help(help_data: dict) -> str:
    """Format error message with similar operations and hints."""
    lines = [f"❌ {help_data['error']}"]
    if 'similar_operations' in help_data:
        lines.append("\n類似的操作:")
        for op in help_data['similar_operations'][:5]:
            lines.append(f"  • aws {help_data.get('service', '?')} {op}")
    if 'hint' in help_data:
        lines.append(f"\n💡 {help_data['hint']}")
    return '\n'.join(lines)


def format_help_text(help_data: dict) -> str:
    """格式化為可讀文字"""
    if 'error' in help_data:
        return _format_error_help(help_data)

    lines = [
        f"📖 aws {help_data['service']} {help_data['operation']}",
        "",
        clean_description(help_data.get('description', ''))[:300],
        "",
        "參數:",
    ]

    # 先顯示必填參數
    for name in help_data.get('required', []):
        if name in help_data['parameters']:
            param = help_data['parameters'][name]
            lines.append(f"  --{name} (必填)")
            lines.append(f"      類型: {param['type']}")
            if param.get('description'):
                lines.append(f"      {param['description'][:100]}")

    # 再顯示選填參數
    for name, param in help_data['parameters'].items():
        if name not in help_data.get('required', []):
            lines.append(f"  --{name}")
            lines.append(f"      類型: {param['type']}")
            if param.get('description'):
                lines.append(f"      {param['description'][:100]}")

    return '\n'.join(lines)
