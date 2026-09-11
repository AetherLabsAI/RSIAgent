"""Signal metadata from real guest HTTP responses must survive transport."""
import base64
from types import SimpleNamespace

import pytest

from env.vm import VM, Trace
from core.verifier_runtime import AgenticVerifierExecutor


def guest(monkeypatch, returncode, output=''):
    body = {'status': 'success', 'returncode': returncode, 'output': output, 'error': ''}
    monkeypatch.setattr('env.vm.requests.post', lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: body))
    return VM(SimpleNamespace(controller=SimpleNamespace(http_server='http://guest.invalid')))


@pytest.mark.parametrize('returncode,partial', [(-9, ''), (-15, 'partial output\n')])
def test_signal_terminated_guest_execute_preserves_failure_and_partial_output(monkeypatch, returncode, partial):
    vm = guest(monkeypatch, returncode, partial)
    out = vm.run_command('synthetic-command', cap=0)
    assert out.startswith('[channel error: guest execute terminated by signal ')
    assert f'signal {-returncode}' in out
    assert 'execution may have occurred' in out
    assert partial.rstrip() in out


@pytest.mark.parametrize('returncode', [None, 0, 1, 143])
def test_ordinary_guest_command_output_is_unchanged(monkeypatch, returncode):
    vm = guest(monkeypatch, returncode, 'ordinary output\n')
    assert vm.run_command('synthetic-command', cap=0) == 'ordinary output'


def test_signal_terminated_transport_is_infra_not_empty_success(monkeypatch):
    vm = guest(monkeypatch, -15)
    trace = vm.run_script('bash', 'printf example', timeout=60)
    assert trace.infra_fail and trace.exit_code is None and not trace.timed_out
    assert 'signal 15' in trace.stdout


def test_model_program_signal_exit_with_intact_wrapper_remains_program_result(monkeypatch):
    envelope = 'FORGE_RUN_OUTPUT_BASE64:' + base64.b64encode(b'Killed\n').decode() + '\n[exit 137]'
    vm = guest(monkeypatch, 0, envelope)
    trace = vm.run_script('bash', 'synthetic-program', timeout=60)
    assert trace.exit_code == 137 and not trace.infra_fail and not trace.timed_out
    assert 'Killed' in trace.stdout


@pytest.mark.parametrize('method', ['_run_isolated', '_run_rollback_mirror'])
def test_missing_readiness_does_not_claim_program_never_ran(method):
    executor = object.__new__(AgenticVerifierExecutor)
    executor._execution_mode = 'effect_isolation'
    executor._workspace = '/mnt/test_workspace'
    executor._namespace_pid = 123
    executor._sudo_password_b64 = ''
    executor._private_paths = ()
    executor._run_harness_script = lambda *a, **k: Trace('partial output after execution', None, 1.0, False, True)
    trace = getattr(executor, method)('python3', 'py', 'print("example")', 60)
    assert trace.infra_fail and trace.exit_code == 125
    assert 'execution is unknown' in trace.stdout
    assert 'partial output after execution' in trace.stdout
    assert 'did not receive' not in trace.stdout
