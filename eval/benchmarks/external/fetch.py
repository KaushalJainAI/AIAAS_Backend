"""
Download → cache, pin revision, verify sha256.

External datasets are never committed (licences, and keeping them out of
training data). They are downloaded at run time into a git-ignored cache
(`settings.EVAL_DATA_DIR`, default `Backend/.eval_data/`).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from django.conf import settings


def cache_dir() -> Path:
    base = getattr(settings, 'EVAL_DATA_DIR', 'Backend/.eval_data/')
    path = Path(base)
    if not path.is_absolute():
        # Relative to the Backend package root.
        from django.conf import settings as _s  # noqa: F401
        path = Path(__file__).resolve().parents[3] / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def verify(path: Path, sha256: str) -> None:
    """Refuse on checksum mismatch."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != sha256:
        raise ValueError(
            f'checksum mismatch for {path.name}: got {digest}, want {sha256}'
        )


def fetch_hf(repo: str, filename: str, *, revision: str = '',
             sha256: str = '', gated: bool = False) -> Path:
    """Download one file from Hugging Face into the cache. Returns its path."""
    if gated and not os.environ.get('HF_TOKEN'):
        raise ValueError(
            'This dataset is gated and needs HF_TOKEN in the environment. '
            'Accept the terms on Hugging Face, mint a token, and retry.'
        )
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise ValueError('huggingface_hub is required to fetch external data') from exc
    dest = cache_dir() / f'{repo.replace("/", "__")}__{filename}'
    if dest.exists():
        if sha256:
            verify(dest, sha256)
        return dest
    downloaded = hf_hub_download(
        repo_id=repo, filename=filename,
        revision=revision or None,
        local_dir=str(cache_dir()),
        local_dir_use_symlinks=False,
    )
    Path(downloaded).rename(dest)
    if sha256:
        verify(dest, sha256)
    return dest


__all__ = ['cache_dir', 'fetch_hf', 'verify']
