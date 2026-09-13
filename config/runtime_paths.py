"""Resolve runtime locations without assuming one person's home directory."""
from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve(path: str | Path) -> Path:
    """Normalize configured paths while allowing targets created later."""
    return Path(path).expanduser().resolve(strict=False)


def _environment(environment: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environment is None else environment


def resolve_forge_root(
        environment: Mapping[str, str] | None = None) -> Path:
    """Return this checkout by default, or an explicit ``FORGE_ROOT``."""
    values = _environment(environment)
    return _resolve(values.get("FORGE_ROOT", _REPO_ROOT))


def resolve_osworld_root(
        environment: Mapping[str, str] | None = None,
        forge_root: Path | None = None) -> Path:
    """Return the sibling OSWorld-V2 checkout unless explicitly configured."""
    values = _environment(environment)
    resolved_forge = forge_root or resolve_forge_root(values)
    return _resolve(values.get(
        "OSWORLD_ROOT", resolved_forge.parent / "OSWorld-V2"))


def resolve_env_file(
        environment: Mapping[str, str] | None = None,
        forge_root: Path | None = None) -> Path:
    """Return the Forge credential file, with an override for secret mounts."""
    values = _environment(environment)
    resolved_forge = forge_root or resolve_forge_root(values)
    return _resolve(values.get("FORGE_ENV_FILE", resolved_forge / ".env"))


def resolve_forge_path(
        path: str | Path,
        environment: Mapping[str, str] | None = None,
        forge_root: Path | None = None) -> Path:
    """Resolve a Forge-owned path independently of the process CWD."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    resolved_forge = forge_root or resolve_forge_root(environment)
    return (resolved_forge / candidate).resolve(strict=False)


def normalize_verifier_config_paths(config, forge_root: Path):
    """Make deferred Verifier config loads safe after entering OSWorld's CWD."""
    for attribute in (
            "agentic_verifier_config",
            "escalation_agentic_verifier_config"):
        value = getattr(config, attribute, "")
        if value:
            setattr(config, attribute, str(resolve_forge_path(
                value, forge_root=forge_root)))
    return config
