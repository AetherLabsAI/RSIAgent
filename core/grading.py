"""Post-Agent grading transport that preserves completed work across outages.

This module knows nothing about benchmark tasks, graders, scores, or candidate state.
It only retries an opaque operation after the Actor/Verifier lifecycle has ended.
Keeping the retry here makes the no-abandonment policy testable without importing the
sealed benchmark boundary from :mod:`benchmarks.osworld.task`.
"""
from __future__ import annotations

import time


class GradingBlocked(RuntimeError):
    """A repeated evaluator defect needs repair; preserve the completed VM."""

    def __init__(self, attempt, error):
        self.attempt = attempt
        self.error = error
        super().__init__(f"grading blocked after {attempt} attempts: {error}")


_PROGRAMMING_ERRORS = (AttributeError, TypeError, NameError, ImportError,
                       SyntaxError, AssertionError)


def run_until_success(operation, *, on_error=None, retry_delay=30.0,
                      sleep_fn=time.sleep, max_identical_programming_errors=3):
    """Run ``operation`` until it returns, preserving state after every exception.

    Transient evaluator/provider outages have no attempt ceiling. Repeated identical
    programming errors raise ``GradingBlocked``; callers must preserve completed
    state and expose the blockage to operators. ``KeyboardInterrupt`` and
    ``SystemExit`` remain operator-controlled terminal signals because they are not
    ``Exception`` subclasses.
    """
    attempt = 0
    previous_error = None
    identical_errors = 0
    while True:
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - opaque external evaluator boundary
            attempt += 1
            if on_error is not None:
                on_error(attempt, exc)
            error_key = (type(exc), str(exc))
            identical_errors = (identical_errors + 1
                                if error_key == previous_error else 1)
            previous_error = error_key
            if (isinstance(exc, _PROGRAMMING_ERRORS)
                    and identical_errors >= max_identical_programming_errors):
                raise GradingBlocked(attempt, exc) from exc
            sleep_fn(max(0.0, float(retry_delay)))
