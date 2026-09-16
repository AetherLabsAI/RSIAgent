"""`python -m llm.models` — the model selector for an OrcaRouter run.

RSIAgent picks its model from a per-run config, so this command is the selection
surface: it lists what the *account* can actually call, filtered for the entry
point that will use it, and refuses a model that is not on that list.

    python -m llm.models list                          # text chat / agent loop
    python -m llm.models list --capability multimodal --modality image
    python -m llm.models list --json                   # for a script or a wrapper
    python -m llm.models check openai/gpt-5.5          # validate a configured slug

The list is read from ``GET {api_base}/models`` on the configured OrcaRouter
origin with the user's own credential. When that read fails, the command says so
and shows the small verified fallback catalog rather than pretending the
workspace has five models — or, worse, accepting a model name typed from memory.

Anything that can drive a model selector — a config editor, a future GUI, a
wrapper script — should call :func:`llm.catalog.discover` and
:func:`llm.catalog.require_compatible` rather than re-deriving a list, which is
how a second, unfiltered menu gets built by accident.
"""
from __future__ import annotations

import argparse
import json
import sys

from llm import catalog as CAT
from llm.provider import (
    AUTHORIZED_APPS_URL,
    ORCA_KEY_ENV,
    ORCAROUTER,
    CredentialUnavailable,
    api_base,
    provider_profile,
)

# Which entry point each capability label corresponds to, in the repository's own
# vocabulary, so a user picking a filter can find theirs.
ENTRY_POINTS = {
    CAT.CAPABILITY_CHAT:
        "text chat and the agent loop (Actor, Verifier, Curriculum, compaction)",
    CAT.CAPABILITY_MULTIMODAL:
        "native Look image understanding (core/eyes.py) — requires a declared "
        "image input modality",
    CAT.CAPABILITY_EMBEDDING:
        "embeddings / retrieval — not currently used by this repository",
    CAT.CAPABILITY_IMAGE:
        "image generation — not currently used by this repository",
    CAT.CAPABILITY_VIDEO:
        "video generation — not currently used by this repository",
    CAT.CAPABILITY_RERANK:
        "reranking — not currently used by this repository",
}


def _describe(record: CAT.ModelRecord) -> str:
    parts = [record.id]
    if record.context_length:
        parts.append(f"ctx {record.context_length:,}")
    if record.input_modalities:
        parts.append("in: " + ",".join(record.input_modalities))
    if record.reasoning_efforts:
        parts.append("reasoning: " + "/".join(record.reasoning_efforts))
    if record.source != CAT.SOURCE_LIVE:
        parts.append(f"[{record.source}]")
    return "  ".join(parts)


def _cmd_list(args) -> int:
    modality = args.modality
    if args.capability == CAT.CAPABILITY_MULTIMODAL and not modality:
        # Default to the modality this repository actually uploads.
        modality = "image"
    try:
        result = CAT.discover(args.capability, modality=modality,
                              refresh=args.refresh)
    except CredentialUnavailable as exc:
        print(f"Not connected to OrcaRouter: {exc}", file=sys.stderr)
        print(f"Set {ORCA_KEY_ENV}, or run `python -m llm.connect login`.",
              file=sys.stderr)
        return 1
    except CAT.CatalogError as exc:
        print(f"Model discovery failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({
            "provider": ORCAROUTER,
            "api_base": api_base(),
            "capability": result.capability,
            "modality": modality,
            "source": result.source,
            "degraded": result.degraded,
            "detail": result.detail,
            "models": [
                {"id": record.id,
                 "context_length": record.context_length,
                 "input_modalities": list(record.input_modalities),
                 "reasoning_efforts": list(record.reasoning_efforts),
                 "source": record.source}
                for record in result.models
            ],
        }, indent=2))
        return 0

    print(f"{provider_profile(ORCAROUTER).label} — {result.capability}"
          + (f" ({modality} input)" if modality else ""))
    print(f"catalog: {result.label()}")
    print(f"endpoint: {api_base()}")
    print(f"entry point: {ENTRY_POINTS.get(result.capability, '')}")
    if result.degraded:
        # State the degradation plainly: a fallback list must never be mistaken
        # for what the account can call.
        print("NOTE: live discovery is unavailable, so this is the verified "
              "fallback catalog, not your workspace list.")
        print(f"      reason: {result.detail}")
    if not result.models:
        print("\nNo model is compatible with this entry point.")
        if result.capability == CAT.CAPABILITY_MULTIMODAL:
            print("That is expected when the catalog declares no model with the "
                  "required input modality. Nothing is guessed from model names.")
        return 0
    print()
    for record in result.models:
        print(f"  {_describe(record)}")
    print(f"\n{len(result.models)} model(s). "
          f"Manage keys at {AUTHORIZED_APPS_URL}")
    return 0


def _cmd_check(args) -> int:
    try:
        result = CAT.discover(args.capability, modality=args.modality,
                              refresh=args.refresh)
    except (CredentialUnavailable, CAT.CatalogError) as exc:
        print(f"cannot validate {args.model!r}: {exc}", file=sys.stderr)
        return 1
    try:
        CAT.require_compatible(args.model, result, modality=args.modality)
    except CAT.IncompatibleModel as exc:
        # Exit 2 distinguishes "not offered" from "could not check" (exit 1), so
        # a caller can tell a stale config value from an outage.
        print(f"incompatible: {exc}", file=sys.stderr)
        return 2
    print(f"{args.model} is offered for {result.capability}"
          + (f" ({args.modality} input)" if args.modality else "")
          + f" — {result.label()}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m llm.models",
        description="List and validate OrcaRouter models for one entry point.")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sub_parser):
        sub_parser.add_argument("--capability", default=CAT.CAPABILITY_CHAT,
                                choices=CAT.CAPABILITIES)
        sub_parser.add_argument(
            "--modality", default=None,
            help="non-text input the entry point uploads "
                 "(image / audio / video); required for multimodal")
        sub_parser.add_argument("--refresh", action="store_true",
                                help="ignore the cached catalog")

    listing = sub.add_parser("list", help="the selectable models")
    add_common(listing)
    listing.add_argument("--json", action="store_true",
                         help="machine-readable output")
    listing.set_defaults(func=_cmd_list)

    check = sub.add_parser("check", help="validate one model against the catalog")
    add_common(check)
    check.add_argument("model")
    check.set_defaults(func=_cmd_check)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if (args.capability == CAT.CAPABILITY_MULTIMODAL and not args.modality
            and args.command == "check"):
        args.modality = "image"
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
