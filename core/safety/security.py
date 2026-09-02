"""
Input Sanitization and Security Utilities

Provides security features for:
- Prompt injection detection and prevention
- Input validation and sanitization
- Content policy enforcement

Usage:
    sanitizer = InputSanitizer()
    clean_text, violations = sanitizer.sanitize(user_input)
    if violations:
        log_security_event(violations)
"""
import re
import logging
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SecurityViolation:
    """Record of a security violation detected during sanitization."""
    pattern_name: str
    matched_text: str
    severity: str  # 'low', 'medium', 'high', 'critical'
    action_taken: str  # 'blocked', 'sanitized', 'logged'


@dataclass
class SanitizationResult:
    """Result of input sanitization."""
    original_text: str
    sanitized_text: str
    is_safe: bool
    violations: list[SecurityViolation] = field(default_factory=list)
    
    @property
    def was_modified(self) -> bool:
        return self.original_text != self.sanitized_text


class InputSanitizer:
    """
    Sanitize user inputs before LLM processing.
    
    Detects and handles:
    - Prompt injection attempts
    - System prompt manipulation
    - Role impersonation
    - Encoding-based attacks
    
    Example:
        >>> sanitizer = InputSanitizer()
        >>> result = sanitizer.sanitize("Ignore previous instructions")
        >>> result.is_safe
        False
    """
    
    # Patterns that indicate prompt injection attempts
    # Format: (name, pattern, severity, block_entirely)
    BLOCKED_PATTERNS = [
        # Direct instruction overrides
        ('instruction_override', r'ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|rules?)', 'high', True),
        ('forget_instructions', r'forget\s+(all\s+)?(previous|prior|your)\s+(instructions?|prompts?|training)', 'high', True),
        ('new_instructions', r'your\s+new\s+(instructions?|rules?|prompt)\s*(are|is|:)', 'high', True),
        ('override_rules', r'override\s+(your|all|the)?\s*(rules?|restrictions?|limitations?)', 'high', True),
        
        # System prompt extraction
        ('system_prompt_reveal', r'(show|reveal|display|print|output|tell\s+me)\s+(your|the)?\s*system\s*prompt', 'critical', True),
        ('initial_prompt', r'(what|show|reveal)\s+(is|are|was)?\s*(your|the)?\s*initial\s*(prompt|instructions?)', 'critical', True),
        ('repeat_instructions', r'repeat\s+(your|the|all)?\s*(system|initial|original)?\s*(prompt|instructions?)', 'critical', True),
        
        # Role/identity manipulation
        ('role_tags', r'<\/?(system|user|assistant|human|ai|bot)>', 'high', True),
        ('role_prefix', r'^(system|assistant|human|ai)\s*:', 'high', True),
        ('pretend_role', r'(pretend|act|behave)\s+(you\s+are|as\s+if|like)\s+(a\s+)?(different|new|another)', 'medium', False),
        
        # Jailbreak attempts
        ('dan_jailbreak', r'\bDAN\b.*\b(mode|enabled?|activated?)\b', 'critical', True),
        ('developer_mode', r'(developer|debug|admin)\s+mode\s+(enabled?|on|activated?)', 'critical', True),
        ('jailbreak_keyword', r'\b(jailbreak|jailbroken|bypass)\b', 'high', True),
        
        # Encoding attacks
        ('base64_injection', r'base64[:\s]+[A-Za-z0-9+/=]{20,}', 'medium', False),
        ('unicode_escape', r'\\u[0-9a-fA-F]{4}', 'low', False),
        
        # Context manipulation
        ('context_end', r'\[\/?(end|context|conversation)\]', 'medium', True),
        ('separator_injection', r'-{5,}|={5,}|\*{5,}', 'low', False),
    ]
    
    # Maximum allowed input lengths
    MAX_INPUT_LENGTH = 50000  # 50k characters
    MAX_MESSAGE_LENGTH = 10000  # 10k for chat messages
    
    def __init__(
        self,
        max_length: Optional[int] = None,
        additional_patterns: Optional[list] = None,
        strict_mode: bool = False
    ):
        """
        Initialize sanitizer.
        
        Args:
            max_length: Override default max input length
            additional_patterns: Extra patterns to check
            strict_mode: If True, block on any violation
        """
        self.max_length = max_length or self.MAX_INPUT_LENGTH
        self.strict_mode = strict_mode
        
        self.patterns = self.BLOCKED_PATTERNS.copy()
        if additional_patterns:
            self.patterns.extend(additional_patterns)
        
        # Compile regex patterns for performance
        self._compiled_patterns = [
            (name, re.compile(pattern, re.IGNORECASE | re.MULTILINE), severity, block)
            for name, pattern, severity, block in self.patterns
        ]
    
    def sanitize(self, text: str) -> SanitizationResult:
        """
        Sanitize input text.
        
        Args:
            text: Raw user input
            
        Returns:
            SanitizationResult with sanitized text and violations
        """
        if not text:
            return SanitizationResult(
                original_text='',
                sanitized_text='',
                is_safe=True
            )
        
        violations = []
        sanitized = text
        is_safe = True
        
        # Check length
        if len(text) > self.max_length:
            violations.append(SecurityViolation(
                pattern_name='input_too_long',
                matched_text=f'Length: {len(text)} > {self.max_length}',
                severity='medium',
                action_taken='truncated'
            ))
            sanitized = text[:self.max_length]
            is_safe = False
        
        # Check patterns
        for name, pattern, severity, should_block in self._compiled_patterns:
            matches = pattern.findall(sanitized)
            if matches:
                matched_text = matches[0] if isinstance(matches[0], str) else str(matches[0])
                violations.append(SecurityViolation(
                    pattern_name=name,
                    matched_text=matched_text[:100],  # Limit length
                    severity=severity,
                    action_taken='blocked' if should_block else 'logged'
                ))
                
                if should_block or self.strict_mode:
                    # Remove the matched content
                    sanitized = pattern.sub('[BLOCKED]', sanitized)
                    is_safe = False
                elif severity in ('high', 'critical'):
                    is_safe = False
        
        # Escape HTML to prevent XSS
        sanitized = self._escape_html(sanitized)
        
        # Log violations
        if violations:
            self._log_violations(text, violations)
        
        return SanitizationResult(
            original_text=text,
            sanitized_text=sanitized,
            is_safe=is_safe,
            violations=violations
        )
    
    def is_safe(self, text: str) -> bool:
        """Quick check if text is safe without full sanitization."""
        for name, pattern, severity, should_block in self._compiled_patterns:
            if should_block and pattern.search(text):
                return False
        return len(text) <= self.max_length
    
    def _escape_html(self, text: str) -> str:
        """Escape HTML special characters while preserving certain tags."""
        if not text:
            return text
        
        if '<' not in text and '>' not in text:
            return text

        # Protect <code>, <pre>, <br> (and their variations) while escaping other < and >
        # Use a single regex with alternation: protected tags are captured in group 1,
        # while bare < and > are captured in group 2.
        protected_tag_regex = r'(</?(?:code|pre|br)\s*/?>)|([<>])'
        
        def replace(match):
            # If group 1 matched, it's one of our protected tags - keep it
            if match.group(1):
                return match.group(1)
            # Group 2 matched a raw bracket to escape
            char = match.group(2)
            if char == '<':
                return '&lt;'
            return '&gt;'

        return re.sub(protected_tag_regex, replace, text, flags=re.IGNORECASE)
    
    def _log_violations(self, original: str, violations: list[SecurityViolation]):
        """Log security violations for audit."""
        for v in violations:
            log_level = {
                'low': logging.INFO,
                'medium': logging.WARNING,
                'high': logging.WARNING,
                'critical': logging.ERROR,
            }.get(v.severity, logging.INFO)
            
            logger.log(
                log_level,
                f"Security violation detected: {v.pattern_name} "
                f"(severity={v.severity}, action={v.action_taken})"
            )


# Singleton instance for easy access
_default_sanitizer: Optional[InputSanitizer] = None


def get_sanitizer() -> InputSanitizer:
    """Get the default InputSanitizer instance."""
    global _default_sanitizer
    if _default_sanitizer is None:
        _default_sanitizer = InputSanitizer()
    return _default_sanitizer


def sanitize_input(text: str) -> tuple[str, bool]:
    """
    Convenience function to sanitize input.
    
    Returns:
        Tuple of (sanitized_text, is_safe)
    """
    result = get_sanitizer().sanitize(text)
    return result.sanitized_text, result.is_safe


class SensitiveDataFilter(logging.Filter):
    """
    Logging filter that redacts sensitive data from log records.
    Used in Django LOGGING config as 'core.safety.security.SensitiveDataFilter'.
    """
    
    def filter(self, record):
        if hasattr(record, 'msg') and isinstance(record.msg, str):
            # Lazy-init sanitizer to avoid circular imports at module load
            sanitizer = get_log_sanitizer()
            record.msg = sanitizer.sanitize(record.msg, redact_pii=True)
        return True



# ============================================================
# Security Config (merged from security_config.py)
# ============================================================

from functools import lru_cache

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
            'password', 'secret', 'api_key', 'apikey', 'auth_token',
            'private_key', 'access_token', 'refresh_token', 'client_secret'
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


# ======================== Global Instances ========================

@lru_cache(maxsize=1)
def get_log_sanitizer() -> LogSanitizer:
    """Get global log sanitizer."""
    return LogSanitizer()
