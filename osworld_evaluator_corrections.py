"""Explicit, versioned corrections applied only after Agent work terminates."""
from __future__ import annotations

from functools import wraps
import hashlib
from pathlib import Path
from types import SimpleNamespace


TASK102_STYLE_CORRECTION = "task102-missing-paragraph-style-v1"
TASK102_SOURCE_SHA256 = "1c6a608323d0e2561de75c8a304f830dac58e167f80f316c62e1fa0d0af49ef5"


class _ParagraphWithDefaultStyle:
    """Supply an empty style name without changing the candidate document."""

    def __init__(self, paragraph):
        self._paragraph = paragraph
        self.style = SimpleNamespace(name="")

    def __getattr__(self, name):
        return getattr(self._paragraph, name)


def apply_evaluator_correction(task, correction_id: str) -> dict:
    """Apply the selected correction to the exact frozen evaluator revision.

    Neither task source files nor the original benchmark locks are modified.
    The returned provenance must accompany every corrected score.
    """
    if correction_id != TASK102_STYLE_CORRECTION:
        raise ValueError(f"unknown evaluator correction: {correction_id}")
    namespace = type(task).evaluate.__globals__
    source = Path(namespace["__file__"])
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if source.name != "task_102.py" or source_hash != TASK102_SOURCE_SHA256:
        raise ValueError("evaluator correction does not match the frozen task source")
    names = ("_heading_level", "_list_kind")
    for name in names:
        original = namespace[name]
        if getattr(original, "_forge_correction", None) == correction_id:
            continue

        def wrap(function):
            @wraps(function)
            def corrected(paragraph):
                if paragraph.style is None:
                    paragraph = _ParagraphWithDefaultStyle(paragraph)
                return function(paragraph)
            corrected._forge_correction = correction_id
            return corrected

        namespace[name] = wrap(original)
    return {
        "id": correction_id,
        "task_source_sha256": source_hash,
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "changed_helpers": list(names),
        "scope": "sealed_evaluator_only",
        "candidate_modified": False,
    }
