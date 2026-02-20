#!/bin/bash
set -euo pipefail

# =============================================================================
# Bouncer Repo Cleanup Script
# 目標：清除所有硬編碼的敏感值，準備 repo 公開
# =============================================================================

REPO_DIR="/tmp/bouncer-cleanup"
BACKUP_DIR="/tmp/bouncer-backup-$(date +%Y%m%d-%H%M%S)"
REPLACEMENTS_FILE="${REPO_DIR}/replacements.txt"

echo "============================================"
echo "Bouncer Repo Cleanup"
echo "============================================"

# =============================================================================
# Step 1: 備份
# =============================================================================
echo ""
echo "[Step 1] 備份原始 repo..."
cp -r "${REPO_DIR}" "${BACKUP_DIR}"
echo "  ✅ 備份到: ${BACKUP_DIR}"

# =============================================================================
# Step 2: 安裝 git-filter-repo
# =============================================================================
echo ""
echo "[Step 2] 檢查/安裝 git-filter-repo..."
if ! command -v git-filter-repo &> /dev/null; then
    pip3 install git-filter-repo
    echo "  ✅ git-filter-repo 已安裝"
else
    echo "  ✅ git-filter-repo 已存在"
fi

# =============================================================================
# Step 3: 執行 git-filter-repo 清洗歷史
# =============================================================================
echo ""
echo "[Step 3] 執行 git-filter-repo 清洗 git 歷史..."
cd "${REPO_DIR}"

# git-filter-repo 需要 fresh clone 或 --force
git-filter-repo --replace-text "${REPLACEMENTS_FILE}" --force

echo "  ✅ Git 歷史文字已清洗"

echo "  刪除 .pyc 檔案歷史..."
git-filter-repo --invert-paths --path-glob '*.pyc' --path-glob '__pycache__/*' --force

echo "  ✅ .pyc 檔案已從歷史中清除"

# =============================================================================
# Step 4: 修改目前程式碼中的硬編碼值
# =============================================================================
echo ""
echo "[Step 4] 修改程式碼中的硬編碼 fallback..."

# --- src/constants.py ---
# 將 fallback 值改為空字串
sed -i "s|os.environ.get('APPROVED_CHAT_ID', '999999999')|os.environ.get('APPROVED_CHAT_ID', '')|g" src/constants.py
sed -i "s|os.environ.get('DEFAULT_ACCOUNT_ID', '111111111111')|os.environ.get('DEFAULT_ACCOUNT_ID', '')|g" src/constants.py

# --- src/app.py ---
# UPLOAD_BUCKET: 改為環境變數
sed -i "s|UPLOAD_BUCKET = 'bouncer-uploads-111111111111'|UPLOAD_BUCKET = os.environ.get('UPLOAD_BUCKET', 'bouncer-uploads')|g" src/app.py
sed -i "s|os.environ.get('AWS_ACCOUNT_ID', '111111111111')|os.environ.get('AWS_ACCOUNT_ID', '')|g" src/app.py

# --- src/mcp_tools.py ---
sed -i "s|UPLOAD_BUCKET = 'bouncer-uploads-111111111111'|UPLOAD_BUCKET = os.environ.get('UPLOAD_BUCKET', 'bouncer-uploads')|g" src/mcp_tools.py

# --- bouncer_mcp.py ---
sed -i "s|os.environ.get('BOUNCER_API_URL', 'https://YOUR_API_GATEWAY_URL')|os.environ.get('BOUNCER_API_URL', '')|g" bouncer_mcp.py

# --- src/compliance_checker.py ---
# 改為從環境變數讀取 trusted account IDs
cat > /tmp/compliance_patch.py << 'PYEOF'
import re

with open("src/compliance_checker.py", "r") as f:
    content = f.read()

# After git-filter-repo, the account IDs are now 111111111111, 222222222222, 333333333333
content = content.replace(
    r'r"iam\s+(update-assume-role-policy|create-role).*arn:aws:iam::(?!111111111111|222222222222|333333333333)\d{12}:"',
    'r"iam\\s+(update-assume-role-policy|create-role).*arn:aws:iam::(?!" + "|".join(TRUSTED_ACCOUNT_IDS) + r")\\d{12}:"'
)

content = content.replace(
    '"只能信任組織內帳號 (111111111111, 222222222222, 333333333333)"',
    '"只能信任組織內帳號 (" + ", ".join(TRUSTED_ACCOUNT_IDS) + ")"'
)

# Add TRUSTED_ACCOUNT_IDS constant near the top (after imports)
if "TRUSTED_ACCOUNT_IDS" not in content:
    # Find the first class or function definition as anchor point
    import_section_end = 0
    for i, line in enumerate(content.split('\n')):
        if line.startswith('import ') or line.startswith('from '):
            import_section_end = i
    
    lines = content.split('\n')
    insert_idx = import_section_end + 1
    
    trust_lines = [
        '',
        '# 受信任的組織內 AWS 帳號 ID（用於合規檢查）',
        "TRUSTED_ACCOUNT_IDS = os.environ.get('TRUSTED_ACCOUNT_IDS', '').split(',') if os.environ.get('TRUSTED_ACCOUNT_IDS') else []",
        '',
    ]
    
    # Also need os import
    if "import os" not in content:
        trust_lines = ['import os'] + trust_lines
    
    lines = lines[:insert_idx] + trust_lines + lines[insert_idx:]
    content = '\n'.join(lines)

with open("src/compliance_checker.py", "w") as f:
    f.write(content)

print("  compliance_checker.py patched successfully")
PYEOF
python3 /tmp/compliance_patch.py

# --- data/risk-rules.json ---
# Account sensitivity 值已被 git-filter-repo 替換為 placeholder IDs
# 但這是期望行為，公開 repo 使用 placeholder 帳號即可

# --- template.yaml ---
# SAM template: 將 Default 改為空字串，ARN 改用 !Sub
sed -i 's|Default: "999999999"|Default: ""|g' template.yaml
sed -i 's|DEFAULT_ACCOUNT_ID: '\''111111111111'\''|DEFAULT_ACCOUNT_ID: !Ref DefaultAccountId|g' template.yaml
# State Machine ARN - 用 !Sub 取代硬編碼
sed -i "s|DEPLOY_STATE_MACHINE_ARN: arn:aws:states:us-east-1:111111111111:stateMachine:sam-deployer-workflow|DEPLOY_STATE_MACHINE_ARN: !Sub 'arn:aws:states:\${AWS::Region}:\${AWS::AccountId}:stateMachine:sam-deployer-workflow'|g" template.yaml
sed -i "s|Resource: arn:aws:states:us-east-1:111111111111:stateMachine:sam-deployer-workflow|Resource: !Sub 'arn:aws:states:\${AWS::Region}:\${AWS::AccountId}:stateMachine:sam-deployer-workflow'|g" template.yaml

# --- deployer/template.yaml ---
sed -i 's|Default: "999999999"|Default: ""|g' deployer/template.yaml

# --- .github/workflows/ci.yaml ---
# CI 中的 APPROVED_CHAT_ID 可以用 test placeholder
sed -i "s|APPROVED_CHAT_ID: '999999999'|APPROVED_CHAT_ID: '123456789'|g" .github/workflows/ci.yaml

# --- tests/test_bouncer.py ---
# 測試中的值已被 git-filter-repo 替換，再統一改為 test-friendly constants
# 測試檔案中使用 placeholder 是 OK 的，不需要環境變數

echo "  ✅ 程式碼硬編碼值已修改"

# =============================================================================
# Step 5: 更新 .gitignore
# =============================================================================
echo ""
echo "[Step 5] 更新 .gitignore..."

# 檢查並添加缺少的項目
for pattern in ".env" ".env.*"; do
    if ! grep -qF "${pattern}" .gitignore; then
        echo "${pattern}" >> .gitignore
        echo "  + 已添加: ${pattern}"
    fi
done

echo "  ✅ .gitignore 已更新"

# =============================================================================
# Step 6: 新增 .env.example
# =============================================================================
echo ""
echo "[Step 6] 建立 .env.example..."

cat > .env.example << 'EOF'
# Bouncer 環境變數
# 複製此檔案為 .env 並填入實際值

# === Telegram ===
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
APPROVED_CHAT_ID=your-telegram-chat-id
TELEGRAM_WEBHOOK_SECRET=your-webhook-secret

# === AWS ===
DEFAULT_ACCOUNT_ID=your-aws-account-id
AWS_ACCOUNT_ID=your-aws-account-id

# === Security ===
REQUEST_SECRET=your-request-secret
ENABLE_HMAC=false

# === S3 ===
UPLOAD_BUCKET=bouncer-uploads-your-account-id

# === API Gateway ===
BOUNCER_API_URL=https://your-api-id.execute-api.us-east-1.amazonaws.com/prod

# === Compliance ===
TRUSTED_ACCOUNT_IDS=account-id-1,account-id-2,account-id-3

# === DynamoDB (自動由 SAM 設定) ===
# TABLE_NAME=clawdbot-approval-requests
# ACCOUNTS_TABLE_NAME=bouncer-accounts
EOF

echo "  ✅ .env.example 已建立"

# =============================================================================
# Step 7: Commit 修改
# =============================================================================
echo ""
echo "[Step 7] Commit 程式碼修改..."

git add -A
git commit -m "chore: remove hardcoded sensitive values for public release

- Replace hardcoded Telegram Chat ID with env var (no fallback)
- Replace hardcoded AWS Account IDs with env vars
- Replace hardcoded API Gateway URL with env var
- Replace hardcoded S3 bucket name with env var
- Use !Sub for SAM template ARNs
- Add TRUSTED_ACCOUNT_IDS env var for compliance checker
- Add .env.example for configuration reference
- Update .gitignore with .env patterns"

echo "  ✅ 已 commit"

# =============================================================================
# Step 8: 驗證 - 確認無殘留
# =============================================================================
echo ""
echo "[Step 8] 驗證：搜尋殘留敏感值..."

FOUND=0

echo "  檢查目前檔案..."
for pattern in "999999999" "111111111111" "222222222222" "333333333333" "YOUR_API_ID"; do
    hits=$(grep -rn "${pattern}" . --include='*.py' --include='*.yaml' --include='*.yml' --include='*.json' --include='*.md' --include='*.txt' --include='*.sh' --include='*.cfg' | grep -v '.git/' | grep -v 'cleanup.sh' | grep -v 'replacements.txt' | grep -v '.env.example' || true)
    if [ -n "${hits}" ]; then
        echo "  ❌ 發現殘留: ${pattern}"
        echo "${hits}" | head -10
        FOUND=1
    else
        echo "  ✅ ${pattern} - 無殘留"
    fi
done

echo ""
echo "  檢查 git 歷史..."
for pattern in "999999999" "111111111111" "222222222222" "333333333333" "YOUR_API_ID"; do
    hits=$(git log -p --all -S "${pattern}" --pretty=format:"%H" 2>/dev/null | head -1 || true)
    if [ -n "${hits}" ]; then
        echo "  ❌ Git 歷史中發現: ${pattern}"
        FOUND=1
    else
        echo "  ✅ ${pattern} - Git 歷史乾淨"
    fi
done

echo ""
echo "  檢查 .pyc 檔案..."
pyc_hits=$(git log --all --name-only --pretty=format:"" -- '*.pyc' 2>/dev/null | sort -u | grep -v '^$' || true)
if [ -n "${pyc_hits}" ]; then
    echo "  ❌ Git 歷史中仍有 .pyc 檔案:"
    echo "${pyc_hits}"
    FOUND=1
else
    echo "  ✅ 無 .pyc 檔案殘留"
fi

echo ""
if [ "${FOUND}" -eq 0 ]; then
    echo "============================================"
    echo "✅ 所有檢查通過！Repo 已準備好公開。"
    echo "============================================"
else
    echo "============================================"
    echo "⚠️  部分檢查未通過，請手動確認上述項目。"
    echo "============================================"
fi

# =============================================================================
# Step 9: Force Push 指引
# =============================================================================
echo ""
echo "============================================"
echo "下一步：Force Push 到 GitHub"
echo "============================================"
echo ""
echo "1. 重新設定 remote（git-filter-repo 會移除 origin）："
echo "   git remote add origin https://github.com/qwer2003tw/bouncer.git"
echo ""
echo "2. Force push 所有分支："
echo "   git push --force --all origin"
echo "   git push --force --tags origin"
echo ""
echo "3. GitHub 操作："
echo "   a. Settings → Actions → General → 清除 Actions cache"
echo "   b. Settings → Danger Zone → Visibility → Make public"
echo ""
echo "4. 通知所有 collaborator 重新 clone（不要 pull）"
echo ""
echo "5. 等 24 小時後確認 GitHub 的 loose objects 已 GC"
echo ""
