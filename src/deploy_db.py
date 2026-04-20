"""
Bouncer Deploy Database Operations
DB/lock/project CRUD operations extracted from deployer.py
"""
import subprocess
import sys
import time
import boto3
from botocore.exceptions import ClientError
import db as _db
from aws_lambda_powertools import Logger
from constants import DEFAULT_REGION, TTL_30_DAYS

logger = Logger(service="bouncer")

# DynamoDB — lazy init (no boto3 call at import time)
# Tests may set these directly: deploy_db._dynamodb = moto_resource
# Legacy tests may also set `deployer._dynamodb = X` etc. — we check deployer
# module first (backward-compat) before falling back to our own lazy init.
_dynamodb = None
projects_table = None
history_table = None
locks_table = None


def _check_deployer_override(attr_name: str):
    """Check if deployer module has a non-None override for this attribute.

    Backward-compat: legacy tests set `deployer.history_table = X`.
    After #355 the canonical location is `deploy_db.history_table`, but tests
    were written for the old layout. We honor overrides on the deployer module
    if present.
    """
    deployer_mod = sys.modules.get('deployer')
    if deployer_mod is None:
        return None
    value = deployer_mod.__dict__.get(attr_name)
    return value  # may be None if test set it to None or did not set at all


def _get_dynamodb():
    """Get DynamoDB resource (lazy init for test compatibility)"""
    global _dynamodb
    # Backward-compat: check deployer override first
    override = _check_deployer_override('_dynamodb')
    if override is not None:
        return override
    if _dynamodb is None:
        _dynamodb = boto3.resource('dynamodb', region_name=DEFAULT_REGION)
    return _dynamodb


def _get_projects_table():
    """Get projects table (lazy init for test compatibility)"""
    global projects_table
    override = _check_deployer_override('projects_table')
    if override is not None:
        return override
    if projects_table is None:
        projects_table = _db.deployer_projects_table._get()
    return projects_table


def _get_history_table():
    """Get history table (lazy init for test compatibility)"""
    global history_table
    override = _check_deployer_override('history_table')
    if override is not None:
        return override
    if history_table is None:
        history_table = _db.deployer_history_table._get()
    return history_table


def _get_locks_table():
    """Get locks table (lazy init for test compatibility)"""
    global locks_table
    override = _check_deployer_override('locks_table')
    if override is not None:
        return override
    if locks_table is None:
        locks_table = _db.deployer_locks_table._get()
    return locks_table


def get_git_commit_info(cwd: str = None) -> dict:
    """
    取得當前 git commit 資訊

    Returns:
        dict with commit_sha (full), commit_short (7 chars), commit_message
        若 git 不可用或非 git repo，回傳全 null 的 graceful fallback
    """
    try:
        kwargs = {'capture_output': True, 'text': True, 'timeout': 5}
        if cwd:
            kwargs['cwd'] = cwd

        sha_result = subprocess.run(['git', 'rev-parse', 'HEAD'], **kwargs)
        if sha_result.returncode != 0:
            return {'commit_sha': None, 'commit_short': None, 'commit_message': None}

        full_sha = sha_result.stdout.strip()
        short_sha = full_sha[:7] if full_sha else None

        log_result = subprocess.run(
            ['git', 'log', '-1', '--format=%h %s'],
            **kwargs
        )
        commit_message = None
        if log_result.returncode == 0 and log_result.stdout.strip():
            parts = log_result.stdout.strip().split(' ', 1)
            commit_message = parts[1] if len(parts) > 1 else None

        return {
            'commit_sha': full_sha,
            'commit_short': short_sha,
            'commit_message': commit_message,
        }
    except Exception as e:  # noqa: BLE001 — git subprocess operations, fail-safe fallback
        logger.warning("get_git_commit_info failed (graceful fallback): %s", e, extra={"src_module": "deploy_db", "operation": "get_git_commit_info", "error": str(e)})
        return {'commit_sha': None, 'commit_short': None, 'commit_message': None}


# ============================================================================
# Project Management
# ============================================================================

def list_projects() -> list:
    """列出所有專案"""
    try:
        result = _get_projects_table().scan()
        return result.get('Items', [])
    except ClientError:
        logger.exception("list_projects failed", extra={"src_module": "deploy_db", "operation": "list_projects", "error_type": "ClientError"})
        return []
    except Exception:
        logger.exception("list_projects failed", extra={"src_module": "deploy_db", "operation": "list_projects"})
        return []


def get_project(project_id: str) -> dict:
    """取得專案配置"""
    return _db.safe_get_item(_get_projects_table(), {'project_id': project_id})

def add_project(project_id: str, config: dict) -> dict:
    """新增專案配置"""
    item = {
        'project_id': project_id,
        'name': config.get('name', project_id),
        'git_repo': config.get('git_repo', ''),
        'git_repo_owner': config.get('git_repo_owner', ''),
        'default_branch': config.get('default_branch', 'master'),
        'stack_name': config.get('stack_name', ''),
        'target_account': config.get('target_account', ''),
        'target_role_arn': config.get('target_role_arn', ''),
        'secrets_id': config.get('secrets_id', ''),
        'sam_template_path': config.get('sam_template_path', '.'),
        'allowed_deployers': config.get('allowed_deployers', []),
        'enabled': True,
        'created_at': int(time.time()),
        'deploy_mode': config.get('deploy_mode', 'manual'),
        'auto_approve_deploy': config.get('auto_approve_deploy', False),
        'auto_approve_code_only': config.get('auto_approve_code_only', False),
        'template_s3_url': config.get('template_s3_url', ''),
    }
    # Filter out empty strings for optional fields to keep DDB clean
    if not item['template_s3_url']:
        del item['template_s3_url']
    _get_projects_table().put_item(Item=item)
    return item


def update_project_config(project_id: str, updates: dict) -> dict:
    """更新 project config 的部分欄位（patch 語義）。

    支援欄位：auto_approve_deploy (bool), template_s3_url (str), 及其他 config 欄位。
    不存在的 project → raise ValueError。
    """
    project = get_project(project_id)
    if not project:
        raise ValueError(f"Project {project_id!r} not found")

    if not updates:
        return project

    # Build DDB UpdateExpression
    set_parts = []
    expr_names = {}
    expr_values = {}
    for k, v in updates.items():
        placeholder = f"#upd_{k}"
        val_placeholder = f":upd_{k}"
        set_parts.append(f"{placeholder} = {val_placeholder}")
        expr_names[placeholder] = k
        expr_values[val_placeholder] = v

    _get_projects_table().update_item(
        Key={'project_id': project_id},
        UpdateExpression='SET ' + ', '.join(set_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )
    return {**project, **updates}


def remove_project(project_id: str) -> bool:
    """移除專案配置"""
    try:
        _get_projects_table().delete_item(Key={'project_id': project_id})
        return True
    except ClientError:
        logger.exception("remove_project failed", extra={"src_module": "deploy_db", "operation": "remove_project", "project_id": project_id, "error_type": "ClientError"})
        return False


# ============================================================================
# Lock Management
# ============================================================================

def acquire_lock(project_id: str, deploy_id: str, locked_by: str) -> bool:
    """嘗試取得部署鎖"""
    try:
        _get_locks_table().put_item(
            Item={
                'project_id': project_id,
                'lock_id': deploy_id,
                'locked_at': int(time.time()),
                'locked_by': locked_by,
                'ttl': int(time.time()) + 3600  # 1 小時自動過期
            },
            ConditionExpression='attribute_not_exists(project_id)'
        )
        logger.info("Deploy lock acquired", extra={
            "src_module": "deploy_db", "operation": "acquire_lock",
            "project_id": project_id,
            "deploy_id": deploy_id,
        })
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return False
        raise


def release_lock(project_id: str, deploy_id: str = None) -> bool:
    """釋放部署鎖"""
    try:
        _get_locks_table().delete_item(Key={'project_id': project_id})
        logger.info("Deploy lock released", extra={
            "src_module": "deploy_db", "operation": "release_lock",
            "project_id": project_id,
            "deploy_id": deploy_id or "unknown",
        })
        return True
    except ClientError:
        logger.exception("release_lock failed", extra={"src_module": "deploy_db", "operation": "release_lock", "project_id": project_id, "error_type": "ClientError"})
        return False

def get_lock(project_id: str) -> dict:
    """取得鎖資訊（檢查是否過期）"""
    try:
        result = _get_locks_table().get_item(Key={'project_id': project_id})
        item = result.get('Item')

        if not item:
            return None

        # 檢查 TTL 是否過期
        ttl = item.get('ttl', 0)
        if ttl and int(time.time()) > ttl:
            # Lock 已過期，自動清理
            release_lock(project_id, item.get('lock_id'))
            return None

        return item
    except ClientError:
        logger.exception("get_lock failed", extra={"src_module": "deploy_db", "operation": "get_lock", "project_id": project_id, "error_type": "ClientError"})
        return None


# ============================================================================
# Deploy History
# ============================================================================

def create_deploy_record(deploy_id: str, project_id: str, config: dict) -> dict:
    """建立部署記錄"""
    # 取得 git commit 資訊
    git_info = get_git_commit_info()

    item = {
        'deploy_id': deploy_id,
        'project_id': project_id,
        'status': 'PENDING',
        'branch': config.get('branch', 'master'),
        'started_at': int(time.time()),
        'triggered_by': config.get('triggered_by', ''),
        'reason': config.get('reason', ''),
        'commit_sha': git_info['commit_sha'],
        'commit_short': git_info['commit_short'],
        'commit_message': git_info['commit_message'],
        'ttl': int(time.time()) + TTL_30_DAYS  # 30 天
    }
    # DynamoDB 不允許存 None，過濾掉 null 欄位
    item = {k: v for k, v in item.items() if v is not None}
    _get_history_table().put_item(Item=item)
    return item


def update_deploy_record(deploy_id: str, updates: dict):
    """更新部署記錄"""
    try:
        update_expr = 'SET ' + ', '.join(f'#{k} = :{k}' for k in updates.keys())
        expr_names = {f'#{k}': k for k in updates.keys()}
        expr_values = {f':{k}': v for k, v in updates.items()}

        _get_history_table().update_item(
            Key={'deploy_id': deploy_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values
        )
    except ClientError as e:
        logger.exception(f"Error updating deploy record: {e}", extra={"src_module": "deploy_db", "operation": "update_deploy_record", "deploy_id": deploy_id})


def get_deploy_record(deploy_id: str) -> dict:
    """取得部署記錄"""
    try:
        result = _get_history_table().get_item(Key={'deploy_id': deploy_id})
        return result.get('Item')
    except ClientError:
        logger.exception("get_deploy_record failed", extra={"src_module": "deploy_db", "operation": "get_deploy_record", "deploy_id": deploy_id, "error_type": "ClientError"})
        return None

def get_deploy_history(project_id: str, limit: int = 10) -> list:
    """取得專案部署歷史"""
    try:
        result = _get_history_table().query(
            IndexName='project-time-index',
            KeyConditionExpression='project_id = :pid',
            ExpressionAttributeValues={':pid': project_id},
            ScanIndexForward=False,
            Limit=limit
        )
        return result.get('Items', [])
    except ClientError as e:
        logger.exception("Error getting deploy history: %s", e, extra={"src_module": "deploy_db", "operation": "get_deploy_history", "error": str(e)})
        return []
