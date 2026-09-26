"""
Every image this platform generates says so, on the picture and in the file.

India's IT Rules (amended Oct 2025, in force Feb 2026) require synthetically
generated content to carry a clear label, and the EU AI Act's Article 50
(from 2 Aug 2026) requires AI-generated images to be marked in a
machine-readable way. So a generated image leaves here with both:

* a **visible label** — a small "AI-generated" tag in the bottom-right corner,
  sized to the image so it is readable on a phone and never covers the subject;
* **metadata** — a PNG text chunk, or the EXIF `ImageDescription`/`Software`
  fields for JPEG and WebP, naming the model and saying the image is synthetic.

A person can crop the tag and strip the metadata; the duty is to mark what we
produce, and a label that survives ordinary sharing does that. Labelling is
best-effort in the one direction that is safe: if Pillow cannot open the bytes
(a format it does not know), the image is returned unchanged and the failure
is logged — never dropped, because the user has already paid for it.
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

LABEL_TEXT = 'AI-generated'


def _metadata_text(model: str) -> str:
    who = f' by {model}' if model else ''
    return f'AI-generated image (synthetic content){who}, made with AIAAS.'


def label_image(data: bytes, ext: str, *, model: str = '') -> bytes:
    """`data` with a visible AI label and AI metadata, in the same format."""
    try:
        from PIL import Image, ImageDraw, ImageFont, PngImagePlugin
    except ImportError:  # pragma: no cover — Pillow is in requirements
        logger.warning('[Labels] Pillow unavailable; image left unlabelled')
        return data

    fmt = {'jpg': 'JPEG', 'jpeg': 'JPEG', 'png': 'PNG', 'webp': 'WEBP'}.get((ext or '').lower())
    if fmt is None:
        # GIFs and anything unknown: animated frames would each need the tag,
        # and a wrong re-encode is worse than the metadata-only record kept on
        # the document row.
        return data
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        base = image.convert('RGBA')
        width, height = base.size
        size = max(12, min(width, height) // 28)
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:  # older Pillow: fixed-size bitmap font
            font = ImageFont.load_default()

        overlay = Image.new('RGBA', base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        left, top, right, bottom = draw.textbbox((0, 0), LABEL_TEXT, font=font)
        pad = max(4, size // 3)
        box_w, box_h = right - left + 2 * pad, bottom - top + 2 * pad
        x0, y0 = width - box_w - pad, height - box_h - pad
        draw.rounded_rectangle((x0, y0, x0 + box_w, y0 + box_h),
                               radius=pad, fill=(0, 0, 0, 150))
        draw.text((x0 + pad - left, y0 + pad - top), LABEL_TEXT,
                  font=font, fill=(255, 255, 255, 235))
        labelled = Image.alpha_composite(base, overlay)

        out = io.BytesIO()
        text = _metadata_text(model)
        if fmt == 'PNG':
            info = PngImagePlugin.PngInfo()
            info.add_text('Description', text)
            info.add_text('Software', 'AIAAS')
            info.add_text('AIGenerated', 'true')
            labelled.save(out, format='PNG', pnginfo=info)
        else:
            exif = Image.Exif()
            exif[0x010E] = text      # ImageDescription
            exif[0x0131] = 'AIAAS'   # Software
            rgb = labelled.convert('RGB') if fmt == 'JPEG' else labelled
            rgb.save(out, format=fmt, exif=exif, quality=92)
        return out.getvalue()
    except Exception:  # noqa: BLE001 — never lose an image the user paid for
        logger.exception('[Labels] Could not label a generated %s image', ext)
        return data


def is_labelled(data: bytes) -> bool:
    """Whether an image carries our AI metadata (for tests and audits)."""
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data))
        if (image.info or {}).get('AIGenerated') == 'true':
            return True
        description = image.getexif().get(0x010E) or ''
        return 'AI-generated' in str(description)
    except Exception:  # noqa: BLE001
        return False
