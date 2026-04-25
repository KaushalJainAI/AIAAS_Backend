import re
from typing import Optional, List
from .schemas import ModelMetadata, ModelVersion, IntentMetadata, OrchestrationState, PolicyMetadata


class Orchestrator:
    MODEL_PATTERNS = {
        "vendor_family_version": r"""
            (?ix)
            ^
            (?P<vendor>[a-z0-9]+)
            [:/\-]?
            (?P<family>[a-z]+(?:[._-]?[a-z0-9]+)*)
            (?:[-_/ ](?P<major>\d+))?
            (?:[._-](?P<minor>\d+))?
            (?:[._-](?P<patch>\d+))?
            (?:[-_/ ](?P<tag>preview|latest|mini|nano|flash|turbo|lite|pro|opus|sonnet|haiku|instruct|chat|reasoning))?
            (?:[-_/ ](?P<date>\d{4}(?:[-_]\d{2}(?:[-_]\d{2})?)?))?
            $
        """,
        "bare_family_version": r"""
            (?ix)
            ^
            (?P<family>[a-z]+(?:[._-]?[a-z0-9]+)*)
            (?:[-_/ ](?P<major>\d+))?
            (?:[._-](?P<minor>\d+))?
            (?:[._-](?P<patch>\d+))?
            (?:[-_/ ](?P<tag>preview|latest|mini|nano|flash|turbo|lite|pro|opus|sonnet|haiku|instruct|chat|reasoning))?
            (?:[-_/ ](?P<date>\d{4}(?:[-_]\d{2}(?:[-_]\d{2})?)?))?
            $
        """
    }

    REQUEST_PATTERNS = {
        "file_read": r"(?i)\b(read|open|load|parse|ingest|import)\b.*\b(file|csv|json|pdf|docx|xlsx|image)\b",
        "file_write": r"(?i)\b(write|save|export|create|generate)\b.*\b(file|csv|json|pdf|report)\b",
        "code_exec": r"(?i)\b(run|execute|write|generate)\b.*\b(code|python|script|sql|bash|query)\b",
        "tool_call": r"(?i)\b(call|use|invoke|fetch|query|search|send|post|get)\b.*\b(api|tool|function|endpoint|service)\b",
        "mixed": r"(?i)\b(read|load)\b.*\b(file)\b.*\b(and|then)\b.*\b(run|execute|call|analyze)\b"
    }

    @classmethod
    def normalize_model(cls, raw: str) -> ModelMetadata:
        metadata = ModelMetadata(raw=raw, normalized=raw)

        match = re.search(cls.MODEL_PATTERNS["vendor_family_version"], raw, re.VERBOSE)
        if not match:
            match = re.search(cls.MODEL_PATTERNS["bare_family_version"], raw, re.VERBOSE)

        if match:
            groups = match.groupdict()
            metadata.vendor = groups.get("vendor", "unknown")
            metadata.family = groups.get("family")
            metadata.version = ModelVersion(
                major=int(groups["major"]) if groups.get("major") else None,
                minor=int(groups["minor"]) if groups.get("minor") else None,
                patch=int(groups["patch"]) if groups.get("patch") else None
            )

            tag = groups.get("tag")
            if tag:
                metadata.tags.append(tag)

            v_str = f"{metadata.version.major}.{metadata.version.minor}" if metadata.version.major else ""
            metadata.normalized = f"{metadata.vendor}/{metadata.family}:{v_str}" if metadata.vendor != "unknown" else f"{metadata.family}:{v_str}"

        return metadata

    @classmethod
    def analyze_intent(cls, content: str) -> IntentMetadata:
        signals = []
        primary_intent = "chat"

        for name, pattern in cls.REQUEST_PATTERNS.items():
            if re.search(pattern, content):
                signals.append(name)

        if "mixed" in signals:
            primary_intent = "mixed"
        elif signals:
            primary_intent = signals[0]

        return IntentMetadata(raw=content, intent=primary_intent, signals=signals)

    @classmethod
    def orchestrate(cls, raw_model: str, content: str) -> OrchestrationState:
        model_meta = cls.normalize_model(raw_model)
        intent_meta = cls.analyze_intent(content)

        policy = PolicyMetadata()
        if "file_write" in intent_meta.signals:
            policy.allow_file_write = True

        return OrchestrationState(
            model=model_meta,
            request=intent_meta,
            policy=policy
        )
