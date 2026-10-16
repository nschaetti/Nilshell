"""Exercise the real stdio transport against a local mock provider."""

import json
import os
import queue
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def test_stdio_chat_roundtrip(tmp_path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"Hi from mock"}}]}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    engine_file = Path(__file__).resolve().parents[1] / "main.py"
    command = (
        [os.environ["NILSHELL_TEST_ENGINE"]]
        if os.environ.get("NILSHELL_TEST_ENGINE")
        else [sys.executable, str(engine_file)]
    )
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **{k: v for k, v in os.environ.items() if "proxy" not in k.lower()},
            "NILSHELL_DATA_DIR": str(tmp_path),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        },
    )
    lines = queue.Queue()

    def reader():
        for line in process.stdout:
            lines.put(json.loads(line))

    threading.Thread(target=reader, daemon=True).start()
    counter = 0
    events = []

    def request(method, params):
        nonlocal counter
        counter += 1
        process.stdin.write(
            json.dumps({"id": str(counter), "method": method, "params": params}) + "\n"
        )
        process.stdin.flush()
        while True:
            result = lines.get(timeout=15)
            events.append(result)
            if result.get("id") == str(counter):
                assert "error" not in result, result
                return result["result"]

    try:
        assert request("bootstrap", {})["providers"] == []
        provider = request(
            "provider.save",
            {
                "name": "Mock",
                "kind": "openai",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            },
        )
        assistant = request(
            "assistant.save",
            {
                "name": "Ada",
                "provider_id": provider["id"],
                "model": "mock-model",
                "system_prompt": "Precise",
            },
        )
        conversation = request("conversation.create", {"assistant_id": assistant["id"]})
        request("chat.start", {"id": conversation["id"], "content": "Hi"})
        # Generation can finish before the chat.start acknowledgement. Poll persisted state.
        for _ in range(30):
            result = request("conversation.get", {"id": conversation["id"]})
            if result["messages"][-1]["status"] == "complete":
                break
            threading.Event().wait(0.02)
        assert result["messages"][-1]["content"] == "Hi from mock", (
            result["messages"],
            [e for e in events if e.get("event")],
            received,
        )
        assert result["messages"][-1]["status"] == "complete"
        assert received[0]["messages"][0] == {"role": "system", "content": "Precise"}
        assert received[0]["stream"] is True
        # The same packaged stdio surface must expose the syscall broker.
        path = tmp_path / "kernel-test.txt"
        prepared = request(
            "system.prepare",
            {"syscall": "fs.write", "arguments": {"path": str(path), "content": "local-only"}},
        )
        assert not path.exists()
        result = request("system.execute", {"id": prepared["id"], "approved": True})
        assert result["result"]["bytes_written"] == 10
        prepared = request(
            "system.prepare", {"syscall": "fs.read", "arguments": {"path": str(path)}}
        )
        result = request("system.execute", {"id": prepared["id"], "approved": True})
        assert result["result"]["content"] == "local-only"
        audit = request("system.history", {})
        assert len(audit) == 2
        assert "local-only" not in json.dumps(audit)

    finally:
        process.terminate()
        process.communicate(timeout=10)
        server.shutdown()
        server.server_close()
