"""Controller policy installation is verified and precedes benchmark setup."""
import base64
from types import SimpleNamespace

import pytest

from env import controller_oom
from env.vm import Trace


def test_installer_requires_existing_service_pid_and_retains_program_resource_limits(monkeypatch):
    seen = []
    class VM:
        def __init__(self, desktop):
            assert desktop.client_password == 'synthetic-test-password'
        def run_script(self, lang, code, **kwargs):
            seen.append(code)
            return Trace('FORGE_CONTROLLER_OOM_POLICY_CONTINUE pid=590 previous=stop\n[exit 0]', 0)
    monkeypatch.setattr(controller_oom, 'VM', VM)
    receipt = controller_oom.ensure_controller_oom_policy(SimpleNamespace(client_password='synthetic-test-password'))
    assert receipt['main_pid'] == 590 and not receipt['service_restarted']
    assert not receipt['ram_or_oom_limits_changed']
    script = controller_oom._ROOT_SCRIPT
    assert 'OOMPolicy=continue' in script and 'daemon-reload' in script
    assert 'before_pid' in script and 'MainPID' in script
    assert ' restart ' not in script and ' stop ' not in script
    assert 'MemoryMax=' not in script and 'OOMScoreAdjust=' not in script
    assert base64.b64encode(script.encode()).decode() in seen[0]


@pytest.mark.parametrize('trace', [
    Trace('', 0),
    Trace('FORGE_CONTROLLER_OOM_POLICY_CONTINUE pid=590 previous=stop', 1),
    Trace('FORGE_CONTROLLER_OOM_POLICY_CONTINUE pid=590 previous=stop', 0, infra_fail=True),
    Trace('FORGE_CONTROLLER_OOM_POLICY_CONTINUE pid=590 previous=stop', 0, timed_out=True),
    Trace('FORGE_CONTROLLER_OOM_POLICY_CONTINUE pid=0 previous=stop', 0),
])
def test_installer_rejects_missing_or_conflicting_confirmation(monkeypatch, trace):
    monkeypatch.setattr(controller_oom, 'VM', lambda desktop: SimpleNamespace(run_script=lambda *a, **k: trace))
    with pytest.raises(RuntimeError, match='not verified'):
        controller_oom.ensure_controller_oom_policy(SimpleNamespace(client_password='test'))


def test_installer_accepts_already_continuing_service(monkeypatch):
    trace = Trace('FORGE_CONTROLLER_OOM_POLICY_CONTINUE pid=590 previous=continue\n[exit 0]', 0)
    monkeypatch.setattr(controller_oom, 'VM', lambda desktop: SimpleNamespace(run_script=lambda *a, **k: trace))
    result = controller_oom.ensure_controller_oom_policy(SimpleNamespace(client_password='test'))
    assert result['previous_policy'] == 'continue' and result['main_pid'] == 590
