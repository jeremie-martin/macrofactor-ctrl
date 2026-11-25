"""Text normalisation shared by matching, search and typing on the phone."""

from __future__ import annotations

import re
import unicodedata


def ascii_fold(text: str) -> str:
    """Drop accents: `adb shell input text` can only type ASCII."""
    decomposed = unicodedata.normalize("NFKD", text.replace("œ", "oe").replace("Œ", "Oe").replace("’", "'"))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).encode("ascii", "ignore").decode()


def norm(text: str) -> str:
    """Comparison key: accent-free, case-free, single-spaced."""
    return re.sub(r"\s+", " ", ascii_fold(text).casefold()).strip()
