"""
Bouncer - MCP Tool Schema 定義
所有 MCP tool 的 JSON Schema 定義
"""

MCP_TOOLS = {
    'bouncer_execute': {
        'description': '執行 AWS CLI 命令。安全命令自動執行，危險命令需要 Telegram 審批。預設異步返回 request_id，用 bouncer_status 查詢結果。',
        'parameters': {
            'type': 'object',
            'properties': {
                'command': {
                    'type': 'string',
                    'description': 'AWS CLI 命令（例如：aws ec2 describe-instances）'
                },
                'account': {
                    'type': 'string',
                    'description': '目標 AWS 帳號 ID（12 位數字），不填則使用預設帳號'
                },
                'reason': {
                    'type': 'string',
                    'description': '執行原因（用於審批記錄）',
                    'default': 'No reason provided'
                },
                'source': {
                    'type': 'string',
                    'description': '請求來源描述（顯示用，例如：Private Bot (Bouncer 部署)）'
                },
                'trust_scope': {
                    'type': 'string',
                    'description': '信任範圍識別符（必填，用於 Trust Session 匹配。使用 session key 或穩定 ID，不要用任務描述）'
                },
                'context': {
                    'type': 'string',
                    'description': '任務上下文說明（例如：清除 bouncer deploy lock，準備部署 schema 修復）'
                },
                'sync': {
                    'type': 'boolean',
                    'description': '同步模式：等待審批結果（可能超時），預設 false',
                    'default': False
                },
                'grant_id': {
                    'type': 'string',
                    'description': 'Grant Session ID（如果有有效的 grant session，命令將自動執行）'
                }
            },
            'required': ['command', 'trust_scope']
        }
    },
    'bouncer_status': {
        'description': '查詢請求狀態（用於異步模式輪詢結果）',
        'parameters': {
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
    'bouncer_help': {
        'description': '查詢 AWS CLI 命令的參數說明，不需要執行命令',
        'parameters': {
            'type': 'object',
            'properties': {
                'command': {
                    'type': 'string',
                    'description': 'AWS CLI 命令（例如：ec2 modify-instance-attribute）'
                },
                'service': {
                    'type': 'string',
                    'description': '只列出服務的所有操作（例如：ec2）'
                }
            }
        }
    },
    'bouncer_list_safelist': {
        'description': '列出自動批准的命令前綴',
        'parameters': {
            'type': 'object',
            'properties': {}
        }
    },
    'bouncer_trust_status': {
        'description': '查詢當前的信任時段狀態',
        'parameters': {
            'type': 'object',
            'properties': {
                'source': {
                    'type': 'string',
                    'description': '來源標識（不填則查詢所有活躍時段）'
                }
            }
        }
    },
    'bouncer_trust_revoke': {
        'description': '撤銷信任時段',
        'parameters': {
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
    'bouncer_add_account': {
        'description': '新增或更新 AWS 帳號配置（需要 Telegram 審批）',
        'parameters': {
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
                    'description': '請求來源識別（例如：Private Bot）'
                },
                'context': {
                    'type': 'string',
                    'description': '任務上下文說明'
                },
                'async': {
                    'type': 'boolean',
                    'description': '異步模式：立即返回 pending，不等審批結果（避免 API Gateway 超時）'
                }
            },
            'required': ['account_id', 'name', 'role_arn']
        }
    },
    'bouncer_list_accounts': {
        'description': '列出已配置的 AWS 帳號',
        'parameters': {
            'type': 'object',
            'properties': {}
        }
    },
    'bouncer_get_page': {
        'description': '取得長輸出的下一頁（當結果有 paged=true 時使用）',
        'parameters': {
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
    'bouncer_list_pending': {
        'description': '列出待審批的請求',
        'parameters': {
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
    'bouncer_remove_account': {
        'description': '移除 AWS 帳號配置（需要 Telegram 審批）',
        'parameters': {
            'type': 'object',
            'properties': {
                'account_id': {
                    'type': 'string',
                    'description': 'AWS 帳號 ID（12 位數字）'
                },
                'source': {
                    'type': 'string',
                    'description': '請求來源識別（例如：Private Bot）'
                },
                'context': {
                    'type': 'string',
                    'description': '任務上下文說明'
                },
                'async': {
                    'type': 'boolean',
                    'description': '異步模式：立即返回 pending，不等審批結果'
                }
            },
            'required': ['account_id']
        }
    },
    # ========== Deployer Tools ==========
    'bouncer_deploy': {
        'description': '部署 SAM 專案（需要 Telegram 審批）',
        'parameters': {
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
                },
                'source': {
                    'type': 'string',
                    'description': '請求來源標識（哪個 agent/系統發的）'
                },
                'context': {
                    'type': 'string',
                    'description': '任務上下文說明'
                }
            },
            'required': ['project', 'reason']
        }
    },
    'bouncer_deploy_status': {
        'description': '查詢部署狀態',
        'parameters': {
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
    'bouncer_deploy_cancel': {
        'description': '取消進行中的部署',
        'parameters': {
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
    'bouncer_deploy_history': {
        'description': '查詢專案部署歷史',
        'parameters': {
            'type': 'object',
            'properties': {
                'project': {
                    'type': 'string',
                    'description': '專案 ID'
                },
                'limit': {
                    'type': 'integer',
                    'description': '返回筆數（預設 10）',
                    'default': 10
                }
            },
            'required': ['project']
        }
    },
    'bouncer_project_list': {
        'description': '列出可部署的專案',
        'parameters': {
            'type': 'object',
            'properties': {}
        }
    },
    # ========== Grant Session Tools ==========
    'bouncer_request_grant': {
        'description': '批次申請多個 AWS CLI 命令的執行權限。每個命令會經過預檢分類（可授權/需個別審批/已攔截），經 Telegram 審批後可在 TTL 內自動執行。',
        'parameters': {
            'type': 'object',
            'properties': {
                'commands': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': '要申請的 AWS CLI 命令清單（1-20 個）'
                },
                'reason': {
                    'type': 'string',
                    'description': '申請原因'
                },
                'source': {
                    'type': 'string',
                    'description': '請求來源標識'
                },
                'account': {
                    'type': 'string',
                    'description': '目標 AWS 帳號 ID（不填則使用預設帳號）'
                },
                'ttl_minutes': {
                    'type': 'integer',
                    'description': 'Grant 有效時間（分鐘），預設 30，最大 60',
                    'default': 30
                },
                'allow_repeat': {
                    'type': 'boolean',
                    'description': '是否允許重複執行同一命令，預設 false',
                    'default': False
                }
            },
            'required': ['commands', 'reason', 'source']
        }
    },
    'bouncer_grant_status': {
        'description': '查詢 Grant Session 狀態（已授權命令數、已使用數、剩餘時間等）',
        'parameters': {
            'type': 'object',
            'properties': {
                'grant_id': {
                    'type': 'string',
                    'description': 'Grant Session ID'
                },
                'source': {
                    'type': 'string',
                    'description': '請求來源標識（必須與申請時一致）'
                }
            },
            'required': ['grant_id', 'source']
        }
    },
    'bouncer_revoke_grant': {
        'description': '撤銷 Grant Session',
        'parameters': {
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
    # ========== Upload Tool ==========
    'bouncer_upload': {
        'description': '上傳檔案到 S3 桶（需要 Telegram 審批）。支援跨帳號上傳，檔案會上傳到 bouncer-uploads-{account_id} 桶，30 天後自動刪除。如果有活躍的 Trust Session 且 trust_scope 匹配，可自動上傳（不需審批）。',
        'parameters': {
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
                    'description': 'Content-Type（預設 application/octet-stream）',
                    'default': 'application/octet-stream'
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
                    'description': '目標 AWS 帳號 ID（預設使用 Bouncer 所在帳號）'
                },
                'sync': {
                    'type': 'boolean',
                    'description': '同步模式：等待審批結果（可能超時），預設 false',
                    'default': False
                }
            },
            'required': ['filename', 'content', 'reason', 'source']
        }
    },
    # ========== Presigned Upload Tool ==========
    'bouncer_request_presigned': {
        'description': (
            '為 S3 staging bucket 生成單檔 presigned PUT URL，讓 client 直接 PUT 大檔案（不經 Lambda，無大小限制）。\n'
            '不需要審批。\n\n'
            '使用流程：\n'
            '  Step 1: 呼叫此工具取得 presigned_url + s3_key\n'
            '  Step 2: curl -X PUT -H "Content-Type: {content_type}" --data-binary @{file} "{presigned_url}"\n'
            '  Step 3: 確認上傳後，用 bouncer_execute s3 cp 從 staging 搬到目標 bucket（需審批）\n\n'
            '適用情境：單一大檔案（> 500KB）直傳。多檔請用 bouncer_request_presigned_batch。\n'
            '上傳目標固定為 staging bucket（bouncer-uploads-{DEFAULT_ACCOUNT_ID}），s3_key 格式：{date}/{request_id}/{filename}。'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'filename': {
                    'type': 'string',
                    'description': '目標檔名（含路徑，例如 assets/pdf.worker.min.mjs）',
                },
                'content_type': {
                    'type': 'string',
                    'description': 'MIME type（例如 application/javascript）',
                },
                'reason': {
                    'type': 'string',
                    'description': '上傳原因',
                },
                'source': {
                    'type': 'string',
                    'description': '請求來源標識（例如 Private Bot (deploy)）',
                },
                'account': {
                    'type': 'string',
                    'description': '目標帳號 ID（預設 DEFAULT_ACCOUNT_ID）',
                },
                'expires_in': {
                    'type': 'integer',
                    'description': 'presigned URL 有效期秒數（預設 900，最大 3600）',
                    'default': 900,
                },
            },
            'required': ['filename', 'content_type', 'reason', 'source'],
        },
    },
    # ========== Presigned Batch Upload Tool ==========
    'bouncer_request_presigned_batch': {
        'description': (
            '一次呼叫，為多個檔案（最多 50 個）批量生成 presigned S3 PUT URL。前端部署推薦用法。\n'
            '不需要審批。所有檔案共用同一 batch_id prefix，s3_key 格式：{date}/{batch_id}/{filename}。\n\n'
            '使用流程：\n'
            '  Step 1: 呼叫此工具，傳入 [{filename, content_type}, ...]\n'
            '  Step 2: 對每個回傳的 presigned_url 執行 PUT（可並行）\n'
            '          curl -X PUT -H "Content-Type: {type}" --data-binary @{file} "{url}"\n'
            '  Step 3: 全部 PUT 完成後，用 bouncer_execute s3 cp 從 staging 搬到目標 bucket（需審批）\n'
            '          aws s3 cp s3://staging/{batch_id}/ s3://target-bucket/ --recursive\n'
            '  Step 4: 如需 CloudFront，用 bouncer_execute cloudfront create-invalidation（需審批）\n\n'
            '與 bouncer_upload_batch 的差異：本工具不經 Lambda，無 500KB 限制，支援任意大小檔案（推薦）。\n'
            'bouncer_upload_batch 已 deprecated，新專案請改用此工具。'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'files': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'filename': {
                                'type': 'string',
                                'description': '目標檔名（含相對路徑，例如 assets/foo.js）',
                            },
                            'content_type': {
                                'type': 'string',
                                'description': 'MIME type（例如 application/javascript）',
                            },
                        },
                        'required': ['filename', 'content_type'],
                    },
                    'description': '要上傳的檔案列表，最多 50 個',
                    'maxItems': 50,
                },
                'reason': {
                    'type': 'string',
                    'description': '上傳原因',
                },
                'source': {
                    'type': 'string',
                    'description': '請求來源標識（例如 Private Bot (deploy)）',
                },
                'account': {
                    'type': 'string',
                    'description': '目標帳號 ID（預設 DEFAULT_ACCOUNT_ID）',
                },
                'expires_in': {
                    'type': 'integer',
                    'description': 'presigned URL 有效期秒數（預設 900，min 60，max 3600）',
                    'default': 900,
                    'minimum': 60,
                    'maximum': 3600,
                },
            },
            'required': ['files', 'reason'],
        },
    },
    # ========== History / Stats Tools ==========
    'bouncer_history': {
        'description': '查詢 Bouncer 請求歷史記錄，支援 source/action/status/account_id 過濾及分頁。action=execute 時同時查詢 command-history table（如存在）。',
        'parameters': {
            'type': 'object',
            'properties': {
                'limit': {
                    'type': 'integer',
                    'description': '每頁筆數（預設 20，最大 50）',
                    'default': 20,
                    'minimum': 1,
                    'maximum': 50,
                },
                'source': {
                    'type': 'string',
                    'description': '過濾來源識別符（例如：Private Bot (Bouncer)）',
                },
                'action': {
                    'type': 'string',
                    'description': '過濾動作類型：execute / upload / upload_batch / deploy / presigned_upload 等',
                },
                'status': {
                    'type': 'string',
                    'description': '過濾狀態：approved / denied / error / pending_approval 等',
                },
                'account_id': {
                    'type': 'string',
                    'description': '過濾目標 AWS 帳號 ID',
                },
                'since_hours': {
                    'type': 'integer',
                    'description': '查詢最近幾小時的記錄（預設 24）',
                    'default': 24,
                },
                'page_token': {
                    'type': 'string',
                    'description': '分頁 token（從上一次回應的 next_page_token 取得）',
                },
            },
        },
    },
    'bouncer_stats': {
        'description': '查詢最近 24 小時的 Bouncer 請求統計：各狀態數量（approved/denied/pending）及各 source 的請求數。',
        'parameters': {
            'type': 'object',
            'properties': {},
        },
    },
    # ========== Confirm Upload Tool ==========
    'bouncer_confirm_upload': {
        'description': (
            '驗證透過 presigned URL 上傳的檔案是否確實存在於 staging bucket。\n'
            '使用 list_objects_v2 一次批量檢查整個 batch，回傳每個檔案的驗證結果。\n'
            '驗證結果會寫入 DynamoDB（TTL 7 天），可用 bouncer_status 查詢歷史。\n\n'
            '使用流程（接在 bouncer_request_presigned_batch 之後）：\n'
            '  Step 1: 呼叫 bouncer_request_presigned_batch 取得 batch_id + presigned URLs\n'
            '  Step 2: 對每個 URL 執行 PUT 上傳\n'
            '  Step 3: 呼叫 bouncer_confirm_upload 確認所有檔案都上傳成功\n'
            '  Step 4: verified=true 後，再執行 bouncer_execute s3 cp 搬到目標 bucket\n\n'
            '一次最多 50 個檔案。'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'batch_id': {
                    'type': 'string',
                    'description': 'Batch ID（從 bouncer_request_presigned_batch 回傳的 batch_id）',
                },
                'files': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            's3_key': {
                                'type': 'string',
                                'description': 'S3 key（從 bouncer_request_presigned_batch 回傳的 s3_key）',
                            },
                        },
                        'required': ['s3_key'],
                    },
                    'description': '要驗證的檔案列表，每個項目包含 s3_key',
                    'maxItems': 50,
                },
            },
            'required': ['batch_id', 'files'],
        },
    },
    'bouncer_upload_batch': {
        'description': '批量上傳多個檔案到 S3，一次審批。如果有活躍的 Trust Session，可自動上傳。',
        'parameters': {
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
                    'description': '目標 AWS 帳號 ID（預設使用 Bouncer 所在帳號）'
                }
            },
            'required': ['files', 'reason', 'source']
        }
    }
}
