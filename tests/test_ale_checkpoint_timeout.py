"""Large ALE checkpoints must retain the mandatory save/load/delete boundary."""
from types import SimpleNamespace

import pytest

from benchmarks.ale.transport import AleVM
from core.verifier_runtime import AgenticVerifierExecutor
from env.qemu_rollback import QemuRollbackError


class SlowCheckpointContainer:
    def __init__(self):
        self.snapshot = ''
        self.commands = []

    def exec_run(self, argv):
        command, timeout = argv[4], int(argv[5])
        self.commands.append(command)
        if command.startswith('savevm '):
            if timeout < 600:
                return SimpleNamespace(exit_code=124, output=b'RSIAGENT_HMP_COMMAND_TIMEOUT')
            self.snapshot = command.split()[1]
        elif command.startswith('loadvm '):
            assert command.split()[1] == self.snapshot
        elif command.startswith('delvm '):
            assert command.split()[1] == self.snapshot
            self.snapshot = ''
        elif command == 'info snapshots':
            row = f'-- {self.snapshot} 26.9 GiB\n' if self.snapshot else ''
            return SimpleNamespace(exit_code=0, output=row.encode())
        else:
            raise AssertionError(command)
        return SimpleNamespace(exit_code=0, output=b'(qemu)\n(qemu)\n')


def test_large_ale_checkpoint_completes_and_restores_candidate(monkeypatch):
    container = SlowCheckpointContainer()
    monkeypatch.setattr('benchmarks.ale.transport.docker.from_env', lambda: SimpleNamespace(
        containers=SimpleNamespace(get=lambda _name: container)))
    vm = AleVM({'os': 'windows', 'id': 'retained-vm', 'endpoint': 'http://unused',
                'python': 'python.exe'})
    monkeypatch.setattr(vm, 'wait_for_controller', lambda **_kwargs: (True, 'healthy'))
    executor = AgenticVerifierExecutor(vm, execution_mode='rollback_mirror')
    transaction = executor._rollback_transaction
    transaction.begin()
    assert transaction.active
    transaction.rollback()
    assert transaction.restored and not transaction.active
    assert container.commands == ['savevm ' + transaction.tag, 'info snapshots',
                                  'loadvm ' + transaction.tag, 'delvm ' + transaction.tag,
                                  'info snapshots']


def test_other_vm_providers_keep_their_existing_checkpoint_deadline():
    container = SlowCheckpointContainer()
    vm = SimpleNamespace(env=SimpleNamespace(provider_name='docker',
        provider=SimpleNamespace(container=container)))
    executor = AgenticVerifierExecutor(vm, execution_mode='rollback_mirror')
    with pytest.raises(QemuRollbackError, match='RSIAGENT_HMP_COMMAND_TIMEOUT'):
        executor._rollback_transaction.begin()
