"""Interpret process and grading state without mistaking retries for progress."""
from __future__ import annotations


def attention_state(*, status, process_alive, process_state=None,
                    grading=None, seconds_since_agent_activity=None):
    grading = grading or {}
    if status == "complete":
        return status, None
    if grading.get("status") == "recovered":
        return "grading_recovered", None
    programming_errors = {"AttributeError", "TypeError", "NameError",
                          "ImportError", "SyntaxError", "AssertionError"}
    if grading.get("status") == "blocked" or (
            grading.get("status") == "pending"
            and grading.get("error_type") in programming_errors
            and grading.get("attempt", 0) >= 3):
        return "grading_blocked", "evaluator_error"
    if process_state in {"T", "t"}:
        return "paused", "operator_paused"
    if status == "running" and not process_alive:
        return "needs_review", "process_exited"
    if grading.get("status") == "pending":
        return "grading_retry", "evaluator_retry"
    if (status == "running" and seconds_since_agent_activity is not None
            and seconds_since_agent_activity >= 900):
        return status, "quiet_15min_review_only"
    return status, None
