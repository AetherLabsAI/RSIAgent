"""Exercise actual tar, symlink, and guest verification commands end to end."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from explore import provision7 as provision


class LocalGuest:
    def run_command(self, command, timeout=None, cap=None):
        result = subprocess.run(['bash', '-c', command], capture_output=True,
                                text=True, timeout=timeout, check=False)
        return result.stdout + result.stderr

    def fetch_file(self, path, max_bytes=None):
        return Path(path).read_bytes(), ''

    def push_file(self, local_path, guest_path):
        shutil.copyfile(local_path, guest_path)
        return True, ''


def test_real_venv_round_trip_preserves_absolute_chained_and_directory_links(tmp_path, monkeypatch):
    guest = tmp_path / 'guest'
    project = guest / 'evolution_project'
    project.mkdir(parents=True)
    venv = project / 'deps/venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True)
    source_links = {str(p.relative_to(guest)): os.readlink(p)
                    for p in venv.rglob('*') if p.is_symlink()}
    assert any(target.startswith('/') for target in source_links.values())
    assert (venv / 'lib64').is_symlink()
    for name, value in {
        'GUEST_HOME': guest, 'GUEST_TGZ_OUT': tmp_path / 'out.tgz',
        'GUEST_TGZ_IN': tmp_path / 'in.tgz', 'GUEST_B64_IN': tmp_path / 'in.b64',
        'GUEST_EXPECTED': tmp_path / 'expected', 'GUEST_ACTUAL': tmp_path / 'actual',
    }.items():
        monkeypatch.setattr(provision, name, str(value))
    vm, snapshot = LocalGuest(), tmp_path / 'snapshot'
    assert provision.capture_project_materials(
        vm, 'venv', str(snapshot), guest_dirs=['evolution_project'], attempts=1)['ok']
    original_bytes = (snapshot / provision.MATERIALS).read_bytes()
    assert provision.read_symlinks(str(snapshot)) == source_links
    shutil.rmtree(project)
    assert provision.replay_project_materials(vm, str(snapshot)) == {'ok': True, 'mismatches': []}
    assert provision.verify_project_materials(vm, str(snapshot)) == {'ok': True, 'mismatches': []}
    assert (snapshot / provision.MATERIALS).read_bytes() == original_bytes
    for rel, target in source_links.items():
        assert os.readlink(guest / rel) == target
    # A regular file containing the target bytes cannot impersonate a symlink.
    link = venv / 'bin/python'
    executable = link.read_bytes()
    link.unlink()
    link.write_bytes(executable)
    checked = provision.verify_project_materials(vm, str(snapshot))
    assert not checked['ok']
    assert str(link.relative_to(guest)) in {m['file'] for m in checked['mismatches']}


@pytest.mark.parametrize('rel', ['../escape', '/absolute', 'project/../escape'])
def test_opaque_targets_do_not_allow_unsafe_link_paths(tmp_path, rel):
    (tmp_path / provision.SYMLINKS).write_text(json.dumps({
        'schema_version': 1, 'links': {rel: '/usr/bin/python3'}}))
    assert provision.read_symlinks(str(tmp_path)) == {}
