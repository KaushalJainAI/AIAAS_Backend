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
import html
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
        """Escape HTML special characters."""
        # Only escape if there are actual HTML-like patterns
        if '<' in text or '>' in text:
            # Preserve legitimate angle brackets in code
            # but escape potential HTML tags
            text = re.sub(r'<(?!/?(?:code|pre|br)\s*/?>)', '&lt;', text)
            text = re.sub(r'(?<!</?(?:code|pre|br))>', '&gt;', text)
        return text
    
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


class ContentPolicyEnforcer:
    """
    Enforce content policies beyond prompt injection.
    
    Checks for:
    - Excessive repetition (spam detection)
    - Suspicious encoding patterns
    - Policy violations
    """
    
    MAX_REPETITION_RATIO = 0.5  # Max 50% repeated characters/words
    
    def check_repetition(self, text: str) -> bool:
        """Check if text has excessive repetition (potential spam)."""
        if len(text) < 50:
            return True
        
        # Check character repetition
        char_counts = {}
        for char in text.lower():
            char_counts[char] = char_counts.get(char, 0) + 1
        
        max_char_ratio = max(char_counts.values()) / len(text)
        if max_char_ratio > self.MAX_REPETITION_RATIO:
            return False
        
        # Check word repetition
        words = text.lower().split()
        if len(words) > 10:
            word_counts = {}
            for word in words:
                word_counts[word] = word_counts.get(word, 0) + 1
            
            max_word_ratio = max(word_counts.values()) / len(words)
            if max_word_ratio > self.MAX_REPETITION_RATIO:
                return False
        
        return True
    
    def check_encoding_attack(self, text: str) -> bool:
        """Check for encoding-based attacks."""
        # Check for excessive escape sequences
        escape_count = text.count('\\')
        if escape_count > len(text) * 0.1:  # More than 10% escapes
            return False
        
        # Check for null bytes (shouldn't appear in text)
        if '\x00' in text:
            return False
        
        return True


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
