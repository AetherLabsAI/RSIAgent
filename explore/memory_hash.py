"""Canonical content hashes for immutable memory snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


class MemoryStateError(RuntimeError):
    """The memory manifest is not canonical JSON data."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    try:
        rendered = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise MemoryStateError(f"state is not canonical JSON data: {exc}") from exc
    return (rendered + ("\n" if newline else "")).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """SHA-256 of canonical UTF-8 JSON (sorted keys, no insignificant space)."""

    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def tree_sha256(hashes: Mapping[str, str]) -> str:
    """Canonical root digest for a ``tree_hashes`` mapping."""

    normalized: dict[str, str] = {}
    for rel, digest in hashes.items():
        if (
            not isinstance(rel, str)
            or not rel
            or rel.startswith("/")
            or ".." in Path(rel).parts
            or not _SHA256_RE.fullmatch(str(digest))
        ):
            raise MemoryStateError("invalid tree-hash mapping")
        normalized[rel] = str(digest)
    return canonical_sha256(dict(sorted(normalized.items())))
