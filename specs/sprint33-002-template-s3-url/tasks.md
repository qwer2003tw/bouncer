# Tasks — Sprint 33-002: sam package explicit + DDB template_s3_url + IAM

> GitHub Issue: #120
> Covers: sprint33-002 (sam_deploy.py) + sprint33-003 (IAM)

## TCS Scoring Guide
D1 Files (0-5) + D2 Cross-module (0-5) + D3 Testing (0-5) + D4 Infra (0-5) + D5 External (0-5)
Simple: 0-6 | Medium: 7-12 | Complex: 13+

---

## Tasks

### [T001] [P0] sam_deploy.py: 實作 _run_sam_package()
新增函數，執行 `sam package --template-file template.yaml --output-template-file /tmp/{stack}-packaged.yaml --s3-bucket {ARTIFACTS_BUCKET} --s3-prefix templates`；失敗時 sys.exit(non-zero)

| TCS = 5 (Simple) |
| D1=1 D2=0 D3=2（mock subprocess，success/failure path）D4=0 D5=2（sam package CLI args） |
| Deliverable: `_run_sam_package(stack, bucket, region) → str（packaged template path）` |

---

### [T002] [P0] sam_deploy.py: 修改 _build_sam_cmd() 支援 --template-file / fallback
移除 `--resolve-s3`，改為：有 `template_file` 參數時用 `--template-file`，否則 fallback `--resolve-s3`

| TCS = 4 (Simple) |
| D1=1 D2=0 D3=2（兩個 path 的 assert）D4=0 D5=1（sam deploy CLI args 差異） |
| Deliverable: `_build_sam_cmd(…, template_file=None)` — packaged template 或 fallback |

---

### [T003] [P0] sam_deploy.py: 實作 _update_template_s3_url()
Best-effort DDB update：使用 botocore session（不帶 explicit credentials，避免 cross-account 污染）更新 ProjectsTable.template_s3_url；失敗只 log，不 exit

| TCS = 6 (Simple) |
| D1=1 D2=0 D3=3（success/DDB error/project_id empty 三個路徑）D4=0 D5=2（botocore session + DDB UpdateItem API） |
| Deliverable: `_update_template_s3_url(stack, project_id, bucket, region)` |

---

### [T004] [P0] sam_deploy.py: 修改 main() 整合 package + DDB update
讀取 `ARTIFACTS_BUCKET` + `PROJECT_ID` env vars；package 邏輯；成功後呼叫 DDB update；retry 成功後也呼叫

| TCS = 7 (Medium) |
| D1=1 D2=1（整合 T001/T002/T003）D3=3（多路徑整合測試）D4=0 D5=2（整體流程） |
| Notes: retry path（import + redeploy）成功後也需觸發 DDB update |
| Deliverable: main() 整合完整，所有路徑測試通過 |

---

### [T005] [P0] template.yaml: CodeBuildRole 新增 DDB UpdateItem（sprint33-003）
在 `CodeBuildRole` policy 新增 `Sid: DDBProjectsTable`，允許 `dynamodb:UpdateItem` on `!GetAtt ProjectsTable.Arn`

| TCS = 3 (Simple) |
| D1=1（template.yaml）D2=0 D3=0（IAM 變更無單元測試）D4=2（需 deployer stack redeploy）D5=0 |
| Deliverable: template.yaml 修改，YAML syntax 正確，可通過 cfn-lint |

---

### [T006] [P0] template.yaml: buildspec 傳遞 PROJECT_ID 給 CodeBuild
在 Step Function `StartBuild` EnvironmentVariablesOverride 新增 `PROJECT_ID` 變數（`Value.$: $.project_id`）

| TCS = 3 (Simple) |
| D1=1（template.yaml）D2=0 D3=0 D4=1（需 deployer stack redeploy）D5=0 |
| Deliverable: template.yaml buildspec 包含 PROJECT_ID |

---

### [T007] [P1] 新增測試 test_sprint33_002_sam_package_ddb.py
覆蓋所有 T001-T004 場景：package 成功/失敗、deploy with template-file/fallback、DDB update success/error/skip

| TCS = 6 (Simple) |
| D1=1（新測試檔）D2=0 D3=4（6 個測試案例）D4=0 D5=1（mock boto3 + subprocess） |
| Deliverable: pytest 全過 |

---

## Summary

| Task | TCS | Category |
|------|-----|----------|
| T001 | 5 | Simple |
| T002 | 4 | Simple |
| T003 | 6 | Simple |
| T004 | 7 | Medium |
| T005 | 3 | Simple |
| T006 | 3 | Simple |
| T007 | 6 | Simple |
| **Total** | **34** | Mixed |

**模型建議**：Sonnet（有完整 spec，照做即可）
**注意**：T005/T006 需要 deployer stack redeploy；T001-T004 + T007 可獨立測試後上傳 sam_deploy.py script

### ⚠️ 重要：部署順序
1. 先部署 deployer stack（T005/T006 IAM + buildspec 生效）
2. 再上傳 sam_deploy.py script (`make -C deployer upload-deploy-script`)
3. 測試新 deploy 流程
