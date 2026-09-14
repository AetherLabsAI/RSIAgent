"""Private host RPC for fresh guest resets; exposes no evaluator operation."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import socketserver
import tempfile
import threading
from pathlib import Path


class ResetService:
    def __init__(self, reset):
        self.loop = asyncio.get_running_loop()
        self.directory = tempfile.TemporaryDirectory(prefix="rsiagent-ale-rpc-")
        self.path = str(Path(self.directory.name) / "reset.sock")
        loop = self.loop

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    request = json.loads(self.rfile.readline(4096))
                    if (
                        set(request) != {"operation", "sandbox_id"}
                        or request["operation"] != "reset"
                    ):
                        raise ValueError(
                            "Only fresh reset of an owned sandbox is allowed"
                        )
                    result = asyncio.run_coroutine_threadsafe(
                        reset(request["sandbox_id"]), loop
                    ).result()
                    reply = {"sandbox": result}
                except Exception as exc:
                    reply = {"error": f"{type(exc).__name__}: {exc}"}
                self.wfile.write(json.dumps(reply).encode() + b"\n")

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = False

        self.server = Server(self.path, Handler)
        os.chmod(self.path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    async def close(self):
        await asyncio.to_thread(self.server.shutdown)
        await asyncio.to_thread(self.server.server_close)
        self.thread.join()
        self.directory.cleanup()


def request_reset(endpoint, sandbox_id):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2400)
        client.connect(endpoint)
        client.sendall(
            json.dumps({"operation": "reset", "sandbox_id": sandbox_id}).encode()
            + b"\n"
        )
        with client.makefile("rb") as stream:
            response = json.loads(stream.readline(1 << 20))
    if "error" in response:
        raise RuntimeError("Fresh ALE reset failed: " + response["error"])
    return response["sandbox"]
