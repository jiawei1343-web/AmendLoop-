"""Actual JSON-RPC stdio exchange with the official-SDK server; bounded timeouts."""
import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest


@unittest.skipUnless(importlib.util.find_spec("mcp"), "Optional MCP SDK not installed")
class MCPIntegrationTests(unittest.TestCase):
    def test_real_stdio_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, AI_CORRECTION_MEMORY_DIR=str(Path(directory) / "memory"))
            server = Path(__file__).resolve().parents[1] / "scripts/mcp_server.py"
            process = subprocess.Popen([sys.executable, "-X", "utf8", str(server)], env=env,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            incoming, errors = queue.Queue(), []
            def read():
                for line in process.stdout:
                    incoming.put(line)
                incoming.put(None)
            def stderr():
                errors.extend(process.stderr)
            threading.Thread(target=read, daemon=True).start()
            threading.Thread(target=stderr, daemon=True).start()
            counter = 0
            def request(method, params=None, notification=False):
                nonlocal counter
                counter += 1
                payload = dict(jsonrpc="2.0", method=method, params=params or {})
                if not notification:
                    payload["id"] = counter
                process.stdin.write(json.dumps(payload) + "\n")
                process.stdin.flush()
                if notification:
                    return
                while True:
                    try:
                        line = incoming.get(timeout=20)
                    except queue.Empty:
                        self.fail("MCP response timed out: " + "".join(errors)[-3000:])
                    if line is None:
                        self.fail("MCP server exited: " + "".join(errors)[-3000:])
                    message = json.loads(line)
                    if message.get("id") == counter:
                        self.assertNotIn("error", message, str(message))
                        return message["result"]
            def call(name, arguments):
                result = request("tools/call", dict(name=name, arguments=arguments))
                self.assertFalse(result.get("isError"), str(result))
                if result.get("structuredContent") is not None:
                    data = result["structuredContent"]
                    return data["result"] if set(data) == {"result"} else data
                return json.loads(next(c["text"] for c in result["content"] if c["type"] == "text"))
            try:
                request("initialize", dict(protocolVersion="2025-11-25", capabilities={}, clientInfo=dict(name="correction-tests", version="0.2.0")))
                request("notifications/initialized", notification=True)
                self.assertEqual(len(request("tools/list")["tools"]), 13)
                task = call("correction_task", dict(session="A", action="begin"))
                spec = dict(key="response.order", title="先给结论", desired="先回答问题再解释",
                            checks=["第一段是否直接回答？"], evidence="这次先给结论", literal_avoid=["总而言之"])
                r = call("correction_record", dict(rule=spec, session="A", scope="task", task=task["task"]))
                self.assertEqual(call("correction_prepare", dict(session="A", task=task["task"]))["count"], 1)
                self.assertEqual(call("correction_prepare", dict(session="B"))["count"], 0)
                review = call("correction_review", dict(draft="总而言之", session="A", task=task["task"]))
                self.assertEqual(len(review["literal_warnings"]), 1)
                assessed = call("correction_assess", dict(run_id=review["run_id"], assessments=[dict(rule_id=r["id"], status="violated", evidence="开头使用了禁用表达")]))
                self.assertEqual(assessed["stage"], "needs_revision")
                self.assertEqual(call("correction_receipt", dict(run_id=review["run_id"]))["verification"], "model_self_report")
                call("correction_change", dict(rule_id=r["id"], revision=1, action="disable"))
                self.assertTrue(call("correction_change", dict(rule_id=r["id"], revision=2, action="undo"))["enabled"])
                self.assertEqual(call("correction_get", dict(rule_id=r["id"]))["revision"], 3)
                self.assertEqual(len(call("correction_history", dict(rule_id=r["id"]))), 3)
                call("correction_task", dict(session="A", task=task["task"], action="finish"))
                self.assertEqual(call("correction_prepare", dict(session="A", task=task["task"]))["count"], 0)
                self.assertEqual(call("correction_clear_session", dict(session="A"))["count"], 1)
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
