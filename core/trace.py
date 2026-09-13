"""Artifact persistence — every attempt is fully replayable from results/ alone.

Layout (one attempt):
  <root>/
    iter_01/turn.txt            raw model output
    iter_01/program.py|.sh|.txt|.md  the submitted program/action payload
    iter_01/trace.txt           full captured output
    iter_01/trace_meta.json     {exit_code, secs, timed_out}
    iter_01/checks.json         {accepted, rejections, results} (if that turn was a done)
    transcript.json             the full conversation (system prompt + every message)
    result.json                 final: status, iters, programs_run, score (sealed), ...
"""
import dataclasses
import json
import os


class ArtifactSink:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _iter_dir(self, n: int) -> str:
        d = os.path.join(self.root, f"iter_{n:02d}")
        os.makedirs(d, exist_ok=True)
        return d

    def save_turn(self, n: int, text: str) -> None:
        self._write(os.path.join(self._iter_dir(n), "turn.txt"), text)

    def save_program(self, n: int, lang: str, code: str) -> None:
        language = (lang or "").lower()
        if language.startswith("py"):
            ext = "py"
        elif language == "verifier-report":
            ext = "md"
        else:
            ext = "sh"
        self._write(os.path.join(self._iter_dir(n), f"program.{ext}"), code)

    def save_trace(self, n: int, trace) -> None:
        d = self._iter_dir(n)
        self._write(os.path.join(d, "trace.txt"), trace.stdout)
        self._json(os.path.join(d, "trace_meta.json"),
                   {"exit_code": trace.exit_code, "secs": round(trace.secs, 1),
                    "timed_out": trace.timed_out,
                    "infra_fail": bool(
                        getattr(trace, "infra_fail", False)),
                    "context_externalized": bool(
                        getattr(trace, "context_stdout", None) is not None)})

    def save_look(self, n: int, path: str, question: str, ok: bool) -> None:
        self._json(os.path.join(self._iter_dir(n), "look.json"),
                   {"path": path, "question": question, "ok": ok})

    def save_ask(self, n: int, question: str, answer: str, ok: bool) -> None:
        self._json(os.path.join(self._iter_dir(n), "ask.json"),
                   {"question": question, "answer": answer, "ok": ok})

    def save_summary(self, n: int, text: str) -> None:
        """The model-maintained WORK LOG at fold n (summary-mode compaction) —
        persisted per fold so summary fidelity can be audited against the full
        transcript."""
        self._write(os.path.join(self._iter_dir(n), "worklog.txt"), text)

    def save_verify2(self, n: int, verdict: str, findings: str,
                     transcript: list = None) -> None:
        # v34: never clobber — the stall-exit/forced inspection can run at the same
        # turn as a done-time inspection (22 archived runs lost the first artifact).
        path = os.path.join(self._iter_dir(n), "verify2.json")
        k = 2
        while os.path.exists(path):
            path = os.path.join(self._iter_dir(n), f"verify2_{k}.json")
            k += 1
        rec = {"verdict": verdict, "findings": str(findings),
               "transcript": transcript or []}
        # E4-A1c: the structured per-requirement judgement, when the inspector
        # reported one — the artifact that makes convergence measurable across a
        # run's inspections (items met 5 -> 7 -> 9).
        items = getattr(findings, "items", ())
        if items:
            rec["items"] = [{"req": r, "status": s, "evidence": e}
                            for r, s, e in items]
        doubts = getattr(findings, "doubts", "")
        if doubts:
            rec["doubts"] = doubts
        self._json(path, rec)

    def save_review(self, n: int, verdict: str, findings: str, reason: str,
                    model: str, transcript: list = None) -> None:
        """R2/v31: the doubt-gated SECOND inspection at the same done-turn — its own
        artifact so it never clobbers the first inspection's verify2.json. v32.1:
        carries the reviewer's probe transcript (it was discarded before — the AB-12
        forensics could not autopsy WHY reviewer probes failed)."""
        self._json(os.path.join(self._iter_dir(n), "review.json"),
                   {"verdict": verdict, "findings": findings,
                    "reason": reason, "model": model,
                    "transcript": transcript or []})

    def save_checks(self, n: int, accepted, rejections, results) -> None:
        self._json(os.path.join(self._iter_dir(n), "checks.json"), {
            "accepted": [dataclasses.asdict(c) for c in accepted],
            "rejections": rejections,
            "results": [dataclasses.asdict(r) for r in results],
        })

    def save_transcript(self, system: str, history: list) -> None:
        self._json(os.path.join(self.root, "transcript.json"),
                   {"system": system, "messages": history})

    def save_result(self, payload: dict) -> None:
        self._json(os.path.join(self.root, "result.json"), payload)

    def save_grading_state(self, payload: dict) -> None:
        """Persist post-Agent evaluator retry state without exposing it upstream."""
        self._json(os.path.join(self.root, "grading.json"), payload)

    def save_recovery(self, payload: dict) -> None:
        """Persist a transport-recovery boundary beside the raw transcript."""
        self._json(os.path.join(self.root, "recovery.json"), payload)

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _write(path: str, text: str) -> None:
        with open(path, "w") as f:
            f.write(text or "")

    @staticmethod
    def _json(path: str, obj) -> None:
        with open(path, "w") as f:
            json.dump(obj, f, indent=2)
