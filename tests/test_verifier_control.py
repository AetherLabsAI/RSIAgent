"""Trusted transport preserves isolation and rejects ambiguous RPC results."""
import base64
import io
import json
from types import SimpleNamespace
import tarfile

import pytest

from env.verifier_control import VerifierControl
from core.verifier_runtime import AgenticVerifierExecutor


class Container:
    def __init__(self, *, stale=False, fail=False):
        self.client = SimpleNamespace(api=SimpleNamespace(timeout=300))
        self.stale, self.fail = stale, fail
        self.requests = []

    def put_archive(self, directory, blob):
        assert directory == '/tmp'
        with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
            info = tf.getmembers()[0]
            assert info.mode == 0o600
            self.request = json.load(tf.extractfile(info))
            self.requests.append(self.request)

    def exec_run(self, args):
        if self.fail:
            return SimpleNamespace(exit_code=124, output=b'')
        result = {'uid':0, 'pid':42} if self.request['op']=='ping' else {
            'exit_code':0, 'timed_out':False, 'output_b64':base64.b64encode(b'valid\xffoutput').decode()}
        payload = {'nonce':'old' if self.stale else self.request['nonce'], 'result':result}
        return SimpleNamespace(exit_code=0, output=(json.dumps(payload)+'\n').encode())


def make_control(**kwargs):
    c = Container(**kwargs)
    env = SimpleNamespace(provider=SimpleNamespace(container=c))
    return VerifierControl(env), c


def test_control_rejects_stale_reply_and_transport_failure():
    for kwargs in [{'stale':True},{'fail':True}]:
        control, _ = make_control(**kwargs)
        with pytest.raises(RuntimeError):
            control.ping()
        trace = control.run_script('bash', 'printf trusted', timeout=20)
        assert trace.infra_fail and trace.exit_code == 125


def test_control_keeps_large_programs_out_of_argv_and_preserves_binary_evidence():
    control, c = make_control()
    code = '#'+('x'*300000)+'\nprintf trusted'
    trace = control.run_script('bash', code, timeout=900)
    sent = base64.b64decode(c.requests[0]['code_b64']).decode()
    assert sent.endswith(code)
    assert trace.exit_code == 0 and not trace.infra_fail
    assert 'dmFsaWT/b3V0cHV0' in trace.stdout
    assert 'EXTERNALIZED' in trace.context_stdout
    assert c.client.api.timeout >= 960


def test_executor_uses_control_for_wrappers_and_never_falls_back():
    calls = []
    result = SimpleNamespace(exit_code=125, infra_fail=True, stdout='control unavailable')
    class Control:
        def run_script(self, lang, code, **kwargs):
            calls.append(code)
            return result
    class VM:
        env = SimpleNamespace(client_password='held-by-harness', _forge_verifier_control=Control())
        def run_script(self, *_args, **_kwargs):
            pytest.fail('must not fall back to ordinary model execution')
    executor = AgenticVerifierExecutor(VM())
    trace = executor('bash', 'printf model')
    assert trace.infra_fail and trace.exit_code == 125
    assert 'control unavailable' in trace.stdout
    assert len(calls) == 1
    assert 'printf model' not in calls[0]
    assert 'id -u user' in calls[0]
