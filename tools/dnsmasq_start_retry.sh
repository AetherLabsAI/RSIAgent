#!/bin/bash
# Retry only transient host inotify exhaustion before QEMU selects networking.
# Never start another VM or hide a persistent/other dnsmasq failure.
set -u
receipt=$(mktemp /tmp/rsiagent-dnsmasq-start.XXXXXX) || exit 1
trap 'rm -f "$receipt"' EXIT
for attempt in {1..30}; do
    /usr/sbin/dnsmasq "$@" >"$receipt" 2>&1
    status=$?
    cat "$receipt" >&2
    if [ "$status" -eq 0 ]; then
        exit 0
    fi
    if ! grep -Fq 'failed to create inotify: Too many open files' "$receipt"; then
        exit "$status"
    fi
    if [ "$attempt" -lt 30 ]; then
        echo "RSIAgent dnsmasq: transient inotify exhaustion, retry $attempt/30" >&2
        sleep 2
    fi
done
exit "$status"
