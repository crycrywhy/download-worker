#!/usr/bin/env python3
"""jrpc_client.py — 最小 stdio JSON-RPC 客户端，用于驱动 mcp_server.py 做端到端测试。

这是「MCP client 侧」的最小实现（测试用，不属于交付的 server 本体）：
按 MCP stdio 传输逐行收发 JSON-RPC，任何 stdout 污染都会在这里暴露成解析错误。

库用法：
    from jrpc_client import MCPClient
    c = MCPClient(env={"WINDL_WORKER_URL": "http://127.0.0.1:9"}).start()
    c.request("initialize", {...}); c.notify("notifications/initialized")
    c.request("tools/list"); c.request("tools/call", {"name": "worker_status", "arguments": {}})
    c.close()
"""
import json
import os
import select
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
DEFAULT_SERVER = os.path.join(PROJECT, "mcp_server.py")


class MCPClient:
    def __init__(self, env=None, server=DEFAULT_SERVER, python=None):
        self.cmd = [python or sys.executable, server]
        self.env = dict(os.environ)
        if env:
            self.env.update(env)
        self.proc = None
        self._id = 0
        self.stderr = []
        self.notifications = []
        self._lock = threading.Lock()

    def start(self, timeout=10):
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=self.env)
        self._t = threading.Thread(target=self._drain_stderr, daemon=True)
        self._t.start()
        time.sleep(0.3)  # 给启动失败留出暴露窗口
        if self.proc.poll() is not None:
            raise RuntimeError(f"server exited immediately rc={self.proc.returncode}\n"
                               + "".join(self.stderr))
        return self

    def _drain_stderr(self):
        for raw in self.proc.stderr:
            with self._lock:
                self.stderr.append(raw.decode("utf-8", "replace").rstrip("\n"))

    def _send(self, msg):
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode()
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def request(self, method, params=None, timeout=120):
        with self._lock:
            self._id += 1
            rid = self._id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"no response to {method} (id={rid}) within {timeout}s")
            r, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if not r:
                continue
            line = self.proc.stdout.readline()
            if not line:
                raise EOFError(f"server closed stdout while waiting for {method}")
            try:
                resp = json.loads(line.decode("utf-8"))
            except Exception as e:
                raise AssertionError(f"stdout pollution (non-JSON line): {line[:200]!r} ({e})")
            if resp.get("id") == rid and ("result" in resp or "error" in resp):
                return resp
            self.notifications.append(resp)  # 通知 / 其他响应

    def call_tool(self, name, arguments=None, timeout=120):
        return self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)

    def close(self):
        try:
            if self.proc and self.proc.stdin:
                self.proc.stdin.close()
            if self.proc:
                self.proc.wait(timeout=5)
        except Exception:
            if self.proc:
                self.proc.kill()
        finally:
            self.stderr_text = "\n".join(self.stderr)


def tool_payload(resp):
    """从 tools/call 响应里取结构化结果；isError 时抛错。"""
    if "error" in resp:
        raise AssertionError(f"JSON-RPC error: {resp['error']}")
    res = resp["result"]
    if res.get("isError"):
        raise AssertionError("tool error: " + res["content"][0]["text"])
    return res.get("structuredContent") or json.loads(res["content"][0]["text"])


if __name__ == "__main__":
    # 手动冒烟：python3 jrpc_client.py ping
    c = MCPClient(env=os.environ).start()
    c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "jrpc-smoke", "version": "0"}})
    c.notify("notifications/initialized")
    print(json.dumps(c.request("tools/list")["result"]["tools"][0]["name"], ensure_ascii=False))
    print(json.dumps(c.call_tool("worker_status"), ensure_ascii=False, indent=2))
    c.close()
