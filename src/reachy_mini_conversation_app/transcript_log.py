"""Shared writer for the active timestamped conversation transcript."""

import logging
from pathlib import Path
from collections.abc import Iterable


logger = logging.getLogger(__name__)

_active_transcript_path: Path | None = None


def set_active_transcript_path(path: Path | None) -> None:
    """Set the transcript file used by conversation and tool diagnostics."""
    global _active_transcript_path
    _active_transcript_path = path


def append_transcript_lines(lines: Iterable[str]) -> None:
    """Append lines to the active transcript, if one has been initialized."""
    if _active_transcript_path is None:
        return

    try:
        with _active_transcript_path.open("a", encoding="utf-8") as transcript_file:
            for line in lines:
                transcript_file.write(f"{line}\n")
    except OSError:
        logger.exception("Could not append to transcript file at %s", _active_transcript_path)
