"""
Bouncer Deployer Tools
MCP tools for SAM deployment
"""
import json
import os
import time
import uuid
import boto3
from botocore.exceptions import ClientError

# 環境變數
PROJECTS_TABLE = os.environ.get('PROJECTS_TABLE', 'bouncer-projects')
HISTORY_TABLE = os.environ.get('HISTORY_TABLE', 'bouncer-deploy-history')
LOCKS_TABLE = os.environ.get('LOCKS_TABLE', 'bouncer-deploy-locks')
STATE_MACHINE_ARN = os.environ.get('DEPLOY_STATE_MACHINE_ARN', '')

# DynamoDB
dynamodb = boto3.resource('dynamodb')
projects_table = dynamodb.Table(PROJECTS_TABLE)
history_table = dynamodb.Table(HISTORY_TABLE)
locks_table = dynamodb.Table(LOCKS_TABLE)

# Step Functions
sfn_client = boto3.client('stepfunctions')


# ============================================================================
# Project Management
# ============================================================================

def list_projects() -> list:
    """列出所有專案"""
    try:
        result = projects_table.scan()
        return result.get('Items', [])
    except Exception:
        return []


def get_project(project_id: str) -> dict:
    """取得專案配置"""
    try:
        result = projects_table.get_item(Key={'project_id': project_id})
        return result.get('Item')
    except Exception:
        return None


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
        'created_at': int(time.time())
    }
    projects_table.put_item(Item=item)
    return item


def remove_project(project_id: str) -> bool:
    """移除專案配置"""
    try:
        projects_table.delete_item(Key={'project_id': project_id})
        return True
    except Exception:
        return False


# ============================================================================
# Lock Management
# ============================================================================

def acquire_lock(project_id: str, deploy_id: str, locked_by: str) -> bool:
    """嘗試取得部署鎖"""
    try:
        locks_table.put_item(
            Item={
                'project_id': project_id,
                'lock_id': deploy_id,
                'locked_at': int(time.time()),
                'locked_by': locked_by,
                'ttl': int(time.time()) + 3600  # 1 小時自動過期
            },
            ConditionExpression='attribute_not_exists(project_id)'
        )
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return False
        raise


def release_lock(project_id: str) -> bool:
    """釋放部署鎖"""
    try:
        locks_table.delete_item(Key={'project_id': project_id})
        return True
    except Exception:
        return False


def get_lock(project_id: str) -> dict:
    """取得鎖資訊（檢查是否過期）"""
    try:
        result = locks_table.get_item(Key={'project_id': project_id})
        item = result.get('Item')

        if not item:
            return None

        # 檢查 TTL 是否過期
        ttl = item.get('ttl', 0)
        if ttl and int(time.time()) > ttl:
            # Lock 已過期，自動清理
            release_lock(project_id)
            return None

        return item
    except Exception:
        return None


# ============================================================================
# Deploy History
# ============================================================================

def create_deploy_record(deploy_id: str, project_id: str, config: dict) -> dict:
    """建立部署記錄"""
    item = {
        'deploy_id': deploy_id,
        'project_id': project_id,
        'status': 'PENDING',
        'branch': config.get('branch', 'master'),
        'started_at': int(time.time()),
        'triggered_by': config.get('triggered_by', ''),
        'reason': config.get('reason', ''),
        'ttl': int(time.time()) + 30 * 24 * 3600  # 30 天
    }
    history_table.put_item(Item=item)
    return item


def update_deploy_record(deploy_id: str, updates: dict):
    """更新部署記錄"""
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
        print(f"Error updating deploy record: {e}")


def get_deploy_record(deploy_id: str) -> dict:
    """取得部署記錄"""
    try:
        result = history_table.get_item(Key={'deploy_id': deploy_id})
        return result.get('Item')
    except Exception:
        return None


def get_deploy_history(project_id: str, limit: int = 10) -> list:
    """取得專案部署歷史"""
    try:
        result = history_table.query(
            IndexName='project-time-index',
            KeyConditionExpression='project_id = :pid',
            ExpressionAttributeValues={':pid': project_id},
            ScanIndexForward=False,
            Limit=limit
        )
        return result.get('Items', [])
    except Exception as e:
        print(f"Error getting deploy history: {e}")
        return []


# ============================================================================
# Deploy Trigger
# ============================================================================

def start_deploy(project_id: str, branch: str, triggered_by: str, reason: str) -> dict:
    """啟動部署"""
    # 取得專案配置
    project = get_project(project_id)
    if not project:
        return {'error': f'專案 {project_id} 不存在'}

    if not project.get('enabled', True):
        return {'error': f'專案 {project_id} 已停用'}

    # 檢查並行鎖
    existing_lock = get_lock(project_id)
    if existing_lock:
        return {
            'error': '此專案有部署正在進行中',
            'current_deploy': existing_lock.get('lock_id'),
            'locked_by': existing_lock.get('locked_by'),
            'locked_at': existing_lock.get('locked_at')
        }

    # 建立部署 ID
    deploy_id = f"deploy-{uuid.uuid4().hex[:12]}"

    # 取得鎖
    if not acquire_lock(project_id, deploy_id, triggered_by):
        return {'error': '無法取得部署鎖，可能有其他部署正在進行'}

    # 建立部署記錄
    create_deploy_record(deploy_id, project_id, {
        'branch': branch,
        'triggered_by': triggered_by,
        'reason': reason
    })

    # 準備 Step Functions 輸入
    sfn_input = {
        'deploy_id': deploy_id,
        'project_id': project_id,
        'git_repo': project.get('git_repo', ''),
        'branch': branch or project.get('default_branch', 'master'),
        'stack_name': project.get('stack_name', ''),
        'sam_template_path': project.get('sam_template_path', '.'),
        'sam_params': project.get('sam_params', ''),
        'github_pat_secret': 'sam-deployer/github-pat',
        'secrets_id': project.get('secrets_id', ''),
        'target_role_arn': project.get('target_role_arn', '')
    }

    # 啟動 Step Functions
    try:
        response = sfn_client.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=deploy_id,
            input=json.dumps(sfn_input)
        )

        # 更新部署記錄
        update_deploy_record(deploy_id, {
            'status': 'RUNNING',
            'execution_arn': response['executionArn']
        })

        return {
            'status': 'started',
            'deploy_id': deploy_id,
            'execution_arn': response['executionArn'],
            'project_id': project_id,
            'branch': sfn_input['branch']
        }

    except Exception as e:
        # 失敗時釋放鎖
        release_lock(project_id)
        update_deploy_record(deploy_id, {
            'status': 'FAILED',
            'error_message': str(e)
        })
        return {'error': f'啟動部署失敗: {str(e)}'}


def cancel_deploy(deploy_id: str) -> dict:
    """取消部署"""
    record = get_deploy_record(deploy_id)
    if not record:
        return {'error': '部署記錄不存在'}

    if record.get('status') not in ['PENDING', 'RUNNING']:
        return {'error': f'部署狀態為 {record.get("status")}，無法取消'}

    execution_arn = record.get('execution_arn')
    if execution_arn:
        try:
            sfn_client.stop_execution(
                executionArn=execution_arn,
                cause='User cancelled'
            )
        except Exception as e:
            print(f"Error stopping execution: {e}")

    # 釋放鎖
    release_lock(record.get('project_id'))

    # 更新記錄
    update_deploy_record(deploy_id, {
        'status': 'CANCELLED',
        'finished_at': int(time.time())
    })

    return {'status': 'cancelled', 'deploy_id': deploy_id}


def get_deploy_status(deploy_id: str) -> dict:
    """取得部署狀態"""
    record = get_deploy_record(deploy_id)
    if not record:
        return {'error': '部署記錄不存在'}

    # 如果有 execution_arn，查詢 Step Functions 狀態
    execution_arn = record.get('execution_arn')
    if execution_arn and record.get('status') == 'RUNNING':
        try:
            response = sfn_client.describe_execution(executionArn=execution_arn)
            sfn_status = response.get('status')

            # 同步狀態
            if sfn_status in ['SUCCEEDED', 'FAILED', 'TIMED_OUT', 'ABORTED']:
                new_status = 'SUCCESS' if sfn_status == 'SUCCEEDED' else 'FAILED'
                update_deploy_record(deploy_id, {
                    'status': new_status,
                    'finished_at': int(time.time())
                })
                record['status'] = new_status

                # 釋放鎖
                release_lock(record.get('project_id'))

        except Exception as e:
            print(f"Error getting execution status: {e}")

    return record


# ============================================================================
# MCP Tool Handlers
# ============================================================================

def mcp_tool_deploy(req_id: str, arguments: dict, table, send_approval_func) -> dict:
    """MCP tool: bouncer_deploy（需要審批）"""
    from app import mcp_result, mcp_error, generate_request_id

    project_id = str(arguments.get('project', '')).strip()
    branch = str(arguments.get('branch', '')).strip() or None
    reason = str(arguments.get('reason', '')).strip()
    source = arguments.get('source', None)
    context = arguments.get('context', None)
    async_mode = arguments.get('async', True)

    if not project_id:
        return mcp_error(req_id, -32602, 'Missing required parameter: project')

    if not reason:
        return mcp_error(req_id, -32602, 'Missing required parameter: reason')

    # 取得專案配置
    project = get_project(project_id)
    if not project:
        available = [p['project_id'] for p in list_projects()]
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': f'專案 {project_id} 不存在',
                'available_projects': available
            })}],
            'isError': True
        })

    # 檢查並行鎖
    existing_lock = get_lock(project_id)
    if existing_lock:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'error',
                'error': '此專案有部署正在進行中',
                'current_deploy': existing_lock.get('lock_id')
            })}],
            'isError': True
        })

    # 建立審批請求
    request_id = generate_request_id(f"deploy:{project_id}")
    ttl = int(time.time()) + 300 + 60

    item = {
        'request_id': request_id,
        'action': 'deploy',
        'project_id': project_id,
        'project_name': project.get('name', project_id),
        'branch': branch or project.get('default_branch', 'master'),
        'stack_name': project.get('stack_name', ''),
        'reason': reason,
        'source': source or 'mcp',  # GSI 不允許 NULL，用預設值
        'context': context or '',
        'status': 'pending_approval',
        'created_at': int(time.time()),
        'ttl': ttl,
        'mode': 'mcp'
    }
    table.put_item(Item=item)

    # 發送 Telegram 審批請求
    send_deploy_approval_request(request_id, project, branch, reason, source, context=context)

    if async_mode:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps({
                'status': 'pending_approval',
                'request_id': request_id,
                'project_id': project_id,
                'message': '部署請求已發送，等待 Telegram 確認',
                'expires_in': '300 seconds'
            })}]
        })

    # 同步模式需要等待，但這裡不實作
    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps({
            'status': 'pending_approval',
            'request_id': request_id
        })}]
    })


def mcp_tool_deploy_status(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_deploy_status"""
    from app import mcp_result, mcp_error, decimal_to_native

    deploy_id = str(arguments.get('deploy_id', '')).strip()

    if not deploy_id:
        return mcp_error(req_id, -32602, 'Missing required parameter: deploy_id')

    record = get_deploy_status(deploy_id)
    if 'error' in record:
        return mcp_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps(record)}],
            'isError': True
        })

    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps(decimal_to_native(record), indent=2, ensure_ascii=False)}]
    })


def mcp_tool_deploy_cancel(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_deploy_cancel"""
    from app import mcp_result, mcp_error

    deploy_id = str(arguments.get('deploy_id', '')).strip()

    if not deploy_id:
        return mcp_error(req_id, -32602, 'Missing required parameter: deploy_id')

    result = cancel_deploy(deploy_id)
    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps(result)}],
        'isError': 'error' in result
    })


def mcp_tool_deploy_history(req_id: str, arguments: dict) -> dict:
    """MCP tool: bouncer_deploy_history"""
    from app import mcp_result, mcp_error, decimal_to_native

    project_id = str(arguments.get('project', '')).strip()
    limit = int(arguments.get('limit', 10))

    if not project_id:
        return mcp_error(req_id, -32602, 'Missing required parameter: project')

    history = get_deploy_history(project_id, limit)
    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps({
            'project_id': project_id,
            'history': [decimal_to_native(h) for h in history]
        }, indent=2, ensure_ascii=False)}]
    })


def mcp_tool_project_list(req_id, arguments: dict) -> dict:
    """MCP tool: bouncer_project_list"""
    from app import mcp_result, decimal_to_native

    projects = list_projects()
    return mcp_result(req_id, {
        'content': [{'type': 'text', 'text': json.dumps({
            'projects': [decimal_to_native(p) for p in projects]
        }, indent=2, ensure_ascii=False)}]
    })


# ============================================================================
# Telegram Notifications
# ============================================================================

def send_deploy_approval_request(request_id: str, project: dict, branch: str, reason: str, source: str, context: str = None):
    """發送部署審批請求到 Telegram"""
    from telegram import send_telegram_message, escape_markdown

    project_id = project.get('project_id', '')
    project_name = project.get('name', project_id)
    stack_name = project.get('stack_name', '')
    target_account = project.get('target_account', '')
    # Fallback: extract account ID from target_role_arn if target_account is empty
    if not target_account:
        target_role_arn = project.get('target_role_arn', '')
        if target_role_arn and ':iam::' in target_role_arn:
            try:
                target_account = target_role_arn.split(':iam::')[1].split(':')[0]
            except (IndexError, AttributeError):
                pass

    branch = branch or project.get('default_branch', 'master')
    # Escape user-provided text for Markdown V1 (underscores break formatting)
    safe_source = escape_markdown(source) if source else ""
    safe_reason = escape_markdown(reason) if reason else ""
    safe_context = escape_markdown(context) if context else ""
    source_line = f"🤖 來源： {safe_source}\n" if source else ""
    context_line = f"📝 任務： {safe_context}\n" if context else ""
    account_line = f"🏢 帳號： {target_account}\n" if target_account else ""

    text = (
        f"🚀 SAM 部署請求\n\n"
        f"{source_line}"
        f"{context_line}"
        f"📦 專案： {project_name}\n"
        f"🌿 分支： {branch}\n"
        f"{account_line}"
        f"📋 Stack： {stack_name}\n\n"
        f"💬 原因： {safe_reason}\n\n"
        f"🆔 ID： {request_id}\n"
        f"⏰ 5 分鐘後過期"
    )

    keyboard = {
        'inline_keyboard': [[
            {'text': '✅ 批准部署', 'callback_data': f'approve:{request_id}'},
            {'text': '❌ 拒絕', 'callback_data': f'deny:{request_id}'}
        ]]
    }

    send_telegram_message(text, reply_markup=keyboard)
