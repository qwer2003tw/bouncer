#!/usr/bin/env python3
"""
Bouncer MCP Client Wrapper

本地 MCP Server，透過 stdio 與 Clawdbot 通訊，
背後呼叫 Lambda API 並輪詢等待審批結果。

使用方式：
    python bouncer_mcp.py

環境變數：
    BOUNCER_API_URL - Bouncer Lambda API URL
    BOUNCER_SECRET - 請求認證 Secret
    BOUNCER_TIMEOUT - 審批等待超時秒數（預設 300）
"""

import json
import os
import sys
import urllib.request
import urllib.error

# ============================================================================
# 配置
# ============================================================================

API_URL = os.environ.get('BOUNCER_API_URL', '')
SECRET = os.environ.get('BOUNCER_SECRET', '')
DEFAULT_TIMEOUT = int(os.environ.get('BOUNCER_TIMEOUT', '300'))  # 5 分鐘
POLL_INTERVAL = 2  # 輪詢間隔（秒）

VERSION = '2.0.0'

# ============================================================================
# MCP Tools 定義
# ============================================================================

TOOLS = [
    {
        'name': 'bouncer_eks_get_token',
        'description': '生成 kubectl EKS 認證 token（k8s-aws-v1.* 格式）。等同於 aws eks get-token，但不需要 awscli binary。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'cluster_name': {'type': 'string', 'description': 'EKS cluster 名稱（例如：ztp-eks-v2）'},
                'region': {'type': 'string', 'description': 'AWS region（預設 us-east-1）'},
                'account': {'type': 'string', 'description': '目標 AWS 帳號 ID'},
            },
            'required': ['cluster_name']
        }
    },
    {
        'name': 'bouncer_execute_native',
        'description': (
            '使用 boto3 native API 執行 AWS 操作，完全不依賴 awscli。'
            '與 bouncer_execute 相同的安全管道（compliance、blocked、trust、approval），但執行層直接呼叫 boto3。'
            '推薦用於新的 AWS 操作，特別是需要避免 awscli global flag 衝突的場景（如 eks create-cluster）。'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'aws': {
                    'type': 'object',
                    'description': 'AWS API 呼叫參數',
                    'properties': {
                        'service': {'type': 'string', 'description': 'boto3 服務名稱（例如：eks, s3, ec2）'},
                        'operation': {'type': 'string', 'description': 'boto3 方法名稱 snake_case（例如：create_cluster）'},
                        'params': {'type': 'object', 'description': 'boto3 方法的參數 dict'},
                        'region': {'type': 'string', 'description': 'AWS region（預設 us-east-1）'},
                        'account': {'type': 'string', 'description': '目標 AWS 帳號 ID（12 位數字）'},
                    },
                    'required': ['service', 'operation', 'params']
                },
                'bouncer': {
                    'type': 'object',
                    'description': 'Bouncer 審批參數',
                    'properties': {
                        'reason': {'type': 'string', 'description': '執行原因'},
                        'source': {'type': 'string', 'description': '來源描述'},
                        'trust_scope': {'type': 'string', 'description': '信任範圍識別符（必填）'},
                        'approval_timeout': {'type': 'integer', 'description': '審批等待秒數（預設 600）'},
                    },
                    'required': ['reason', 'trust_scope']
                },
            },
            'required': ['aws', 'bouncer']
        }
    },
    {
        'name': 'bouncer_status',
        'description': '查詢審批請求狀態（用於異步模式輪詢結果）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'request_id': {
                    'type': 'string',
                    'description': '請求 ID'
                }
            },
            'required': ['request_id']
        }
    },
    {
        'name': 'bouncer_help',
        'description': '查詢 AWS CLI 命令的參數說明。不需要執行命令，直接返回參數文檔。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'command': {
                    'type': 'string',
                    'description': 'AWS CLI 命令（例如：ec2 modify-instance-attribute 或 aws ec2 describe-instances）'
                },
                'service': {
                    'type': 'string',
                    'description': '只列出服務的所有操作（例如：ec2）'
                }
            }
        }
    },
    {
        'name': 'bouncer_add_account',
        'description': '新增或更新 AWS 帳號配置（需要 Telegram 審批）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'account_id': {
                    'type': 'string',
                    'description': 'AWS 帳號 ID（12 位數字）'
                },
                'name': {
                    'type': 'string',
                    'description': '帳號名稱（例如：Production, Staging）'
                },
                'role_arn': {
                    'type': 'string',
                    'description': 'IAM Role ARN（例如：arn:aws:iam::111111111111:role/BouncerRole）'
                },
                'source': {
                    'type': 'string',
                    'description': '來源標識'
                }
            },
            'required': ['account_id', 'name', 'role_arn']
        }
    },
    {
        'name': 'bouncer_list_accounts',
        'description': '列出已配置的 AWS 帳號',
        'inputSchema': {
            'type': 'object',
            'properties': {}
        }
    },
    {
        'name': 'bouncer_remove_account',
        'description': '移除 AWS 帳號配置（需要 Telegram 審批）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'account_id': {
                    'type': 'string',
                    'description': 'AWS 帳號 ID（12 位數字）'
                },
                'source': {
                    'type': 'string',
                    'description': '來源標識'
                }
            },
            'required': ['account_id']
        }
    },
    # ========== Deployer Tools ==========
    {
        'name': 'bouncer_deploy',
        'description': '部署 SAM 專案（需要 Telegram 審批）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'project': {
                    'type': 'string',
                    'description': '專案 ID（例如：bouncer）'
                },
                'branch': {
                    'type': 'string',
                    'description': 'Git 分支（預設使用專案設定的分支）'
                },
                'reason': {
                    'type': 'string',
                    'description': '部署原因'
                }
            },
            'required': ['project', 'reason']
        }
    },
    {
        'name': 'bouncer_deploy_status',
        'description': '查詢部署狀態',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'deploy_id': {
                    'type': 'string',
                    'description': '部署 ID'
                }
            },
            'required': ['deploy_id']
        }
    },
    {
        'name': 'bouncer_deploy_cancel',
        'description': '取消進行中的部署',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'deploy_id': {
                    'type': 'string',
                    'description': '部署 ID'
                }
            },
            'required': ['deploy_id']
        }
    },
    {
        'name': 'bouncer_deploy_history',
        'description': '查詢專案部署歷史',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'project': {
                    'type': 'string',
                    'description': '專案 ID'
                },
                'limit': {
                    'type': 'integer',
                    'description': '返回筆數（預設 10）'
                }
            },
            'required': ['project']
        }
    },
    {
        'name': 'bouncer_project_list',
        'description': '列出可部署的專案',
        'inputSchema': {
            'type': 'object',
            'properties': {}
        }
    },
    {
        'name': 'bouncer_list_safelist',
        'description': '列出命令分類規則：哪些命令會自動執行（safelist）、哪些會被封鎖（blocked）',
        'inputSchema': {
            'type': 'object',
            'properties': {}
        }
    },
    {
        'name': 'bouncer_get_page',
        'description': '取得長輸出的下一頁（當結果有 paged=true 時使用）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'page_id': {
                    'type': 'string',
                    'description': '分頁 ID（從 next_page 欄位取得）'
                }
            },
            'required': ['page_id']
        }
    },
    {
        'name': 'bouncer_list_pending',
        'description': '列出待審批的請求',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'source': {
                    'type': 'string',
                    'description': '來源標識（不填則列出所有）'
                },
                'limit': {
                    'type': 'integer',
                    'description': '最大數量（預設 20）'
                }
            }
        }
    },
    {
        'name': 'bouncer_trust_status',
        'description': '查詢當前的信任時段狀態',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'source': {
                    'type': 'string',
                    'description': '來源標識（不填則查詢所有活躍時段）'
                }
            }
        }
    },
    {
        'name': 'bouncer_trust_revoke',
        'description': '撤銷信任時段',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'trust_id': {
                    'type': 'string',
                    'description': '信任時段 ID'
                }
            },
            'required': ['trust_id']
        }
    },
    {
        'name': 'bouncer_upload',
        'description': '上傳檔案到固定 S3 桶（需要 Telegram 審批）。預設異步返回 request_id，用 bouncer_status 查詢結果。檔案大小限制 4.5 MB。有 Trust Session 時可自動上傳。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'filename': {
                    'type': 'string',
                    'description': '檔案名稱（例如 template.yaml）'
                },
                'content': {
                    'type': 'string',
                    'description': '檔案內容（base64 encoded）'
                },
                'content_type': {
                    'type': 'string',
                    'description': 'Content-Type（預設 application/octet-stream）'
                },
                'reason': {
                    'type': 'string',
                    'description': '上傳原因'
                },
                'source': {
                    'type': 'string',
                    'description': '請求來源標識'
                },
                'trust_scope': {
                    'type': 'string',
                    'description': '信任範圍識別符（可選，有 Trust Session 時可自動上傳）'
                },
                'sync': {
                    'type': 'boolean',
                    'description': '同步模式：等待審批結果（可能超時），預設 false'
                }
            },
            'required': ['filename', 'content', 'reason', 'source']
        }
    },
    {
        'name': 'bouncer_upload_batch',
        'description': '批量上傳多個檔案到 S3，一次審批。有 Trust Session 時可自動上傳。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'files': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'filename': {'type': 'string'},
                            'content': {'type': 'string', 'description': 'base64 encoded'},
                            'content_type': {'type': 'string'}
                        },
                        'required': ['filename', 'content']
                    },
                    'description': '要上傳的檔案清單'
                },
                'reason': {
                    'type': 'string',
                    'description': '上傳原因'
                },
                'source': {
                    'type': 'string',
                    'description': '請求來源標識'
                },
                'trust_scope': {
                    'type': 'string',
                    'description': '信任範圍識別符（可選，有 Trust Session 時可自動上傳）'
                },
                'account': {
                    'type': 'string',
                    'description': '目標 AWS 帳號 ID'
                }
            },
            'required': ['files', 'reason', 'source']
        }
    },
    {
        'name': 'bouncer_request_presigned',
        'description': '生成單檔 S3 presigned PUT URL，client 直接 PUT 大檔案，不經 Lambda，無大小限制，不需審批。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'filename': {'type': 'string', 'description': '目標檔名（含路徑，如 assets/foo.js）'},
                'content_type': {'type': 'string', 'description': 'MIME type'},
                'reason': {'type': 'string', 'description': '上傳原因'},
                'source': {'type': 'string', 'description': '請求來源標識'},
                'account': {'type': 'string', 'description': '目標帳號（選填）'},
                'expires_in': {'type': 'integer', 'description': 'URL 有效期秒數（預設 900，min 60，max 3600）'}
            },
            'required': ['filename', 'content_type', 'reason', 'source']
        }
    },
    {
        'name': 'bouncer_request_presigned_batch',
        'description': '批量生成 N 個 presigned PUT URL，前端部署推薦用法。一次呼叫，所有檔案共用 batch_id prefix，不需審批。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'files': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'filename': {'type': 'string'},
                            'content_type': {'type': 'string'}
                        },
                        'required': ['filename', 'content_type']
                    },
                    'description': '檔案清單（最多 50 個）'
                },
                'reason': {'type': 'string', 'description': '上傳原因'},
                'source': {'type': 'string', 'description': '請求來源標識'},
                'account': {'type': 'string', 'description': '目標帳號（選填）'},
                'expires_in': {'type': 'integer', 'description': 'URL 有效期秒數（預設 900）'}
            },
            'required': ['files', 'reason', 'source']
        }
    },
    {
        'name': 'bouncer_request_grant',
        'description': '申請批次權限授予。Agent 預先列出需要的命令，Steven 一鍵審批後，Agent 可在 TTL 內自動執行。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'commands': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': '需要授權的 AWS CLI 命令清單（1-20 個）'
                },
                'reason': {
                    'type': 'string',
                    'description': '申請原因'
                },
                'source': {
                    'type': 'string',
                    'description': '請求來源標識'
                },
                'ttl_minutes': {
                    'type': 'integer',
                    'description': '有效期（分鐘），預設 30，最大 60'
                },
                'allow_repeat': {
                    'type': 'boolean',
                    'description': '是否允許同一命令重複執行，預設 false'
                },
                'approval_timeout': {
                    'type': 'integer',
                    'description': '審批等待時間（秒），預設 300（5分鐘），最大 900（15分鐘）。多步驟操作建議設 600-900。',
                    'minimum': 60,
                    'maximum': 900
                },
                'account': {
                    'type': 'string',
                    'description': '目標 AWS 帳號 ID'
                }
            },
            'required': ['commands', 'reason', 'source']
        }
    },
    {
        'name': 'bouncer_grant_status',
        'description': '查詢 Grant Session 狀態（剩餘命令、剩餘時間）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'grant_id': {
                    'type': 'string',
                    'description': 'Grant Session ID'
                },
                'source': {
                    'type': 'string',
                    'description': '請求來源（用於驗證）'
                }
            },
            'required': ['grant_id', 'source']
        }
    },
    {
        'name': 'bouncer_revoke_grant',
        'description': '撤銷 Grant Session',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'grant_id': {
                    'type': 'string',
                    'description': 'Grant Session ID'
                }
            },
            'required': ['grant_id']
        }
    },
    {
        'name': 'bouncer_request_frontend_presigned',
        'description': '前端部署 Step 1：生成 presigned PUT URL，繞過 API GW 6MB 限制。Agent 用 presigned URL 直接 PUT 檔案到 S3，然後呼叫 bouncer_confirm_frontend_deploy。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'files': {
                    'type': 'array',
                    'description': '檔案 metadata 清單（不含 content）',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'filename': {'type': 'string'},
                            'content_type': {'type': 'string'}
                        },
                        'required': ['filename']
                    }
                },
                'project': {
                    'type': 'string',
                    'description': '專案名稱（如 ztp-files）'
                },
                'reason': {
                    'type': 'string',
                    'description': '部署原因'
                },
                'source': {
                    'type': 'string',
                    'description': '來源'
                },
                'trust_scope': {
                    'type': 'string',
                    'description': '信任範圍識別符'
                },
                'account_id': {
                    'type': 'string',
                    'description': 'AWS 帳號 ID（選填）'
                }
            },
            'required': ['files', 'project']
        }
    },
    {
        'name': 'bouncer_confirm_frontend_deploy',
        'description': '前端部署 Step 2：確認所有檔案已上傳，建立人工審批請求。先呼叫 bouncer_request_frontend_presigned 取得 presigned URLs，上傳後再呼叫此 tool。',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'request_id': {
                    'type': 'string',
                    'description': 'Step 1 回傳的 request_id'
                },
                'files': {
                    'type': 'array',
                    'description': '與 Step 1 相同的檔案 metadata 清單',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'filename': {'type': 'string'},
                            'content_type': {'type': 'string'}
                        },
                        'required': ['filename']
                    }
                },
                'project': {
                    'type': 'string',
                    'description': '專案名稱'
                },
                'reason': {
                    'type': 'string',
                    'description': '部署原因'
                },
                'source': {
                    'type': 'string',
                    'description': '來源'
                },
                'trust_scope': {
                    'type': 'string',
                    'description': '信任範圍識別符'
                },
                'account_id': {
                    'type': 'string',
                    'description': 'AWS 帳號 ID（選填）'
                }
            },
            'required': ['request_id', 'files', 'project']
        }
    },
    {
        'name': 'bouncer_grant_execute',
        'description': '在已批准的 Grant Session 內執行 AWS 操作（boto3 native 格式，精確匹配授權清單）',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'grant_id': {
                    'type': 'string',
                    'description': 'Grant Session ID'
                },
                'aws': {
                    'type': 'object',
                    'description': 'AWS API 呼叫參數',
                    'properties': {
                        'service': {
                            'type': 'string',
                            'description': 'boto3 服務名稱（例如：eks, s3, ec2）'
                        },
                        'operation': {
                            'type': 'string',
                            'description': 'boto3 方法名稱（snake_case，例如：create_cluster）'
                        },
                        'params': {
                            'type': 'object',
                            'description': 'boto3 方法的參數 dict'
                        },
                        'region': {
                            'type': 'string',
                            'description': 'AWS region（不填則使用環境變數）'
                        }
                    },
                    'required': ['service', 'operation', 'params']
                },
                'source': {
                    'type': 'string',
                    'description': '請求來源標識（必須與 grant 建立時的 source 一致）'
                },
                'account': {
                    'type': 'string',
                    'description': '目標 AWS 帳號 ID'
                },
                'reason': {
                    'type': 'string',
                    'description': '執行原因（用於 audit log）'
                }
            },
            'required': ['grant_id', 'aws', 'source']
        }
    },
    # ========== CloudWatch Logs Query Tools ==========
    {
        'name': 'bouncer_query_logs',
        'description': (
            '查詢 CloudWatch Log Insights。log_group 必須在允許名單中（用 bouncer_logs_allowlist 管理）。'
            '支援跨帳號查詢、自訂 Log Insights 查詢語法、時間範圍過濾。'
            '時間範圍最大 30 天，結果最大 1000 筆。'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'log_group': {
                    'type': 'string',
                    'description': 'CloudWatch Log Group 名稱（例如：/aws/lambda/my-function）'
                },
                'query': {
                    'type': 'string',
                    'description': 'CloudWatch Logs Insights 查詢語法（預設：fields @timestamp, @message | sort @timestamp desc）'
                },
                'filter_pattern': {
                    'type': 'string',
                    'description': '簡易文字過濾（未提供 query 時，自動轉為 filter @message like /pattern/）'
                },
                'start_time': {
                    'type': 'integer',
                    'description': '查詢起始時間（Unix timestamp 秒），預設 1 小時前'
                },
                'end_time': {
                    'type': 'integer',
                    'description': '查詢結束時間（Unix timestamp 秒），預設現在'
                },
                'limit': {
                    'type': 'integer',
                    'description': '最大結果筆數（預設 100，最大 1000）'
                },
                'account': {
                    'type': 'string',
                    'description': '目標 AWS 帳號 ID（不填則使用預設帳號）'
                },
                'region': {
                    'type': 'string',
                    'description': 'AWS region（不填則使用環境變數）'
                }
            },
            'required': ['log_group']
        }
    },
    {
        'name': 'bouncer_logs_allowlist',
        'description': (
            '管理 CloudWatch Logs 查詢的允許名單。'
            '支援 add（加入）、remove（移除）、list（列出）、add_batch（批量加入）。'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'action': {
                    'type': 'string',
                    'description': '操作類型：add / remove / list / add_batch',
                    'enum': ['add', 'remove', 'list', 'add_batch']
                },
                'log_group': {
                    'type': 'string',
                    'description': 'Log Group 名稱（add / remove 時必填）'
                },
                'log_groups': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Log Group 名稱清單（add_batch 時必填，最多 50 個）'
                },
                'account': {
                    'type': 'string',
                    'description': '目標 AWS 帳號 ID（不填則使用預設帳號）'
                },
                'source': {
                    'type': 'string',
                    'description': '請求來源標識'
                }
            },
            'required': ['action']
        }
    },
    {
        'name': 'bouncer_agent_key_revoke',
        'description': 'Revoke an agent API key by prefix.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'key_prefix': {'type': 'string', 'description': 'Key prefix (e.g. bncr_priv_a1b2c3d4)'},
                'agent_id': {'type': 'string', 'description': 'Agent identifier'},
            },
            'required': ['key_prefix', 'agent_id']
        }
    },
    {
        'name': 'bouncer_agent_key_list',
        'description': 'List agent API keys (never shows full key or hash).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'agent_id': {'type': 'string', 'description': 'Filter by agent ID (optional)'},
            }
        }
    },
    ]

# ============================================================================
# HTTP 請求
# ============================================================================

def http_request(method: str, path: str, data: dict = None) -> dict:
    """發送 HTTP 請求到 Bouncer API"""
    url = f"{API_URL.rstrip('/')}{path}"

    headers = {
        'Content-Type': 'application/json',
        'X-Approval-Secret': SECRET
    }

    body = json.dumps(data).encode() if data else None

    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        try:
            return json.loads(error_body)
        except Exception:
            return {'error': error_body, 'status_code': e.code}
    except Exception as e:
        return {'error': str(e)}

# ============================================================================
# Tool 實作
# ============================================================================

def tool_status(arguments: dict) -> dict:
    """查詢請求狀態"""
    request_id = arguments.get('request_id', '')

    if not request_id:
        return {'error': 'Missing required parameter: request_id'}

    return http_request('GET', f'/status/{request_id}')


def tool_help(arguments: dict) -> dict:
    """查詢 AWS CLI 命令說明"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'help',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_help',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)

    # 解析 MCP 回應
    if 'result' in result:
        content = result['result'].get('content', [])
        if content and content[0].get('type') == 'text':
            try:
                return json.loads(content[0]['text'])
            except Exception:
                return result

    return result


def tool_add_account(arguments: dict) -> dict:
    """新增帳號（立即返回，不做本地輪詢）"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'add-account',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_add_account',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)

    # 解析 MCP 回應
    inner_result = None
    if 'result' in result:
        content = result['result'].get('content', [])
        if content and content[0].get('type') == 'text':
            try:
                inner_result = json.loads(content[0]['text'])
            except Exception:
                return result

    if not inner_result:
        return result

    # 直接返回結果（不做本地輪詢）
    return inner_result


def tool_list_accounts(arguments: dict) -> dict:
    """列出帳號（走 MCP 端點）"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'list-accounts',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_list_accounts',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)

    if 'result' in result:
        content = result['result'].get('content', [])
        if content and content[0].get('type') == 'text':
            try:
                return json.loads(content[0]['text'])
            except Exception:
                return content[0]
    return result


def tool_remove_account(arguments: dict) -> dict:
    """移除帳號（立即返回，不做本地輪詢）"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'remove-account',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_remove_account',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)

    inner_result = None
    if 'result' in result:
        content = result['result'].get('content', [])
        if content and content[0].get('type') == 'text':
            try:
                inner_result = json.loads(content[0]['text'])
            except Exception:
                return result

    if not inner_result:
        return result

    # 直接返回結果（不做本地輪詢）
    return inner_result


# ============================================================================
# Deployer Tools
# ============================================================================

def tool_deploy(arguments: dict) -> dict:
    """部署專案（立即返回，不做本地輪詢）"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'deploy',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_deploy',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    inner_result = parse_mcp_result(result)

    if not inner_result:
        return result

    # 直接返回結果（不做本地輪詢）
    return inner_result


def tool_deploy_status(arguments: dict) -> dict:
    """查詢部署狀態"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'deploy-status',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_deploy_status',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_deploy_cancel(arguments: dict) -> dict:
    """取消部署"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'deploy-cancel',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_deploy_cancel',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_deploy_history(arguments: dict) -> dict:
    """查詢部署歷史"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'deploy-history',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_deploy_history',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_project_list(arguments: dict) -> dict:
    """列出專案"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'project-list',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_project_list',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_list_safelist(arguments: dict) -> dict:
    """列出 safelist 和 blocked patterns"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'list-safelist',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_list_safelist',
            'arguments': {}
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_get_page(arguments: dict) -> dict:
    """取得長輸出的下一頁"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_get_page',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_list_pending(arguments: dict) -> dict:
    """列出待審批的請求"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'list-pending',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_list_pending',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_trust_status(arguments: dict) -> dict:
    """查詢信任時段狀態"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'trust-status',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_trust_status',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_trust_revoke(arguments: dict) -> dict:
    """撤銷信任時段"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'trust-revoke',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_trust_revoke',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_upload(arguments: dict) -> dict:
    """上傳檔案到 S3"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'upload',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_upload',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_upload_batch(arguments: dict) -> dict:
    """批量上傳檔案到 S3"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'upload_batch',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_upload_batch',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_request_presigned(arguments: dict) -> dict:
    """生成單檔 S3 presigned PUT URL（不過 Lambda，無大小限制）"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'request_presigned',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_request_presigned',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_request_presigned_batch(arguments: dict) -> dict:
    """批量生成 S3 presigned PUT URL，前端部署推薦用法"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'request_presigned_batch',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_request_presigned_batch',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_request_grant(arguments: dict) -> dict:
    """申請批次權限授予"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'request_grant',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_request_grant',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_grant_status(arguments: dict) -> dict:
    """查詢 Grant Session 狀態"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'grant_status',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_grant_status',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_request_frontend_presigned(arguments: dict) -> dict:
    """前端部署 Step 1：生成 presigned PUT URLs"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'request_frontend_presigned',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_request_frontend_presigned',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_confirm_frontend_deploy(arguments: dict) -> dict:
    """前端部署 Step 2：確認上傳 + 建立審批請求"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'confirm_frontend_deploy',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_confirm_frontend_deploy',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_execute_native(arguments: dict) -> dict:
    """執行 AWS boto3 native API 呼叫，等待審批（立即返回）。"""
    aws_args = arguments.get('aws', {})
    bouncer_args = arguments.get('bouncer', {})

    if not aws_args.get('service'):
        return {'error': 'Missing required parameter: aws.service'}
    if not aws_args.get('operation'):
        return {'error': 'Missing required parameter: aws.operation'}
    if not bouncer_args.get('trust_scope'):
        return {'error': 'Missing required parameter: bouncer.trust_scope'}
    if not bouncer_args.get('reason'):
        return {'error': 'Missing required parameter: bouncer.reason'}

    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    mcp_args = {
        'aws': {k: v for k, v in {
            'service': aws_args.get('service'),
            'operation': aws_args.get('operation'),
            'params': aws_args.get('params', {}),
            'region': aws_args.get('region', 'us-east-1'),
            'account': aws_args.get('account'),
        }.items() if v is not None},
        'bouncer': {k: v for k, v in {
            'reason': bouncer_args.get('reason'),
            'source': bouncer_args.get('source', 'OpenClaw Agent'),
            'trust_scope': bouncer_args.get('trust_scope'),
            'approval_timeout': bouncer_args.get('approval_timeout', 600),
            'key': bouncer_args.get('key'),
        }.items() if v is not None},
    }

    payload = {
        'jsonrpc': '2.0',
        'id': 'execute_native',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_execute_native',
            'arguments': mcp_args
        }
    }

    result = http_request('POST', '/mcp', payload)

    # Parse MCP response
    if 'result' in result:
        content = result['result'].get('content', [])
        if content and content[0].get('type') == 'text':
            try:
                return json.loads(content[0]['text'])
            except Exception:
                return {'result': content[0]['text']}
    if 'error' in result:
        return result
    return result


def tool_eks_get_token(arguments: dict) -> dict:
    """Generate EKS kubectl token via STS presigned URL."""
    cluster_name = str(arguments.get('cluster_name', '')).strip()
    region = str(arguments.get('region', 'us-east-1')).strip()
    account = arguments.get('account', None)
    if account:
        account = str(account).strip()

    if not cluster_name:
        return {'error': 'Missing required parameter: cluster_name'}
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    mcp_args = {'cluster_name': cluster_name, 'region': region}
    if account:
        mcp_args['account'] = account

    payload = {
        'jsonrpc': '2.0', 'id': 'eks_get_token',
        'method': 'tools/call',
        'params': {'name': 'bouncer_eks_get_token', 'arguments': mcp_args}
    }
    result = http_request('POST', '/mcp', payload)
    if 'result' in result:
        content_list = result['result'].get('content', [])
        if content_list and content_list[0].get('type') == 'text':
            try:
                import json as _json
                return _json.loads(content_list[0]['text'])
            except Exception:
                return {'result': content_list[0]['text']}
    return result


def tool_grant_execute(arguments: dict) -> dict:
    """在已批准的 Grant Session 內執行命令（精確匹配）"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'grant_execute',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_grant_execute',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_revoke_grant(arguments: dict) -> dict:
    """撤銷 Grant Session"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'revoke_grant',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_revoke_grant',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_query_logs(arguments: dict) -> dict:
    """查詢 CloudWatch Log Insights"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'query_logs',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_query_logs',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_logs_allowlist(arguments: dict) -> dict:
    """管理 logs 查詢允許名單"""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}

    payload = {
        'jsonrpc': '2.0',
        'id': 'logs_allowlist',
        'method': 'tools/call',
        'params': {
            'name': 'bouncer_logs_allowlist',
            'arguments': arguments
        }
    }

    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def parse_mcp_result(result: dict) -> dict:
    """解析 MCP 回應"""
    if 'result' in result:
        content = result['result'].get('content', [])
        if content and content[0].get('type') == 'text':
            try:
                return json.loads(content[0]['text'])
            except Exception:
                pass
    return None

# ============================================================================
# MCP Server
# ============================================================================

def log(msg: str):
    """寫 log 到 stderr（不影響 stdout 的 JSON-RPC）"""
    print(f"[Bouncer] {msg}", file=sys.stderr)


def handle_request(request: dict) -> dict:
    """處理 JSON-RPC 請求"""
    method = request.get('method', '')
    params = request.get('params', {})
    req_id = request.get('id')

    if request.get('jsonrpc') != '2.0':
        return error_response(req_id, -32600, 'Invalid Request')

    if method == 'initialize':
        return success_response(req_id, {
            'protocolVersion': '2024-11-05',
            'serverInfo': {'name': 'bouncer-client', 'version': VERSION},
            'capabilities': {'tools': {}}
        })

    elif method == 'notifications/initialized':
        return success_response(req_id, {})

    elif method == 'tools/list':
        return success_response(req_id, {'tools': TOOLS})

    elif method == 'tools/call':
        tool_name = params.get('name', '')
        arguments = params.get('arguments', {})

        # Track tool usage via CloudWatch EMF
        try:
            from metrics import emit_metric
            emit_metric('Bouncer', 'ToolCall', 1, dimensions={'ToolName': tool_name})
        except Exception:  # noqa: BLE001 — metrics are best-effort, never block tool execution
            pass

        if tool_name == 'bouncer_eks_get_token':
            result = tool_eks_get_token(arguments)
        elif tool_name == 'bouncer_execute_native':
            result = tool_execute_native(arguments)
        elif tool_name == 'bouncer_status':
            result = tool_status(arguments)
        elif tool_name == 'bouncer_help':
            result = tool_help(arguments)
        elif tool_name == 'bouncer_add_account':
            result = tool_add_account(arguments)
        elif tool_name == 'bouncer_list_accounts':
            result = tool_list_accounts(arguments)
        elif tool_name == 'bouncer_remove_account':
            result = tool_remove_account(arguments)
        # Deployer tools
        elif tool_name == 'bouncer_deploy':
            result = tool_deploy(arguments)
        elif tool_name == 'bouncer_deploy_status':
            result = tool_deploy_status(arguments)
        elif tool_name == 'bouncer_deploy_cancel':
            result = tool_deploy_cancel(arguments)
        elif tool_name == 'bouncer_deploy_history':
            result = tool_deploy_history(arguments)
        elif tool_name == 'bouncer_project_list':
            result = tool_project_list(arguments)
        elif tool_name == 'bouncer_list_safelist':
            result = tool_list_safelist(arguments)
        elif tool_name == 'bouncer_get_page':
            result = tool_get_page(arguments)
        elif tool_name == 'bouncer_list_pending':
            result = tool_list_pending(arguments)
        elif tool_name == 'bouncer_trust_status':
            result = tool_trust_status(arguments)
        elif tool_name == 'bouncer_trust_revoke':
            result = tool_trust_revoke(arguments)
        elif tool_name == 'bouncer_upload':
            result = tool_upload(arguments)
        elif tool_name == 'bouncer_upload_batch':
            result = tool_upload_batch(arguments)
        elif tool_name == 'bouncer_request_presigned':
            result = tool_request_presigned(arguments)
        elif tool_name == 'bouncer_request_presigned_batch':
            result = tool_request_presigned_batch(arguments)
        # Grant session tools
        elif tool_name == 'bouncer_request_grant':
            result = tool_request_grant(arguments)
        elif tool_name == 'bouncer_grant_status':
            result = tool_grant_status(arguments)
        elif tool_name == 'bouncer_revoke_grant':
            result = tool_revoke_grant(arguments)
        # Frontend deploy
        elif tool_name == 'bouncer_request_frontend_presigned':
            result = tool_request_frontend_presigned(arguments)
        elif tool_name == 'bouncer_confirm_frontend_deploy':
            result = tool_confirm_frontend_deploy(arguments)
        # Grant execute
        elif tool_name == 'bouncer_grant_execute':
            result = tool_grant_execute(arguments)
        # CloudWatch Logs query
        elif tool_name == 'bouncer_query_logs':
            result = tool_query_logs(arguments)
        elif tool_name == 'bouncer_logs_allowlist':
            result = tool_logs_allowlist(arguments)
        elif tool_name == 'bouncer_agent_key_revoke':
            result = tool_agent_key_revoke(arguments)
        elif tool_name == 'bouncer_agent_key_list':
            result = tool_agent_key_list(arguments)
        else:
            return error_response(req_id, -32602, f'Unknown tool: {tool_name}')

        is_error = 'error' in result or result.get('status') in ('denied', 'timeout', 'blocked')

        return success_response(req_id, {
            'content': [{'type': 'text', 'text': json.dumps(result, indent=2, ensure_ascii=False)}],
            'isError': is_error
        })

    else:
        return error_response(req_id, -32601, f'Method not found: {method}')


def success_response(req_id, result) -> dict:
    return {'jsonrpc': '2.0', 'id': req_id, 'result': result}


def error_response(req_id, code: int, message: str) -> dict:
    return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': code, 'message': message}}


def main():
    log(f"MCP Client Wrapper v{VERSION} started")
    log(f"API: {API_URL}")
    log(f"Secret configured: {'Yes' if SECRET else 'No'}")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
            response = handle_request(request)
            print(json.dumps(response), flush=True)
        except json.JSONDecodeError as e:
            print(json.dumps(error_response(None, -32700, f'Parse error: {e}')), flush=True)
        except Exception as e:
            log(f"Error: {e}")
            print(json.dumps(error_response(None, -32603, f'Internal error: {e}')), flush=True)


def tool_agent_key_revoke(arguments: dict) -> dict:
    """Revoke an agent API key."""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}
    payload = {
        'jsonrpc': '2.0', 'id': 'agent_key_revoke',
        'method': 'tools/call',
        'params': {'name': 'bouncer_agent_key_revoke', 'arguments': arguments}
    }
    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


def tool_agent_key_list(arguments: dict) -> dict:
    """List agent API keys."""
    if not SECRET:
        return {'error': 'BOUNCER_SECRET not configured'}
    payload = {
        'jsonrpc': '2.0', 'id': 'agent_key_list',
        'method': 'tools/call',
        'params': {'name': 'bouncer_agent_key_list', 'arguments': arguments}
    }
    result = http_request('POST', '/mcp', payload)
    return parse_mcp_result(result) or result


if __name__ == '__main__':
    main()


