"""Completed Agent work survives sealed-evaluator transport failures."""
import pytest

from core.grading import GradingBlocked, run_until_success
from core.trace import ArtifactSink


def test_grading_retries_same_operation_without_attempt_ceiling(tmp_path):
    calls = 0
    errors = []

    def grade():
        nonlocal calls
        calls += 1
        if calls <= 25:
            raise ConnectionError(f"temporary-{calls}")
        return {"score": 0.75}

    result = run_until_success(
        grade,
        on_error=lambda attempt, error: errors.append((attempt, str(error))),
        retry_delay=15,
        sleep_fn=lambda _seconds: None,
    )

    assert result == {"score": 0.75}
    assert calls == 26
    assert errors[0] == (1, "temporary-1")
    assert errors[-1] == (25, "temporary-25")


def test_grading_keeps_operator_interrupt_authoritative():
    with pytest.raises(KeyboardInterrupt):
        run_until_success(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))


def test_repeated_evaluator_bug_blocks_instead_of_retrying_forever():
    attempts = []
    sleeps = []

    def grade():
        raise AttributeError("'NoneType' object has no attribute 'name'")

    with pytest.raises(GradingBlocked) as raised:
        run_until_success(grade, on_error=lambda n, e: attempts.append(n),
                          sleep_fn=sleeps.append)
    assert attempts == [1, 2, 3]
    assert len(sleeps) == 2
    assert raised.value.attempt == 3
    assert isinstance(raised.value.__cause__, AttributeError)


def test_transient_failure_breaks_a_programming_error_streak():
    events = iter([AttributeError("bad"), AttributeError("bad"),
                   ConnectionError("temporary"), AttributeError("bad"), 0.6])

    def grade():
        event = next(events)
        if isinstance(event, Exception):
            raise event
        return event

    assert run_until_success(grade, sleep_fn=lambda _: None) == 0.6


def test_grading_state_is_durable_and_separate_from_result(tmp_path):
    sink = ArtifactSink(str(tmp_path / "run"))
    sink.save_grading_state({"status": "pending", "attempt": 3})

    assert (tmp_path / "run/grading.json").read_text().find('"attempt": 3') >= 0
    assert not (tmp_path / "run/result.json").exists()
