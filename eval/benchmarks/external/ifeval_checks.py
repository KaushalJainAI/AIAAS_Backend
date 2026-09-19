"""
Ported IFEval instruction checkers (Apache-2.0, original (c) Google).

Only instruction types with no extra dependencies are ported. Language
detection and similar are skipped unless the user agrees to a dev-only dep.
"""
from __future__ import annotations

import re


def check_keyword_frequency(answer: str, kwargs: dict) -> bool:
    keyword = str(kwargs.get('keyword', '')).lower()
    count = (answer or '').lower().count(keyword)
    want = int(kwargs.get('frequency', 1))
    relation = str(kwargs.get('relation', 'at_least'))
    if relation == 'at_least':
        return count >= want
    if relation == 'at_most':
        return count <= want
    return count == want


def check_keyword_presence(answer: str, kwargs: dict) -> bool:
    keywords = kwargs.get('keywords') or []
    return all(str(k).lower() in (answer or '').lower() for k in keywords)


def check_keyword_absence(answer: str, kwargs: dict) -> bool:
    keywords = kwargs.get('forbidden') or []
    return all(str(k).lower() not in (answer or '').lower() for k in keywords)


def check_length_words(answer: str, kwargs: dict) -> bool:
    words = len((answer or '').split())
    low = int(kwargs.get('min', 0))
    high = int(kwargs.get('max', 10 ** 9))
    return low <= words <= high


def check_end_sentence(answer: str, kwargs: dict) -> bool:
    ending = str(kwargs.get('ending', ''))
    return (answer or '').strip().endswith(ending)


def check_start_paragraph(answer: str, kwargs: dict) -> bool:
    prefix = str(kwargs.get('prefix', ''))
    text = (answer or '').strip()
    return text.startswith(prefix)


def check_bullet_count(answer: str, kwargs: dict) -> bool:
    lines = [line for line in (answer or '').splitlines() if line.strip().startswith('- ')]
    return len(lines) == int(kwargs.get('count', 0))


def check_json_format(answer: str, kwargs: dict) -> bool:
    import json

    try:
        json.loads(answer or '')
        return True
    except Exception:  # noqa: BLE001
        return False


def check_letter_frequency(answer: str, kwargs: dict) -> bool:
    letter = str(kwargs.get('letter', 'e')).lower()
    count = (answer or '').lower().count(letter)
    return count >= int(kwargs.get('min', 1))


def check_capital_word_frequency(answer: str, kwargs: dict) -> bool:
    words = re.findall(r'\b[A-Z][a-z]*\b', answer or '')
    return len(words) >= int(kwargs.get('min', 1))


__all__ = [name for name in dir() if name.startswith('check_')]
