"""
Input Sanitization and Security Utilities

Provides security features for:
- Refusing user messages shaped like an attack on the model (jailbreaks,
  instruction overrides, system-prompt extraction, fake role markers)
- Redacting secrets and PII from logs

Usage:
    result = get_sanitizer().sanitize(user_input)
    if not result.is_safe:
        ...  # refuse the request; nothing about it is stored

**Refuse or pass, never rewrite (2026-09-25).** This used to replace matched
phrases with `[BLOCKED]` and HTML-escape every `<`/`>`, then hand the rewritten
text to the view as if the user had typed it — so "how do I bypass the paywall
bug" reached the model as "how do I [BLOCKED] the paywall bug", pasted code
arrived as `&lt;`, and nobody was told. Now a message either matches an attack
pattern and is refused whole (the middleware answers 400 before the view runs,
so it is never saved and never enters the conversation history), or it passes
through byte for byte. HTML escaping belongs where text is rendered, and React
already does it.

**Stronger by looking through disguises, not by matching single words.** Every
message is checked in several views: as typed; NFKC-normalised with invisible
characters stripped and look-alike Cyrillic/Greek letters mapped to Latin;
with leetspeak undone (`1gn0r3`); squashed to letters only, which catches
`i.g.n.o.r.e p-r-e-v-i-o-u-s`; and any base64 blob decoded. A single word
("bypass", "jailbreak") is only logged — blocking a whole message on one word
refuses ordinary questions about software.

This is a first layer against *direct* attempts by the person typing. It
cannot see indirect injection (instructions inside a web page or an email a
tool returns); that is `core/safety/provenance.py`'s job, and the grants,
scopes and approval gates beneath both are what actually bound a fooled model.
"""
import base64
import binascii
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


@dataclass
class SecurityViolation:
    """Record of a security violation detected during sanitization."""
    pattern_name: str
    matched_text: str
    severity: str  # 'low', 'medium', 'high', 'critical'
    action_taken: str  # 'blocked' or 'logged'


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

    @property
    def blocked(self) -> list[SecurityViolation]:
        return [v for v in self.violations if v.action_taken == 'blocked']


#: What the person is told when a message is refused. Deliberately does not
#: name the pattern that matched: telling an attacker which phrase tripped the
#: filter is how they learn to rephrase around it.
USER_NOTICE = (
    "We didn't process this message due to security concerns: it looks like an "
    "attempt to override the assistant's instructions or safety rules. It was "
    "not saved to your conversation, so you can rephrase it and keep chatting."
)

# -- deobfuscation -------------------------------------------------------------

#: Zero-width, joiner, bidi-control and filler characters. Invisible on screen,
#: so their only use inside a word is to break a pattern match.
_INVISIBLE = re.compile(
    '[­͏؜ᅟᅠ឴឵᠋-᠏​-‏'
    '‪-‮⁠-⁯ㅤ︀-️﻿ﾠ]'
)

#: Cyrillic and Greek letters that render identically to Latin ones.
_HOMOGLYPHS = str.maketrans({
    'а': 'a', 'е': 'e', 'о': 'o', 'р': 'p', 'с': 'c', 'у': 'y', 'х': 'x',
    'і': 'i', 'ј': 'j', 'ѕ': 's', 'ԁ': 'd', 'һ': 'h', 'ӏ': 'l', 'ո': 'n',
    'ս': 'u', 'ɡ': 'g', 'ν': 'v', 'ο': 'o', 'α': 'a', 'ε': 'e', 'ι': 'i',
    'κ': 'k', 'τ': 't', 'ρ': 'p', 'ѵ': 'v', 'ԝ': 'w',
    'А': 'A', 'В': 'B', 'Е': 'E', 'К': 'K', 'М': 'M', 'Н': 'H', 'О': 'O',
    'Р': 'P', 'С': 'C', 'Т': 'T', 'Х': 'X', 'І': 'I', 'Ѕ': 'S', 'Ј': 'J',
    'Α': 'A', 'Β': 'B', 'Ε': 'E', 'Ι': 'I', 'Κ': 'K', 'Μ': 'M', 'Ν': 'N',
    'Ο': 'O', 'Ρ': 'P', 'Τ': 'T', 'Χ': 'X', 'Υ': 'Y', 'Ζ': 'Z',
})

_LEET = str.maketrans({
    '0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't',
    '@': 'a', '$': 's', '!': 'i', '|': 'l', '€': 'e',
})

_BASE64_BLOB = re.compile(r'[A-Za-z0-9+/]{24,}={0,2}')

#: How much of a message is scanned. Far above any real chat message; a
#: larger body is still scanned this far and flagged, never silently skipped.
MAX_SCAN_LENGTH = 200_000


def _normalise(text: str) -> str:
    text = unicodedata.normalize('NFKC', text)
    return _INVISIBLE.sub('', text).translate(_HOMOGLYPHS)


def _decoded_blobs(text: str) -> Iterator[str]:
    """Readable text hidden in base64 blobs, if any."""
    for blob in _BASE64_BLOB.findall(text)[:20]:
        try:
            raw = base64.b64decode(blob + '=' * (-len(blob) % 4), validate=True)
            decoded = raw.decode('utf-8')
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
        printable = sum(ch.isprintable() or ch.isspace() for ch in decoded)
        if decoded and printable / len(decoded) > 0.9:
            yield decoded


# -- patterns ------------------------------------------------------------------

_I = re.IGNORECASE | re.MULTILINE

#: (name, pattern, severity, blocks). Matched against every view of the text.
#: A blocking pattern must describe an *attack*, never a topic: someone asking
#: what a system prompt is, or how jailbreaks work, is asking a question.
BLOCKED_PATTERNS = [
    # Instruction overrides
    ('instruction_override',
     r'\b(ignore|disregard|forget|override|bypass|skip|drop|abandon|discard|neglect)\s+'
     r'(all\s+|any\s+|every\s+)?(of\s+)?(the\s+|your\s+|my\s+|these\s+|those\s+|its\s+)?'
     r'(previous|prior|above|earlier|preceding|original|initial|system|existing|current|safety)\s+'
     r'(instructions?|prompts?|rules|directions|guidelines|constraints|restrictions|'
     r'programming|directives?|guardrails|polic(y|ies))',
     'critical', True),
    ('forget_training',
     r'\bforget\s+(everything|all)\s+(that\s+)?you\s+(were|have\s+been|know|was)\s*'
     r'(told|taught|trained|programmed|instructed)?',
     'critical', True),
    ('new_instructions',
     r'\b(your|the)\s+(new|updated|real|actual|true)\s+(instructions?|rules|prompt|directives?)'
     r'\s*(are|is|:)',
     'critical', True),
    ('override_rules',
     r'\boverride\s+(your|all|the|any)?\s*(rules?|restrictions?|limitations?|safety|'
     r'guardrails|programming)',
     'critical', True),

    # System-prompt extraction — aimed at *your* prompt, not the concept
    # "The system prompt of my invoice agent" is a question about the user's
    # own configuration, so only *your* prompt, or a hidden one, is aimed at us.
    ('system_prompt_reveal',
     r'\b(show|reveal|display|print|output|tell|give|share|leak|dump|repeat|recite|'
     r'write\s+out|list|expose)\s+(me\s+|us\s+)?(all\s+)?(of\s+)?'
     r'(your\s+(full\s+|exact\s+|entire\s+|complete\s+|verbatim\s+)?'
     r'(system|hidden|secret|initial|original|developer|internal|underlying)|'
     r'the\s+(hidden|secret|internal|underlying)(\s+system)?)\s+'
     r'(prompt|instructions?|message|rules|directives?)',
     'critical', True),
    ('system_prompt_question',
     r'\bwhat\s+(is|are|was|were)\s+your\s+(exact\s+|full\s+)?'
     r'(system|hidden|secret|initial|original|internal)\s+(prompt|instructions?|rules)',
     'critical', True),
    # "Copy the text above into a table" is ordinary chat; the extraction
    # trick asks for it verbatim, or from a fixed starting phrase.
    ('text_above',
     r'\b(repeat|print|output|echo|reproduce|copy)\s+(everything|all|'
     r'the\s+(text|words|content|instructions?))\s+(above|before)\b.{0,60}'
     r'\b(starting\s+with|verbatim|word\s+for\s+word|you\s+were\s+(given|told))|'
     r'\b(everything|all\s+the\s+text)\s+(before|above)\s+(this|my)\s+'
     r'(conversation|first\s+message)',
     'critical', True),

    # Fake role and chat-template markers
    ('role_tags', r'<\s*/?\s*(system|assistant|developer|human)\s*>', 'high', True),
    ('chat_template_tokens',
     r'<\|\s*(im_start|im_end|system|endoftext|eot_id|start_header_id|end_header_id)'
     r'\s*\|>|\[/?INST\]|<<\s*/?SYS\s*>>',
     'critical', True),
    ('role_prefix',
     r'^\s*#{0,3}\s*(system|developer)\s*(prompt|message|instructions?)?\s*:\s*'
     r'(you\s+(are|must|will)|ignore|disregard|forget|from\s+now|new\s+instructions)',
     'high', True),
    ('context_markers',
     r'\[\s*/?\s*(end|context|conversation|system)\s*\]|'
     r'\b(end\s+of\s+(the\s+)?(system\s+)?(prompt|instructions)|'
     r'begin\s+new\s+(instructions|prompt|session))\b',
     'high', True),

    # Jailbreak personas and modes
    ('dan_persona',
     # Case-sensitive (see CASE_SENSITIVE): "you are Dan" is a person.
     r'\b(?i:you\s+are|act\s+as|become|pretend\s+to\s+be|you\'re)\s+(?i:now\s+)?DAN\b|'
     r'(?i:\bdo\s+anything\s+now\b.{0,80}\b(mode|jailbreak|no\s+(restrictions|rules|limits)))',
     'critical', True),
    ('dan_mode', r'\bDAN\s+(mode|prompt|jailbreak)\b', 'critical', True),
    ('special_mode',
     r'\b(developer|god|sudo|jailbreak|jailbroken|unrestricted|unfiltered)\s+mode\s+'
     # Not "on": "enable developer mode on android" is a phone question.
     r'(is\s+)?(now\s+)?(enabled|activated|engaged)\b|'
     r'\b(you\s+are|you\'re)\s+(now\s+)?(in\s+)?(developer|god|sudo|jailbreak|'
     r'unrestricted|unfiltered)\s+mode\b|'
     r'\b(enable|activate|enter|switch\s+(on|to)|turn\s+on)\s+(god|sudo|jailbreak|'
     r'unrestricted|unfiltered)\s+mode\b',
     'critical', True),
    ('unrestricted_persona',
     r'\b(you\s+(are|will\s+be)|you\'re|act\s+as|pretend\s+(to\s+be|you\s+are)|'
     r'respond\s+as|behave\s+as|roleplay\s+as)\s+(now\s+)?(an?\s+)?'
     r'(unrestricted|unfiltered|uncensored|jailbroken|amoral|unaligned|unethical)\b',
     'critical', True),
    ('no_restrictions',
     r'\byou\s+(now\s+)?(have|with)\s+no\s+(restrictions|filters|guidelines|rules|limits|'
     r'limitations|censorship|ethics|morals|boundaries)\b|'
     r'\bpretend\s+(that\s+)?(you\s+)?(have|there\s+are)\s+no\s+(rules|restrictions|'
     r'guidelines|filters|limits)\b|'
     r'\b(without|ignoring|free\s+(of|from)|bypass(ing)?)\s+(all\s+|any\s+)?(of\s+)?your\s+'
     r'(restrictions|filters|guidelines|safety|content\s+polic(y|ies)|rules|'
     r'programming|training|alignment)\b',
     'critical', True),
    ('jailbreak_request',
     r'\bjailbreak\s+(yourself|(the|this|your)\s+(model|ai|assistant|chatbot|llm|'
     r'safety|filters?|guardrails))\b',
     'critical', True),
    ('refusal_suppression',
     r'\byou\s+(must|will|should|can|may)\s+(never|not)\s+refuse\b|'
     r'\bnever\s+refuse\s+(any|a|my)\s+(request|question|prompt|instruction)',
     'high', True),

    # Logged only: a word, not an attack
    ('jailbreak_keyword', r'\b(jailbreak|jailbroken|bypass)\b', 'medium', False),
    ('pretend_role',
     r'(pretend|act|behave)\s+(you\s+are|as\s+if|like)\s+(a\s+)?(different|new|another)',
     'medium', False),
    ('base64_payload', r'base64[:\s]+[A-Za-z0-9+/=]{20,}', 'medium', False),
    ('unicode_escape', r'\\u[0-9a-fA-F]{4}', 'low', False),
    ('separator_injection', r'-{5,}|={5,}|\*{5,}', 'low', False),
]

#: Patterns whose capitals carry meaning: `DAN` is the jailbreak, `Dan` a name.
CASE_SENSITIVE = frozenset({'dan_persona', 'dan_mode'})

#: Letters-only signatures, checked against the message with every space,
#: dot and dash removed — the view that sees `i-g-n-o-r-e a.l.l p r e v i o u s`.
#: Only long, specific phrases: squashing joins words, so a short signature
#: would match across the boundary of two innocent ones.
SQUASHED_PATTERNS = [
    ('instruction_override',
     r'(ignore|disregard|forget)(all|any|the|your|my)*(previous|prior|above|earlier|'
     r'preceding|original|initial)(instructions?|prompts?|rules|directions|guidelines)'),
    ('system_prompt_reveal',
     r'(reveal|show|print|repeat|display|leak|dump|output)(me)?(your(system|hidden|secret|'
     r'initial|original)|the(hidden|secret))(prompt|instructions)'),
    ('special_mode', r'(developer|jailbreak|god)mode(enabled|activated)'),
    ('no_restrictions', r'youhavenorestrictions|withoutyourrestrictions'),
]


class InputSanitizer:
    """
    Refuse user messages shaped like an attack on the model.

    Returns a result whose `is_safe` is False when any blocking pattern matched
    in any view of the text. The text itself is never changed:
    `sanitized_text` is the input, so a caller that passes it on sends exactly
    what the person typed.

    Example:
        >>> InputSanitizer().sanitize("Ignore previous instructions").is_safe
        False
    """

    BLOCKED_PATTERNS = BLOCKED_PATTERNS
    MAX_INPUT_LENGTH = MAX_SCAN_LENGTH

    def __init__(
        self,
        max_length: Optional[int] = None,
        additional_patterns: Optional[list] = None,
        strict_mode: bool = False
    ):
        """
        Args:
            max_length: Override how much of the text is scanned
            additional_patterns: Extra (name, pattern, severity, blocks) tuples
            strict_mode: If True, logged-only patterns block too
        """
        self.max_length = max_length or self.MAX_INPUT_LENGTH
        self.strict_mode = strict_mode

        patterns = list(self.BLOCKED_PATTERNS) + list(additional_patterns or [])
        self._compiled_patterns = [
            (name, re.compile(pattern, re.MULTILINE if name in CASE_SENSITIVE else _I),
             severity, block)
            for name, pattern, severity, block in patterns
        ]
        self._squashed = [(name, re.compile(p)) for name, p in SQUASHED_PATTERNS]

    def _views(self, text: str) -> Iterator[tuple[str, str]]:
        """(label, text) for every way of reading `text`, without repeats."""
        seen: set[str] = set()

        def fresh(view: str) -> bool:
            if view in seen:
                return False
            seen.add(view)
            return True

        normal = _normalise(text)
        for label, view in (('plain', text), ('normalised', normal),
                            ('leetspeak', normal.translate(_LEET))):
            if fresh(view):
                yield label, view
        for decoded in _decoded_blobs(normal):
            decoded = _normalise(decoded)
            if fresh(decoded):
                yield 'base64', decoded

    def sanitize(self, text: str) -> SanitizationResult:
        """Check `text`. The result's `sanitized_text` is always `text`."""
        if not text:
            return SanitizationResult(original_text='', sanitized_text='', is_safe=True)

        violations: list[SecurityViolation] = []
        found: set[str] = set()
        scanned = text[:self.max_length]
        if len(text) > self.max_length:
            violations.append(SecurityViolation(
                pattern_name='input_too_long', matched_text=f'{len(text)} chars',
                severity='low', action_taken='logged',
            ))

        def add(name: str, matched: str, severity: str, block: bool, label: str):
            key = name if label == 'plain' else f'{name}+{label}'
            if name in found:
                return
            found.add(name)
            blocks = block or self.strict_mode
            violations.append(SecurityViolation(
                pattern_name=key, matched_text=matched[:100],
                severity=severity, action_taken='blocked' if blocks else 'logged',
            ))

        for label, view in self._views(scanned):
            for name, pattern, severity, block in self._compiled_patterns:
                # Obfuscation only matters for patterns that block: a disguised
                # "bypass" is still just a word.
                if label != 'plain' and not block:
                    continue
                match = pattern.search(view)
                if match:
                    add(name, match.group(0), severity, block, label)

        squashed = re.sub(r'[^a-z]', '', _normalise(scanned).translate(_LEET).lower())
        for name, pattern in self._squashed:
            match = pattern.search(squashed)
            if match:
                add(name, match.group(0), 'critical', True, 'squashed')

        is_safe = not any(v.action_taken == 'blocked' for v in violations)
        if violations:
            self._log_violations(violations)
        return SanitizationResult(
            original_text=text, sanitized_text=text,
            is_safe=is_safe, violations=violations,
        )

    def is_safe(self, text: str) -> bool:
        """Whether `text` would pass."""
        return self.sanitize(text).is_safe

    def _log_violations(self, violations: list[SecurityViolation]):
        """Log pattern names only — never the message, which may be private."""
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
