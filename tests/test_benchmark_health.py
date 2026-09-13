from tools.benchmark_health import attention_state


def test_retrying_evaluator_bug_is_visible_even_when_process_is_alive():
    assert attention_state(
        status="running", process_alive=True,
        grading={"status": "pending", "attempt": 730, "error_type": "AttributeError"},
        seconds_since_agent_activity=7,
    ) == ("grading_blocked", "evaluator_error")


def test_stopped_process_is_paused_and_completed_job_is_complete():
    assert attention_state(status="running", process_alive=True,
                           process_state="T") == ("paused", "operator_paused")
    assert attention_state(status="complete", process_alive=False,
                           grading={"status": "blocked"}) == ("complete", None)


def test_transient_grader_outage_is_distinguished_from_actor_progress():
    assert attention_state(
        status="running", process_alive=True,
        grading={"status": "pending", "attempt": 30, "error_type": "ConnectionError"},
    ) == ("grading_retry", "evaluator_retry")


def test_corrected_regrade_is_explicit_even_if_original_process_is_paused():
    assert attention_state(
        status="running", process_alive=True, process_state="T",
        grading={"status": "recovered", "corrected_score": 0.1},
    ) == ("grading_recovered", None)
