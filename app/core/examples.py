"""Sample content bundled with the repository (used by the review UI).

Kept in a module rather than inline in the HTML so the demo transcript is a real
file a reader can open and edit, and so the UI and the docs cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
SAMPLE_TRANSCRIPT_PATH = EXAMPLES_DIR / "sample_transcript.txt"


def load_sample_transcript() -> str:
    """Return the bundled demo transcript, or an empty string if it is missing."""
    try:
        return SAMPLE_TRANSCRIPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""