"""Upload file scanner for security screening (#smart-phase5)."""
import re
import os
from dataclasses import dataclass, field
from typing import List, Tuple
from aws_lambda_powertools import Logger
from metrics import emit_metric

logger = Logger(service="bouncer")

BLOCKED_EXTENSIONS = {
    '.exe', '.sh', '.bat', '.cmd', '.ps1', '.vbs',
    '.dll', '.so', '.dylib', '.bin', '.msi', '.deb', '.rpm',
}

# Only scan text-based content types
SCANNABLE_CONTENT_TYPES = {
    'text/', 'application/json', 'application/yaml', 'application/x-yaml',
    'application/xml', 'application/javascript', 'application/typescript',
}

SECRET_PATTERNS: List[Tuple[str, str]] = [
    (r'(?i)aws_secret_access_key\s*[=:]\s*[A-Za-z0-9+/]{40}', "AWS Secret Access Key"),
    (r'AKIA[A-Z0-9]{16}', "AWS Access Key ID"),
    (r'ghp_[A-Za-z0-9]{36}', "GitHub PAT"),
    (r'(?i)(password|passwd|secret|api_key|token)\s*[=:]\s*["\'][^"\']{8,}["\']', "Hardcoded credential"),
    (r'-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----', "Private key"),
]

MAX_SCAN_SIZE = 1_000_000  # 1MB — don't scan files larger than this


@dataclass
class UploadScanResult:
    is_blocked: bool = False         # True = reject immediately
    risk_level: str = 'safe'         # 'blocked' / 'high' / 'medium' / 'safe' / 'error'
    findings: List[str] = field(default_factory=list)
    summary: str = ''


def scan_upload(filename: str, content_bytes: bytes, content_type: str = '') -> UploadScanResult:
    """Scan uploaded file for security risks.

    Returns UploadScanResult. Never raises — on exception returns error (fail-closed, s60-001).
    """
    try:
        ext = os.path.splitext(filename.lower())[1]

        # Check blocked extensions
        if ext in BLOCKED_EXTENSIONS:
            return UploadScanResult(
                is_blocked=True,
                risk_level='blocked',
                findings=[f"Blocked file type: {ext}"],
                summary=f"파일 유형 {ext}은(는) 허용되지 않습니다 / 不允許的檔案類型：{ext}",
            )

        # Check if scannable
        # Handle files like .env, .gitignore that have no extension
        basename = os.path.basename(filename.lower())
        is_text = (
            content_type.startswith('text/') or
            any(ct in content_type for ct in SCANNABLE_CONTENT_TYPES) or
            ext in {'.yaml', '.yml', '.json', '.txt', '.csv', '.xml', '.js', '.ts', '.py', '.env', '.conf', '.cfg', '.ini'} or
            basename in {'.env', '.gitignore', '.dockerignore', '.npmrc', '.pypirc'}
        )

        if not is_text or len(content_bytes) > MAX_SCAN_SIZE:
            return UploadScanResult(risk_level='safe', summary='')

        # Decode text content
        try:
            text = content_bytes.decode('utf-8', errors='replace')
        except Exception:
            return UploadScanResult(risk_level='safe', summary='')

        # Scan for secret patterns
        findings = []
        for pattern, description in SECRET_PATTERNS:
            if re.search(pattern, text):
                findings.append(description)

        if findings:
            return UploadScanResult(
                is_blocked=False,
                risk_level='high',
                findings=findings,
                summary=f"偵測到敏感資訊：{', '.join(findings)}",
            )

        return UploadScanResult(risk_level='safe', summary='')

    except Exception as exc:  # noqa: BLE001
        # Fail-closed: scanner error requires human review (s60-001)
        error_type = type(exc).__name__
        error_msg = str(exc)[:200]
        logger.error("upload_scanner: unexpected error during scan", extra={
            "src_module": "upload_scanner",
            "operation": "scan_upload",
            "filename": filename,
            "error_type": error_type,
            "error_message": error_msg,
        })
        emit_metric('Bouncer', 'ScannerError', 1)
        return UploadScanResult(
            risk_level='error',
            is_blocked=False,
            summary=f'Scanner error ({error_type}): file requires manual review',
        )
