"""
Security Configuration and Utilities

Infrastructure security settings including CORS, CSP, cookies, and HTTPS.
Also includes log sanitization and secret detection.
"""
import re
import logging
from typing import Any
from functools import lru_cache

from django.conf import settings

logger = logging.getLogger(__name__)


# ======================== Secret Patterns ========================

SECRET_PATTERNS = [
    # API Keys
    (r'(?i)(api[_-]?key|apikey)["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_-]{20,})', 'API_KEY'),
    (r'(?i)(secret[_-]?key|secretkey)["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_-]{20,})', 'SECRET_KEY'),
    
    # Tokens
    (r'(?i)(bearer|token)["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_.-]{20,})', 'TOKEN'),
    (r'(?i)(access[_-]?token)["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_.-]{20,})', 'ACCESS_TOKEN'),
    (r'(?i)(refresh[_-]?token)["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_.-]{20,})', 'REFRESH_TOKEN'),
    
    # Passwords
    (r'(?i)(password|passwd|pwd)["\']?\s*[:=]\s*["\']?([^\s"\']{8,})', 'PASSWORD'),
    
    # AWS
    (r'AKIA[0-9A-Z]{16}', 'AWS_ACCESS_KEY'),
    (r'(?i)(aws[_-]?secret)["\']?\s*[:=]\s*["\']?([a-zA-Z0-9/+=]{40})', 'AWS_SECRET'),
    
    # OAuth
    (r'(?i)(client[_-]?secret)["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_-]{20,})', 'CLIENT_SECRET'),
    
    # Database URLs
    (r'(?i)(postgres|mysql|mongodb)://[^:]+:([^@]+)@', 'DATABASE_PASSWORD'),
    
    # Private Keys
    (r'-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----', 'PRIVATE_KEY'),
    
    # Credit Cards
    (r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13})\b', 'CREDIT_CARD'),
    
    # SSN
    (r'\b\d{3}-\d{2}-\d{4}\b', 'SSN'),
]

# PII patterns
PII_PATTERNS = [
    (r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', 'EMAIL'),
    (r'\b\d{10}\b|\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b', 'PHONE'),
    (r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', 'IP_ADDRESS'),
]


class LogSanitizer:
    """
    Sanitizes logs by stripping PII and secrets.
    
    Usage:
        sanitizer = LogSanitizer()
        clean_log = sanitizer.sanitize("API key is sk-abc123...")
    """
    
    def __init__(self, mask: str = "***REDACTED***"):
        self.mask = mask
        self._secret_patterns = [
            (re.compile(pattern), name) for pattern, name in SECRET_PATTERNS
        ]
        self._pii_patterns = [
            (re.compile(pattern), name) for pattern, name in PII_PATTERNS
        ]
    
    def sanitize(self, text: str, redact_pii: bool = True) -> str:
        """
        Sanitize text by removing secrets and optionally PII.
        
        Args:
            text: Text to sanitize
            redact_pii: Whether to also redact PII
            
        Returns:
            Sanitized text
        """
        if not text:
            return text
        
        result = text
        
        # Remove secrets
        for pattern, name in self._secret_patterns:
            result = pattern.sub(f"[{name}:{self.mask}]", result)
        
        # Remove PII if requested
        if redact_pii:
            for pattern, name in self._pii_patterns:
                result = pattern.sub(f"[{name}:{self.mask}]", result)
        
        return result
    
    def sanitize_dict(self, data: dict, redact_pii: bool = True) -> dict:
        """Recursively sanitize a dictionary."""
        if not isinstance(data, dict):
            return data
        
        result = {}
        sensitive_keys = {
            'password', 'secret', 'token', 'api_key', 'apikey', 'auth',
            'credential', 'private', 'key', 'access_token', 'refresh_token'
        }
        
        for key, value in data.items():
            key_lower = key.lower()
            
            # Fully redact sensitive keys
            if any(sk in key_lower for sk in sensitive_keys):
                result[key] = self.mask
            elif isinstance(value, dict):
                result[key] = self.sanitize_dict(value, redact_pii)
            elif isinstance(value, list):
                result[key] = [
                    self.sanitize_dict(item, redact_pii) if isinstance(item, dict)
                    else self.sanitize(str(item), redact_pii) if isinstance(item, str)
                    else item
                    for item in value
                ]
            elif isinstance(value, str):
                result[key] = self.sanitize(value, redact_pii)
            else:
                result[key] = value
        
        return result


# ======================== Security Headers ========================

def get_security_headers() -> dict[str, str]:
    """
    Get recommended security headers for responses.
    
    Returns:
        Dictionary of header name -> value
    """
    return {
        # Prevent MIME type sniffing
        'X-Content-Type-Options': 'nosniff',
        
        # XSS Protection
        'X-XSS-Protection': '1; mode=block',
        
        # Clickjacking protection
        'X-Frame-Options': 'DENY',
        
        # Referrer policy
        'Referrer-Policy': 'strict-origin-when-cross-origin',
        
        # Permissions policy (disable dangerous features)
        'Permissions-Policy': 'geolocation=(), microphone=(), camera=()',
        
        # Content Security Policy
        'Content-Security-Policy': (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "font-src 'self'; "
            "connect-src 'self' wss: https:; "
            "frame-ancestors 'none';"
        ),
    }


def get_cors_settings() -> dict[str, Any]:
    """
    Get CORS configuration.
    
    Returns:
        Dictionary of CORS settings
    """
    # Allow specific origins in production
    allowed_origins = getattr(settings, 'CORS_ALLOWED_ORIGINS', [
        'http://localhost:3000',
        'http://localhost:5173',
        'http://127.0.0.1:3000',
        'http://127.0.0.1:5173',
    ])
    
    return {
        'CORS_ALLOWED_ORIGINS': allowed_origins,
        'CORS_ALLOW_CREDENTIALS': True,
        'CORS_ALLOW_METHODS': ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
        'CORS_ALLOW_HEADERS': [
            'accept',
            'accept-encoding',
            'authorization',
            'content-type',
            'dnt',
            'origin',
            'user-agent',
            'x-csrftoken',
            'x-requested-with',
            'x-api-key',
        ],
        'CORS_EXPOSE_HEADERS': [
            'x-ratelimit-limit',
            'x-ratelimit-remaining',
            'x-ratelimit-reset',
        ],
        'CORS_MAX_AGE': 86400,  # 24 hours
    }


def get_cookie_settings() -> dict[str, Any]:
    """
    Get secure cookie settings.
    
    Returns:
        Dictionary of cookie settings
    """
    is_production = not getattr(settings, 'DEBUG', True)
    
    return {
        'SESSION_COOKIE_SECURE': is_production,
        'SESSION_COOKIE_HTTPONLY': True,
        'SESSION_COOKIE_SAMESITE': 'Lax',
        'SESSION_COOKIE_AGE': 86400 * 7,  # 7 days
        
        'CSRF_COOKIE_SECURE': is_production,
        'CSRF_COOKIE_HTTPONLY': True,
        'CSRF_COOKIE_SAMESITE': 'Lax',
    }


# ======================== Abuse Detection ========================

class AbuseDetector:
    """
    Detects and blocks abusive behavior patterns.
    
    Tracks:
    - Failed login attempts
    - Rate limit violations
    - Suspicious request patterns
    """
    
    def __init__(self):
        # In-memory storage (use Redis in production)
        self._failed_logins: dict[str, list] = {}
        self._rate_violations: dict[str, int] = {}
        self._blocked_ips: set[str] = set()
        
        # Thresholds
        self.max_failed_logins = 5
        self.max_rate_violations = 10
        self.block_duration_hours = 24
    
    def record_failed_login(self, ip: str, user_identifier: str = "") -> bool:
        """
        Record a failed login attempt.
        
        Returns True if IP should be blocked.
        """
        from datetime import datetime, timedelta
        
        key = ip
        now = datetime.utcnow()
        
        if key not in self._failed_logins:
            self._failed_logins[key] = []
        
        # Remove old attempts (last hour)
        cutoff = now - timedelta(hours=1)
        self._failed_logins[key] = [
            t for t in self._failed_logins[key] if t > cutoff
        ]
        
        # Add new attempt
        self._failed_logins[key].append(now)
        
        # Check threshold
        if len(self._failed_logins[key]) >= self.max_failed_logins:
            self.block_ip(ip)
            return True
        
        return False
    
    def record_rate_violation(self, ip: str) -> bool:
        """
        Record a rate limit violation.
        
        Returns True if IP should be blocked.
        """
        self._rate_violations[ip] = self._rate_violations.get(ip, 0) + 1
        
        if self._rate_violations[ip] >= self.max_rate_violations:
            self.block_ip(ip)
            return True
        
        return False
    
    def block_ip(self, ip: str) -> None:
        """Block an IP address."""
        self._blocked_ips.add(ip)
        logger.warning(f"Blocked IP due to abuse: {ip}")
    
    def unblock_ip(self, ip: str) -> None:
        """Unblock an IP address."""
        self._blocked_ips.discard(ip)
    
    def is_blocked(self, ip: str) -> bool:
        """Check if IP is blocked."""
        return ip in self._blocked_ips
    
    def get_blocked_ips(self) -> list[str]:
        """Get list of blocked IPs."""
        return list(self._blocked_ips)


# ======================== Global Instances ========================

_log_sanitizer: LogSanitizer | None = None
_abuse_detector: AbuseDetector | None = None


@lru_cache(maxsize=1)
def get_log_sanitizer() -> LogSanitizer:
    """Get global log sanitizer."""
    return LogSanitizer()


def get_abuse_detector() -> AbuseDetector:
    """Get global abuse detector."""
    global _abuse_detector
    if _abuse_detector is None:
        _abuse_detector = AbuseDetector()
    return _abuse_detector
