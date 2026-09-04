"""Persist a completed `Generation` as a `Document` in the user's file tree.

Every generated artifact lands in a per-user folder so the Documents page and
the agent VFS can find it without a second index:

  Images  → "Images" folder at root
  Videos  → "Videos" folder at root
  Audio   → "Audio"  folder at root

Folders are created idempotently via `filesystem.ensure_folder`; two concurrent
completions racing to create the same folder converge on one row rather than
a 400.

The bytes come from either a data URL (image base64, TTS base64) or a remote
signed URL (video `unsigned_urls`). Remote video bytes are fetched with a
60 s timeout and streamed; data URL bytes are decoded locally. The file is
then stored as a normal Document row (same `user_document_path` layout,
same `normalize_file_type`, same `pending` → background `process_document`
thread as every other upload), so existing Document lifecycle (trash, purge,
sharing) applies unchanged.

Failures are swallowed after logging: the Generation is already `completed`
and the user has its `output_url`; a Document that cannot be stored should
not turn a successful generation into a `failed` row.

Linking: the created Document's id is written to `Generation.metadata`
under `document_id` (server-owned, read-only to the client) so callers can
correlate the two without a schema migration. A nullable FK would be cleaner
but this keeps the change additive and backward-compatible.
"""
import base64
import logging
import re
import threading
import time
from typing import Optional

import requests
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

# Folder names at root, per-kinematic generation kind.
FOLDER_BY_KIND = {
    "image": "Images",
    "video": "Videos",
    "audio": "Audio",
}

MIME_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/ogg": "ogg",
}

EXT_BY_KIND = {
    "image": "png",
    "video": "mp4",
    "audio": "mp3",
}


def _ext_for(mime: str, kind: str) -> str:
    if mime:
        mime = mime.split(";")[0].strip().lower()
        if mime in MIME_TO_EXT:
            return MIME_TO_EXT[mime]
        # mime like "image/png" -> "png"
        if "/" in mime:
            return mime.split("/")[-1].split("+")[0][:12]
    return EXT_BY_KIND.get(kind, "bin")


def _sanitize_slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower())
    s = s.strip("-")[:limit].strip("-")
    return s or "generation"


def persist_generation_as_document(generation) -> Optional[object]:
    """Save a completed generation into the user's files.

    Returns the *first* Document, or None — the signature every caller already
    has. A batch is what makes this a loop: `n` may return up to ten images
    from one request, and persisting `output_url` alone put one of them in My
    Files and dropped the rest, which the user had asked for and been billed
    for. `output_urls` is the authority, with `output_url` as the fallback for
    rows written before that field existed.

    Safe to call from sync or async contexts (sync ORM only) and from Celery
    workers (lazy imports).
    """
    urls = list(getattr(generation, "output_urls", None) or [])
    if not urls and generation.output_url:
        urls = [generation.output_url]
    if not urls:
        logger.info("No output for generation %s; skip document persist", generation.id)
        return None

    documents = []
    for index, url in enumerate(urls):
        document = _persist_one(generation, url, index, len(urls))
        if document is not None:
            documents.append(document)

    if not documents:
        return None

    # Correlation is written once, after the loop: a partial failure should not
    # leave metadata claiming documents that were never created.
    try:
        meta = dict(generation.metadata or {})
        meta["document_id"] = documents[0].id
        meta["document_name"] = documents[0].name
        # Every document, for a batch. Kept beside the singular key rather than
        # replacing it, because the API and the frontend both read that one.
        meta["document_ids"] = [d.id for d in documents]
        generation.metadata = meta
        generation.save(update_fields=["metadata"])
    except Exception as e:
        logger.warning("Could not write document ids to generation %s metadata: %s",
                       generation.id, e)

    return documents[0]


def _persist_one(generation, output_url: str, index: int, total: int) -> Optional[object]:
    """One output of a generation -> one Document."""
    try:
        from inference.filesystem import ensure_folder
        from inference.models import Document, KnowledgeBase
        from inference.utils import normalize_file_type

        # Idempotency, per output rather than per generation: a batch has
        # several, and keying on the first would have skipped the other nine.
        try:
            existing = ((generation.metadata or {}).get("document_ids")
                        or ([(generation.metadata or {}).get("document_id")]
                            if (generation.metadata or {}).get("document_id") else []))
            if index < len(existing) and existing[index]:
                from inference.models import Document as DocCheck

                if DocCheck.objects.filter(id=existing[index], user=generation.user).exists():
                    logger.info("Generation %s output %s already persisted; skip",
                                generation.id, index)
                    return None
        except Exception:
            pass

        user = generation.user
        kind = (generation.type or "image").lower()
        folder_name = FOLDER_BY_KIND.get(kind, "Generated")
        try:
            folder = ensure_folder(user, folder_name, parent=None)
        except Exception as e:
            logger.warning("Could not ensure folder %s for user %s: %s", folder_name, user.id, e)
            folder = None

        # Decode bytes
        file_bytes: bytes
        mime = ""
        if output_url.startswith("data:"):
            try:
                header, b64 = output_url.split(",", 1)
                # header like data:image/png;base64
                if ";" in header:
                    mime = header.split(":", 1)[1].split(";")[0] if ":" in header else ""
                else:
                    mime = header.split(":", 1)[1] if ":" in header else ""
                file_bytes = base64.b64decode(b64, validate=False)
            except Exception as e:
                logger.warning("Failed to decode data URL for generation %s: %s", generation.id, e)
                return None
        else:
            try:
                resp = requests.get(output_url, timeout=60, stream=False)
                resp.raise_for_status()
                mime = resp.headers.get("Content-Type", "") or ""
                file_bytes = resp.content
                if not file_bytes:
                    logger.warning("Empty bytes fetched for generation %s from %s", generation.id, output_url[:80])
                    return None
            except Exception as e:
                logger.warning("Failed to download output_url for generation %s: %s", generation.id, e)
                return None

        ext = _ext_for(mime, kind)
        slug = _sanitize_slug(generation.prompt or kind)
        # Filename must be unique enough but human-readable: <slug>-<id>-<ts>.<ext>
        ts = int(time.time())
        # The index is in the name only for a batch: `-1` on a single result
        # would be noise, and four files sharing one timestamp would otherwise
        # differ by nothing at all.
        suffix = f"-{index + 1}" if total > 1 else ""
        filename = f"{slug}-{generation.id}{suffix}-{ts}.{ext}"

        file_type = normalize_file_type(filename, mime or "")
        # Ensure KB exists (same default as uploads) so process_document doesn't create race
        try:
            kb, _ = KnowledgeBase.objects.get_or_create(
                user=user,
                is_default=True,
                defaults={"name": "Default", "description": "Auto-created default knowledge base"},
            )
        except Exception:
            kb = None

        # Generated media (image/video/audio) is binary with no extractable
        # text — mark stored directly. Previously we created with status
        # pending and spawned process_document, which tried to open a FAISS
        # index and failed in environments without faiss/numpy (tests) — leaving
        # the document in failed state. Stored keeps it visible and downloadable
        # without invoking the vector/keyword backends. The file_type derived
        # from extension may be 'txt' for audio (mp3 not in the vocabulary),
        # so we gate on the generation kind, not the inferred file_type.
        is_media_generation = kind in ("image", "video", "audio")
        should_index = (not is_media_generation) and file_type in ("pdf", "txt", "md", "docx", "csv", "json", "html")
        doc_status = "pending" if should_index else "stored"

        document = Document.objects.create(
            user=user,
            name=filename,
            content_text="",
            file=ContentFile(file_bytes, name=filename),
            file_type=file_type,
            file_size=len(file_bytes),
            status=doc_status,
            knowledge_base=kb,
            folder=folder,
            metadata={"source": "imagine", "generation_id": generation.id,
                      "kind": generation.type, "output_index": index},
        )

        # Only index text-like documents; media is stored as-is.
        if should_index:
            try:
                from inference.tasks import process_document

                threading.Thread(
                    target=process_document, args=(document.id, kb.id if kb else None), daemon=True
                ).start()
            except Exception as e:
                logger.warning("Could not start process_document for %s: %s", document.id, e)

        logger.info(
            "Persisted generation %s (%s) as document %s in folder %s",
            generation.id,
            kind,
            document.id,
            folder_name,
        )
        return document
    except Exception as e:
        logger.exception("persist_generation_as_document failed for generation %s: %s",
                         getattr(generation, "id", "?"), e)
        return None

