"""
Bouncer MCP Server - Database Layer
SQLite 狀態存儲
"""

import sqlite3
import time
import json
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List
from contextlib import contextmanager

# 預設資料庫路徑
DEFAULT_DB_PATH = Path(__file__).parent / "bouncer.db"


class Database:
    """Thread-safe SQLite database wrapper"""
    
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._local = threading.local()
        self._init_schema()
    
    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local connection"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False
            )
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn
    
    @contextmanager
    def _cursor(self):
        """Context manager for cursor with auto-commit"""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
    
    def _init_schema(self):
        """Initialize database schema"""
        schema_path = Path(__file__).parent / "schema.sql"
        if schema_path.exists():
            with self._cursor() as cursor:
                cursor.executescript(schema_path.read_text())
    
    # =========================================================================
    # Request CRUD
    # =========================================================================
    
    def create_request(
        self,
        request_id: str,
        command: str,
        reason: str = "No reason provided",
        classification: str = "APPROVAL",
        expires_in: int = 300
    ) -> Dict[str, Any]:
        """Create a new approval request"""
        now = int(time.time())
        
        with self._cursor() as cursor:
            cursor.execute("""
                INSERT INTO requests 
                (request_id, command, reason, status, classification, created_at, expires_at)
                VALUES (?, ?, ?, 'pending', ?, ?, ?)
            """, (request_id, command, reason, classification, now, now + expires_in))
            
            # Log creation
            self._log_action(cursor, request_id, 'created', 'system', {
                'command': command,
                'reason': reason,
                'classification': classification
            })
        
        return self.get_request(request_id)
    
    def get_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Get request by ID"""
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM requests WHERE request_id = ?",
                (request_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def update_request(
        self,
        request_id: str,
        status: Optional[str] = None,
        result: Optional[str] = None,
        exit_code: Optional[int] = None,
        telegram_message_id: Optional[int] = None,
        approved_by: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Update request fields"""
        updates = []
        values = []
        
        if status is not None:
            updates.append("status = ?")
            values.append(status)
        if result is not None:
            updates.append("result = ?")
            values.append(result[:10000])  # Limit result size
        if exit_code is not None:
            updates.append("exit_code = ?")
            values.append(exit_code)
        if telegram_message_id is not None:
            updates.append("telegram_message_id = ?")
            values.append(telegram_message_id)
        if approved_by is not None:
            updates.append("approved_by = ?")
            values.append(approved_by)
            updates.append("approved_at = ?")
            values.append(int(time.time()))
        
        if not updates:
            return self.get_request(request_id)
        
        updates.append("updated_at = ?")
        values.append(int(time.time()))
        values.append(request_id)
        
        with self._cursor() as cursor:
            cursor.execute(
                f"UPDATE requests SET {', '.join(updates)} WHERE request_id = ?",
                values
            )
        
        return self.get_request(request_id)
    
    def get_pending_requests(self) -> List[Dict[str, Any]]:
        """Get all pending requests"""
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM requests WHERE status = 'pending' ORDER BY created_at"
            )
            return [dict(row) for row in cursor.fetchall()]
    
    def get_request_by_message_id(self, message_id: int) -> Optional[Dict[str, Any]]:
        """Get request by Telegram message ID"""
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM requests WHERE telegram_message_id = ?",
                (message_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def expire_old_requests(self) -> int:
        """Mark expired requests as timeout, return count"""
        now = int(time.time())
        
        with self._cursor() as cursor:
            cursor.execute("""
                UPDATE requests 
                SET status = 'timeout', updated_at = ?
                WHERE status = 'pending' AND expires_at < ?
            """, (now, now))
            return cursor.rowcount
    
    # =========================================================================
    # Audit Log
    # =========================================================================
    
    def _log_action(
        self,
        cursor: sqlite3.Cursor,
        request_id: str,
        action: str,
        actor: str,
        details: Optional[Dict] = None
    ):
        """Log an action to audit table"""
        cursor.execute("""
            INSERT INTO audit_log (request_id, action, actor, details, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            request_id,
            action,
            actor,
            json.dumps(details) if details else None,
            int(time.time())
        ))
    
    def log_action(
        self,
        request_id: str,
        action: str,
        actor: str = "system",
        details: Optional[Dict] = None
    ):
        """Public method to log an action"""
        with self._cursor() as cursor:
            self._log_action(cursor, request_id, action, actor, details)
    
    def get_audit_log(
        self,
        request_id: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get audit log entries"""
        with self._cursor() as cursor:
            if request_id:
                cursor.execute(
                    "SELECT * FROM audit_log WHERE request_id = ? ORDER BY created_at DESC LIMIT ?",
                    (request_id, limit)
                )
            else:
                cursor.execute(
                    "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                )
            return [dict(row) for row in cursor.fetchall()]
    
    # =========================================================================
    # Stats
    # =========================================================================
    
    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics"""
        with self._cursor() as cursor:
            cursor.execute("""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) as approved,
                    SUM(CASE WHEN status = 'denied' THEN 1 ELSE 0 END) as denied,
                    SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) as blocked,
                    SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeout
                FROM requests
            """)
            row = cursor.fetchone()
            return dict(row) if row else {}


# Singleton instance
_db: Optional[Database] = None


def get_db(db_path: Optional[Path] = None) -> Database:
    """Get or create database singleton"""
    global _db
    if _db is None:
        _db = Database(db_path or DEFAULT_DB_PATH)
    return _db


def reset_db():
    """Reset database singleton (for testing)"""
    global _db
    _db = None
