#!/usr/bin/env python3
"""Practice app B — "Ticket Stream" (live-window triage service, port 5082).

Self-authored practice terrain (E3): work arrives in TIMED BATCHES for ~35
minutes after first contact — one early sweep misses most of it. Organs:
long-window awareness, state polling, late-arrival handling. Generic content.
"""
import random
import threading
import time

from flask import Flask, jsonify, request

app = Flask(__name__)
random.seed(82)

TICKETS = {}          # id -> {text, severity, arrived, triaged}
_started = [None]     # first-contact timestamp
_lock = threading.Lock()
KINDS = [("printer queue jam on floor %d", "low"),
         ("badge reader offline at gate %d", "medium"),
         ("cold-room sensor %d drifting", "high"),
         ("backup job %d skipped a volume", "high"),
         ("meeting room %d display flicker", "low")]


def _add_batch(n):
    with _lock:
        for _ in range(n):
            tid = len(TICKETS) + 1
            tmpl, sev = random.choice(KINDS)
            TICKETS[tid] = {"id": tid, "text": tmpl % random.randint(1, 9),
                            "severity": sev, "arrived": int(time.time()),
                            "triaged": None}


def _feeder():
    # 5 batches, ~7 min apart, from first contact — a ~35-minute live window.
    for _ in range(5):
        time.sleep(7 * 60)
        _add_batch(random.randint(2, 4))


@app.route("/api/state")
def state():
    if _started[0] is None:
        _started[0] = int(time.time())
        _add_batch(3)
        threading.Thread(target=_feeder, daemon=True).start()
    with _lock:
        items = sorted(TICKETS.values(), key=lambda t: t["id"])
    open_n = sum(1 for t in items if not t["triaged"])
    return jsonify({"window_open_secs": int(time.time()) - _started[0],
                    "feed_active": (int(time.time()) - _started[0]) < 36 * 60,
                    "open": open_n, "tickets": items})


@app.route("/triage", methods=["POST"])
def triage():
    tid = request.json.get("id") if request.is_json else None
    action = request.json.get("action") if request.is_json else None
    if action not in ("ack", "escalate", "close"):
        return jsonify({"error": "action must be ack|escalate|close"}), 400
    with _lock:
        t = TICKETS.get(tid)
        if not t:
            return jsonify({"error": f"no ticket {tid}"}), 404
        t["triaged"] = action
    return jsonify({"ok": True, "ticket": t})


@app.route("/")
def home():
    return ("<h3>Ticket Stream</h3><p>JSON service. GET /api/state to open the "
            "window (new tickets keep arriving for a while); POST /triage "
            '{"id": N, "action": "ack|escalate|close"}.</p>')


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5082)
