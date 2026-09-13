"""E7 STALL DETECTOR (PREREG E7 v2.2 §5, B5) — night-stop authority #2,
now SPECIFIED: a per-iteration QUIESCENCE probe, not a turn-count backstop
(the 150-it backstop is retired; 400 it / 8 h remains as authority #3).

Progress is defined by the WORLD, not the transcript:

  night_stamp()     mtime anchor (/tmp/.night_stamp) — touched at night
                    start and re-touched after every progressing probe, so
                    each probe asks "anything new since the LAST progress?".
  progress_probe()  one reading. True iff (a) any file under /home/user or
                    ~/.memory is newer than the stamp — ~/.memory dotfiles
                    INCLUDED (B5: a banking tail is progress, not
                    quiescence), while hidden paths OUTSIDE ~/.memory are
                    excluded (the live desktop writes .cache/.config
                    continuously — same rationale as the e8 gate manifest);
                    or (b) a melt/ffmpeg/shotcut process is alive (a
                    long render polls quietly for many iterations — B5:
                    running renders are progress).
  StallTracker      consecutive-quiet counter; `stalled` at threshold
                    (cfg practice_stall_iters, default 25; <=0 = OFF).

The e7 loop owns the wiring (between iterations) and fires the result as
status ``stalled_quiescent`` — flagged in curve.jsonl, EXCLUDED from band
arithmetic (§5). core/loop.py is never touched.

⚠️ pgrep runs WITHOUT -f: run_command wraps every command in a shell whose
own cmdline contains the pattern — -f would match the wrapper and the
detector could never fire. Name-only matching cannot self-match.
"""

NIGHT_STAMP = "/tmp/.night_stamp"
STALLED_STATUS = "stalled_quiescent"      # curve outcome the e7 loop records

PROBE_FIND = (
    f"find /home/user ~/.memory -newer {NIGHT_STAMP} -type f "
    r"\( -path '*/.memory/*' -o ! -path '*/.*' \) "
    "2>/dev/null | head -1")
PROBE_PGREP = "pgrep 'melt|ffmpeg|shotcut'"


def night_stamp(vm) -> None:
    """(Re)set the progress baseline — night start + after every True probe."""
    vm.run_command(f"touch {NIGHT_STAMP}", timeout=30)


def progress_probe(vm) -> bool:
    """One quiescence reading. Output filters are shape-based so channel
    errors, "[exit N]" trailers and find warnings can never counterfeit
    progress: a found file is a line starting with "/", a live render is an
    all-digit pid line. A True probe re-touches the stamp (the next probe
    measures from HERE, so quiet iterations accumulate, progress resets)."""
    out = vm.run_command(PROBE_FIND, timeout=60) or ""
    progressed = any(l.strip().startswith("/") for l in out.splitlines())
    if not progressed:
        out = vm.run_command(PROBE_PGREP, timeout=30) or ""
        progressed = any(l.strip().isdigit() for l in out.splitlines())
    if progressed:
        night_stamp(vm)
    return progressed


class StallTracker:
    """Consecutive-False probe counter. threshold = cfg.practice_stall_iters
    (25 in e7_actor.yaml; <=0 = detector OFF, `stalled` never fires)."""

    def __init__(self, threshold: int = 25):
        self.threshold = int(threshold)
        self.quiet = 0

    def update(self, progressed: bool) -> None:
        self.quiet = 0 if progressed else self.quiet + 1

    @property
    def stalled(self) -> bool:
        return self.threshold > 0 and self.quiet >= self.threshold
