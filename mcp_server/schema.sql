-- Bouncer MCP Server - SQLite Schema
-- 版本: 1.0.0

-- 審批請求表
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    reason TEXT DEFAULT 'No reason provided',
    status TEXT NOT NULL DEFAULT 'pending',  -- pending, approved, denied, blocked, timeout
    classification TEXT,  -- BLOCKED, SAFELIST, APPROVAL
    
    -- 執行結果
    result TEXT,
    exit_code INTEGER,
    
    -- 審批資訊
    telegram_message_id INTEGER,
    approved_by TEXT,
    approved_at INTEGER,
    
    -- 時間戳
    created_at INTEGER NOT NULL,
    updated_at INTEGER,
    expires_at INTEGER,
    
    -- 索引用
    mode TEXT DEFAULT 'mcp'  -- mcp, rest (保留向後兼容)
);

-- 索引：按狀態查詢 pending 請求
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);

-- 索引：按創建時間排序
CREATE INDEX IF NOT EXISTS idx_requests_created ON requests(created_at DESC);

-- 審計日誌表（可選，記錄所有操作）
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT,
    action TEXT NOT NULL,  -- created, approved, denied, executed, timeout
    actor TEXT,            -- system, telegram_user_id
    details TEXT,          -- JSON 格式的額外資訊
    created_at INTEGER NOT NULL,
    
    FOREIGN KEY (request_id) REFERENCES requests(request_id)
);

CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_log(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
