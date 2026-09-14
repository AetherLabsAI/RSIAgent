#!/usr/bin/env python3
"""HOST-SIDE ONLY (grader-diagnostic class): build the P2 silent-audit corpus —
8-gram shingles over all 108 task instructions. The corpus file is consumed
exclusively by explore/commit.silent_audit (host process); it never enters any
agent context, and run_explore.py never imports THIS module or task_loader.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config.runtime_paths import resolve_root, resolve_osworld_root

V2 = str(resolve_osworld_root())
RSIAGENT = str(resolve_root())
for p in (V2, RSIAGENT):
    sys.path.insert(0, p)
os.chdir(V2)

from task_loader import load_task_config, resolve_task_json_path   # noqa: E402
from explore.commit import (CORPUS_NORMALIZATION, build_corpus,    # noqa: E402
                            normalize_instruction_for_corpus)

OUT = os.path.join(RSIAGENT, "results", "explore", "corpus_shingles.json")

instructions, missing = [], []
for i in range(1, 121):
    tid = f"task_{i:03d}"
    try:
        cf = resolve_task_json_path(task_id=tid, base_dir="evaluation_examples",
                                    eval_version="v2")
        tc = load_task_config(cf, task_id=tid, base_dir="evaluation_examples",
                              eval_version="v2")
        # Prefer a declared static base when present (026/041), then normalize
        # the labeled run-generated credential line used by 057/072. Secrets
        # are neither useful leakage shingles nor reproducible provenance.
        ins = str(getattr(tc, "base_instruction", None)
                  or getattr(tc, "instruction", None)
                  or tc["instruction"])
        instructions.append(normalize_instruction_for_corpus(ins))
    except Exception:                                  # noqa: BLE001
        missing.append(tid)

os.makedirs(os.path.dirname(OUT), exist_ok=True)
build_corpus(OUT, instructions)
corpus_bytes = open(OUT, "rb").read()
shingles = json.loads(corpus_bytes)
manifest = {
    "schema_version": 2,
    "benchmark": "OSWorld-V2",
    "evaluation_version": "v2",
    "normalization": CORPUS_NORMALIZATION,
    "instruction_count": len(instructions),
    "absent_task_ids": missing,
    "shingle_count": len(shingles),
    "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
}
with open(OUT + ".MANIFEST.json", "w") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
    f.write("\n")
print(f"corpus built: {len(instructions)} instructions, "
      f"{len(missing)} ids absent, -> {OUT} "
      f"({os.path.getsize(OUT)} bytes, {len(shingles)} shingles, "
      f"sha256={manifest['corpus_sha256']})")
