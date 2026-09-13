#!/usr/bin/env python3
"""Practice app A — "Ops Console" (generic order-approval service, port 5081).

Self-authored practice terrain (E3): session/identity discipline, hidden state
channels (sqlite + /api/state), form validation, and exact deliverable shape
(exports must contain ONLY approved rows). Generic content by design — no
benchmark app, host, or task is imitated.
"""
import json
import random
import sqlite3
import time

from flask import Flask, jsonify, redirect, request, session

app = Flask(__name__)
app.secret_key = "practice-console-key"
DB = "/tmp/ops_console.db"
random.seed(81)

ITEMS = ["beam clamp", "hex stock", "flux pen", "din rail", "torx bit",
         "edge trim", "core sleeve", "gasket kit", "lug crimp", "vent cap"]


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    c = db()
    c.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, "
              "item TEXT, qty INTEGER, requester TEXT, status TEXT, "
              "note TEXT DEFAULT '')")
    if c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
        for i in range(14):
            c.execute("INSERT INTO orders (item, qty, requester, status) "
                      "VALUES (?,?,?,?)",
                      (random.choice(ITEMS), random.randint(1, 40),
                       f"desk-{random.randint(1, 6)}", "pending"))
    c.commit()
    c.close()


def authed():
    return session.get("user") == "analyst"


@app.route("/")
def home():
    if not authed():
        return ('<h3>Ops Console</h3><form method=post action=/login>'
                '<input name=user placeholder=user> '
                '<input name=pw type=password placeholder=password> '
                '<button>sign in</button></form>'
                '<p style="color:#888">provisioned account: analyst / rail-vent-42</p>')
    rows = db().execute("SELECT * FROM orders ORDER BY id").fetchall()
    body = "".join(
        f"<tr><td>{r['id']}</td><td>{r['item']}</td><td>{r['qty']}</td>"
        f"<td>{r['requester']}</td><td>{r['status']}</td>"
        f"<td><form method=post action=/decide style='display:inline'>"
        f"<input type=hidden name=id value={r['id']}>"
        f"<button name=verdict value=approved>approve</button>"
        f"<button name=verdict value=rejected>reject</button></form></td></tr>"
        for r in rows)
    return (f"<h3>Orders ({session['user']})</h3><table border=1>"
            f"<tr><th>id</th><th>item</th><th>qty</th><th>from</th>"
            f"<th>status</th><th>act</th></tr>{body}</table>"
            f"<p><a href=/export/approved.json>export approved</a> · "
            f"<a href=/logout>sign out</a></p>")


@app.route("/login", methods=["POST"])
def login():
    if request.form.get("user") == "analyst" and \
            request.form.get("pw") == "rail-vent-42":
        session["user"] = "analyst"
        return redirect("/")
    return "bad credentials", 403


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/decide", methods=["POST"])
def decide():
    if not authed():
        return "sign in first", 403
    oid, verdict = request.form.get("id"), request.form.get("verdict")
    if verdict not in ("approved", "rejected"):
        return "verdict must be approved|rejected", 400
    c = db()
    c.execute("UPDATE orders SET status=? WHERE id=?", (verdict, oid))
    c.commit()
    return redirect("/")


@app.route("/export/approved.json")
def export():
    # THE SHAPE RULE: an export is valid only if it contains approved rows and
    # NOTHING else — extra rows make it wrong, whatever else is right.
    if not authed():
        return "sign in first", 403
    rows = db().execute(
        "SELECT id, item, qty, requester FROM orders "
        "WHERE status='approved' ORDER BY id").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/state")
def state():
    if not authed():
        return jsonify({"error": "authenticated session required"}), 401
    rows = db().execute("SELECT * FROM orders ORDER BY id").fetchall()
    return jsonify({"user": session["user"], "server_time": int(time.time()),
                    "orders": [dict(r) for r in rows]})


if __name__ == "__main__":
    init()
    app.run(host="127.0.0.1", port=5081)
