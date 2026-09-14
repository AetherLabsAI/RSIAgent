"""Keep OSWorld's command service alive when one guest program is OOM-killed."""
from __future__ import annotations

import base64
import logging
import re

from env.vm import VM

log = logging.getLogger(__name__)
POLICY_PATH = '/run/systemd/system/osworld.service.d/50-rsiagent-execute-oom.conf'
READY = 'RSIAGENT_CONTROLLER_OOM_POLICY_CONTINUE'
_ROOT_SCRIPT = r'''set -eu
unit=osworld.service
before_pid=$(systemctl show "$unit" --value -p MainPID)
test "$before_pid" -gt 0
before_policy=$(systemctl show "$unit" --value -p OOMPolicy)
case "$before_policy" in stop|kill|continue) ;; *) exit 125 ;; esac
systemctl is-active --quiet "$unit"
if [ "$before_policy" != continue ]; then
  install -d -m 0755 /run/systemd/system/osworld.service.d
  destination=/run/systemd/system/osworld.service.d/50-rsiagent-execute-oom.conf
  temporary=$(mktemp /run/systemd/system/osworld.service.d/.rsiagent-oom-XXXXXX)
  trap 'rm -f -- "$temporary"' EXIT
  printf '[Service]\nOOMPolicy=continue\n' > "$temporary"
  chmod 0644 "$temporary"
  if [ -e "$destination" ]; then
    cmp -s "$temporary" "$destination" || exit 125
  else
    mv -T -- "$temporary" "$destination"
  fi
  systemctl daemon-reload
fi
test "$(systemctl show "$unit" --value -p OOMPolicy)" = continue
test "$(systemctl show "$unit" --value -p MainPID)" = "$before_pid"
systemctl is-active --quiet "$unit"
printf 'RSIAGENT_CONTROLLER_OOM_POLICY_CONTINUE pid=%s previous=%s\n' "$before_pid" "$before_policy"
'''


def ensure_controller_oom_policy(desktop):
    """Install before task setup; do not restart the service or change RAM/OOM limits."""
    password = base64.b64encode(desktop.client_password.encode()).decode()
    payload = base64.b64encode(_ROOT_SCRIPT.encode()).decode()
    script = f'''set -eu
printf %s {password!r} | base64 -d | sudo -S -k -p '' -- bash -ceu '
  printf %s "$1" | base64 -d | bash
' rsiagent-controller-oom {payload!r}
sudo -K
'''
    trace = VM(desktop).run_script('bash', script, timeout=60, cap=0)
    match = re.search(r'^' + READY + r' pid=([1-9][0-9]*) previous=(stop|kill|continue)$',
                      trace.stdout, re.MULTILINE)
    if trace.exit_code != 0 or trace.infra_fail or trace.timed_out or not match:
        raise RuntimeError('OSWorld controller OOM policy setup was not verified: ' + trace.stdout)
    receipt = {'unit': 'osworld.service', 'policy': 'continue',
               'main_pid': int(match[1]), 'previous_policy': match[2],
               'service_restarted': False, 'ram_or_oom_limits_changed': False}
    log.info('OSWorld controller OOMPolicy=continue verified; service PID %s unchanged', match[1])
    return receipt
