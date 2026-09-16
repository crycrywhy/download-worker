#!/usr/bin/env python3
"""run_offline_tests.py — 包装指南 §16 离线测试套件（不接触真 Windows Worker）。

用 vendored 的 mock_stream_server.py 起真 HTTP：它同时提供 /stream（含故障注入）与 /health，
所以「源站」「假 Worker」两条通路都能离线完整跑通；真机只剩一个几 MB 的点到点小样
（见 run_worker_tests.py）。

覆盖：§16.1 启动 / §16.2 tools/list / §16.3 普通下载 / §16.4 Worker 路径（离线替身）/
      §16.5 Resume / §16.6 connections / §16.7 Worker 不可用（指向死端口模拟，不动真隧道）/
      §16.8 参数错误。

用法: python3 tests/run_offline_tests.py [--keep]
"""
import csv
import hashlib
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from jrpc_client import MCPClient, tool_payload  # noqa: E402
import mock_stream_server as mss  # noqa: E402  (vendored mock；提供 /stream + /health)
from http.server import ThreadingHTTPServer  # noqa: E402

MIB = 1024 * 1024
KEEP = "--keep" in sys.argv


# ------------------------------------------------------------------ 基础设施

class Mock:
    """本地 mock：/stream + /health + 任意路径 Range 全在同一个 http server 上。"""

    def __init__(self, payload, delay_s=0.0):
        mss.PAYLOAD, mss.DELAY_S = payload, delay_s
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), mss.H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.srv.shutdown()


def make_payload(path, size):
    with open(path, "wb") as f:
        chunk = os.urandom(MIB)
        left = size
        while left > 0:
            n = min(len(chunk), left)
            f.write(chunk[:n])
            left -= n
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(MIB), b""):
            h.update(b)
    return h.hexdigest()


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(MIB), b""):
            h.update(b)
    return h.hexdigest()


def log_of(path):
    p = path + ".download.log"
    return open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else ""


# ------------------------------------------------------------------ 测试用例

def test_startup_and_tools_list(env, tmp):
    c = MCPClient(env=env).start()
    r = c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "t", "version": "0"}})["result"]
    assert r["protocolVersion"] == "2025-06-18", "应回显客户端协议版本"
    assert r["serverInfo"]["name"] == "downloader" and "tools" in r["capabilities"]
    c.notify("notifications/initialized")

    tools = c.request("tools/list")["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == ["download", "download_status", "worker_status", "alerts", "overview"], names
    dl = tools[0]
    assert dl["inputSchema"]["required"] == ["url", "output"]
    assert "outputSchema" in dl and dl["inputSchema"]["properties"]["via_worker"]["type"] == "boolean"
    # worker 参数只接受 auto/端口（不接受任意 URL，§15）
    assert dl["inputSchema"]["properties"]["worker"]["type"] == ["string", "integer"]
    assert tools[2]["inputSchema"]["properties"]["include_history"]["type"] == "boolean"
    assert tools[3]["inputSchema"]["properties"]["ensure"]["type"] == "boolean"
    assert tools[4]["inputSchema"]["properties"]["refresh"]["type"] == "boolean"
    # 无 stdout 污染：整个会话每行都能被 json.loads（由 client 断言），再核 stderr 有启动行
    c.close()
    assert any("started v" in l for l in c.stderr), "stderr 应有启动横幅"
    return ("initialize 回显协议版本；tools/list = download/download_status/worker_status/"
            "alerts/overview（5 个）；stdout 无污染")


def test_worker_status(env, tmp):
    payload = os.path.join(tmp, "p.bin")
    make_payload(payload, 2 * MIB)
    mock = Mock(payload)
    try:
        c = MCPClient(env={**env, "WINDL_WORKER_URL": mock.base}).start()
        c.request("initialize", {"protocolVersion": "2025-06-18"})
        ok = tool_payload(c.call_tool("worker_status"))
        assert ok["available"] is True and ok["worker"] == "mock-stream", ok
        c.close()

        dead = MCPClient(env={**env, "WINDL_WORKER_URL": "http://127.0.0.1:9", "WINDL_HEALTH_TIMEOUT": "1"}).start()
        dead.request("initialize", {"protocolVersion": "2025-06-18"})
        bad = tool_payload(dead.call_tool("worker_status"))
        assert bad["available"] is False and "error" in bad and bad["endpoint"].endswith(":9"), bad
        dead.close()
    finally:
        mock.stop()
    return "假 Worker → available=true；死端口 → available=false + 明确 error（不动真隧道）"


def test_invalid_args(env, tmp):
    c = MCPClient(env=env).start()
    c.request("initialize", {"protocolVersion": "2025-06-18"})
    cases = [
        ({"output": "/tmp/x.bin"}, "url is required"),
        ({"url": "ftp://x/y", "output": "/tmp/x.bin"}, "absolute http(s) URL"),
        ({"url": "http://h/x"}, "output is required"),
        ({"url": "http://h/x", "output": "/tmp/x.bin", "connections": 0}, "connections must be >= 1"),
        ({"url": "http://h/x", "output": "/tmp/x.bin", "chunk_size": 0}, "chunk_size must be >= 1"),
        ({"url": "http://h/x", "output": "/tmp/x.bin", "retries": -1}, "retries must be >= 0"),
        ({"url": "http://h/x", "output": "/tmp/x.bin", "via_worker": "yes"}, "via_worker must be a boolean"),
        ({"url": "http://h/x", "output": "/tmp/x.bin", "wait_seconds": 99999}, "wait_seconds must be <= 3600"),
        ({"url": "http://h/x", "output": "/tmp/x.bin", "max_bytes": 0}, "max_bytes must be >= 1"),
    ]
    for args, want in cases:
        resp = c.call_tool("download", args)
        res = resp.get("result", {})
        assert res.get("isError"), f"应当报参数错误: {args}"
        text = res["content"][0]["text"]
        assert want in text, f"{args} → 期望包含 {want!r}，实际: {text[:200]}"
    # 未知 tool → JSON-RPC 错误
    r = c.call_tool("download_all_the_things", {})
    assert r.get("error", {}).get("code") == -32602, r
    c.close()
    return f"{len(cases)} 个非法参数 + 未知 tool 全部给出清晰错误"


def test_direct_download(env, tmp):
    payload = os.path.join(tmp, "p.bin")
    out = os.path.join(tmp, "out.bin")
    md5 = make_payload(payload, 12 * MIB)
    mock = Mock(payload)
    try:
        c = MCPClient(env=env).start()
        c.request("initialize", {"protocolVersion": "2025-06-18"})
        t0 = time.time()
        r = tool_payload(c.call_tool("download", {
            "url": f"{mock.base}/origin.bin", "output": out, "md5": md5,
            "chunk_size": 4 * MIB, "via_worker": False, "wait_seconds": 120}))
        assert r["state"] == "completed" and r["size"] == 12 * MIB, r
        assert md5_of(out) == md5, "md5 不一致"
        assert not os.path.exists(out + ".download.json"), "完成后 sidecar 应删除"
        assert "==== 下载完成" in log_of(out), "downloader 日志应有完成标记"
        c.close()
    finally:
        mock.stop()
    return f"direct 12 MiB（3×4MiB 块）md5 逐字节一致，用时 {time.time()-t0:.1f}s，sidecar 已清理"


def test_connections_mapping(env, tmp):
    payload = os.path.join(tmp, "p.bin")
    md5 = make_payload(payload, 8 * MIB)
    mock = Mock(payload)
    try:
        c = MCPClient(env=env).start()
        c.request("initialize", {"protocolVersion": "2025-06-18"})
        outs = {}
        for conns in (1, 2):
            out = os.path.join(tmp, f"out_c{conns}.bin")
            tool_payload(c.call_tool("download", {
                "url": f"{mock.base}/origin.bin", "output": out, "md5": md5,
                "chunk_size": 4 * MIB, "connections": conns, "via_worker": False, "wait_seconds": 120}))
            log = log_of(out)
            outs[conns] = log
            assert md5_of(out) == md5
        assert "并发下载" not in outs[1], "connections=1 应保持串行（无并发调度日志）"
        assert "并发下载: 1 连接" not in outs[1]
        assert "并发下载: 2 连接" in outs[2], "connections=2 应进入并发调度"
        c.close()
    finally:
        mock.stop()
    return "connections=1 → 串行路径；connections=2 → 并发调度（与 CLI 行为一一对应）"


def test_resume(env, tmp):
    payload = os.path.join(tmp, "p.bin")
    out = os.path.join(tmp, "out.bin")
    md5 = make_payload(payload, 20 * MIB)          # 5 × 4MiB 块
    mock = Mock(payload, delay_s=1.2)              # 拉慢，保证能中途打断
    try:
        c = MCPClient(env=env).start()
        c.request("initialize", {"protocolVersion": "2025-06-18"})
        args = {"url": f"{mock.base}/origin.bin", "output": out, "md5": md5,
                "chunk_size": 4 * MIB, "connections": 1, "via_worker": False}
        r = tool_payload(c.call_tool("download", args))
        pid = r["pid"]
        # 等第 1 块落盘后强杀（模拟中断），确认 sidecar 保留
        t0 = time.time()
        while time.time() - t0 < 60:
            sidecar = out + ".download.json"
            if os.path.exists(sidecar):
                done = len(json.load(open(sidecar)).get("completed_chunks", []))
                if done >= 1:
                    break
            time.sleep(0.2)
        else:
            raise AssertionError("60s 内没等到第 1 块完成")
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
        assert os.path.exists(out + ".download.json"), "中断后 sidecar 必须保留"
        done = len(json.load(open(out + ".download.json"))["completed_chunks"])
        assert 1 <= done < 5, f"应中断在 1..4 块之间，实际 {done}"
        st = tool_payload(c.call_tool("download_status", {"output": out}))
        assert st["state"] == "incomplete" and st["completed_chunks"] == done, st
        # 重跑同参数 → 自动续传
        r2 = tool_payload(c.call_tool("download", {**args, "wait_seconds": 120}))
        assert r2["state"] == "completed", r2
        log = log_of(out)
        assert f"断点恢复: {done}/5 块已完成, 跳过" in log, f"应跳过已完成块；日志:\n{log[-400:]}"
        assert md5_of(out) == md5
        assert not os.path.exists(out + ".download.json")
        c.close()
    finally:
        mock.stop()
    return f"中途 SIGKILL：sidecar 保留（{done}/5）→ 重跑跳过已完成块 → 终验 md5 一致"


def test_max_bytes(env, tmp):
    payload = os.path.join(tmp, "p.bin")
    out = os.path.join(tmp, "out.bin")
    make_payload(payload, 8 * MIB)
    mock = Mock(payload)
    try:
        c = MCPClient(env=env).start()
        c.request("initialize", {"protocolVersion": "2025-06-18"})
        r = tool_payload(c.call_tool("download", {
            "url": f"{mock.base}/origin.bin", "output": out, "chunk_size": 4 * MIB,
            "max_bytes": 5 * MIB, "via_worker": False, "wait_seconds": 60}))
        assert r["state"] == "completed" and os.path.getsize(out) == 5 * MIB, r
        c.close()
    finally:
        mock.stop()
    return "max_bytes=5MiB → 截断下载（测试用能力，映射 --max-bytes）"


def test_via_worker_path(env, tmp):
    payload = os.path.join(tmp, "p.bin")
    out = os.path.join(tmp, "out.bin")
    md5 = make_payload(payload, 8 * MIB)
    mock = Mock(payload)
    try:
        c = MCPClient(env={**env, "WINDL_WORKER_URL": mock.base}).start()
        c.request("initialize", {"protocolVersion": "2025-06-18"})
        r = tool_payload(c.call_tool("download", {
            "url": "http://origin.invalid/data.bin", "output": out, "md5": md5,
            "chunk_size": 4 * MIB, "via_worker": True, "wait_seconds": 120}))
        assert r["state"] == "completed" and r["via_worker"] is True, r
        assert r["worker_endpoint"] == mock.base
        assert md5_of(out) == md5
        # 命令必须带 --worker 且不带 --direct
        assert "--worker" in r["command"] and "--direct" not in r["command"]
        c.close()
    finally:
        mock.stop()
    return "via_worker=true → 走 Worker /stream 通路（离线替身），命令含 --worker 无 --direct"


def test_worker_unavailable_no_fallback(env, tmp):
    out = os.path.join(tmp, "out.bin")
    c = MCPClient(env={**env, "WINDL_WORKER_URL": "http://127.0.0.1:9", "WINDL_HEALTH_TIMEOUT": "1"}).start()
    c.request("initialize", {"protocolVersion": "2025-06-18"})
    resp = c.call_tool("download", {"url": "http://origin.invalid/data.bin", "output": out,
                                    "via_worker": True, "wait_seconds": 0})
    res = resp["result"]
    assert res.get("isError"), "Worker 不可用时必须报错"
    text = res["content"][0]["text"]
    assert "unavailable" in text and "via_worker=false" in text, text
    assert not os.path.exists(out), "不得偷偷回退成 direct 下载"
    c.close()
    return "via_worker=true + Worker 不可用 → 明确报错并提示 via_worker=false，不回退"


def test_duplicate_output_guard(env, tmp):
    payload = os.path.join(tmp, "p.bin")
    out = os.path.join(tmp, "out.bin")
    make_payload(payload, 8 * MIB)
    mock = Mock(payload, delay_s=2.0)
    try:
        c = MCPClient(env=env).start()
        c.request("initialize", {"protocolVersion": "2025-06-18"})
        r = tool_payload(c.call_tool("download", {
            "url": f"{mock.base}/origin.bin", "output": out, "chunk_size": 4 * MIB,
            "connections": 1, "via_worker": False}))
        assert r["state"] == "running"
        resp = c.call_tool("download", {"url": f"{mock.base}/origin.bin", "output": out,
                                        "chunk_size": 4 * MIB, "via_worker": False})
        text = resp["result"]["content"][0]["text"]
        assert resp["result"].get("isError") and "already running" in text, text
        os.kill(r["pid"], signal.SIGKILL)
        c.close()
    finally:
        mock.stop()
    return "同一 output 并发提交被拒（防止两个进程写同一文件）"


# ------------------------------------------------------------------ v2 fixtures

def free_port():
    """拿一个当下一定空闲的端口（用作「注册表里但没人听」的死口）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_scripts_dir(tmp, alive_port, dead_port, alive_label="stub-A",
                     pool_stub=None, alert_stub=None, collector_stub=None,
                     journal_events=None, ledger_rows=None, manifests=None):
    """搭一个「假 scripts 目录」：注册表 + 哨兵状态 + 告警流水 + 台账 CSV + state/。

    只放本 server 会去读的那些文件；返回 env 覆盖（WINDL_SCRIPTS_DIR 指过去即可）。
    默认路径全部走 server 自身的推导（<scripts>/workers.json、<scripts>/state/...），
    所以这同时验证了默认路径规则。
    """
    scripts = os.path.join(tmp, "scripts")
    state = os.path.join(scripts, "state")
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(scripts, "workers.json"), "w") as f:
        json.dump({"_comment": "fixture", "scan_range": [8766, 8775],
                   "workers": {str(alive_port): {"label": alive_label, "device": "stub-dev-1",
                                                 "enabled": True},
                               str(dead_port): {"label": "stub-B", "device": "stub-dev-2",
                                                "enabled": True},
                               str(dead_port + 1): {"label": "stub-disabled", "enabled": False}}},
                  f)
    with open(os.path.join(scripts, "worker_status.json"), "w") as f:
        json.dump({"updated_utc8": "2026-09-14 06:00:00",
                   "workers": {str(dead_port): {"was_down": True, "ok": False, "label": "stub-B",
                                                "enabled": True, "down_since": "2026-09-14 04:00:00",
                                                "fail_streak": 7, "last_ok_utc8": "2026-09-14 03:59:00",
                                                "last_fail_utc8": "2026-09-14 06:00:00",
                                                "note": "fixture down"}},
                   "events": {str(dead_port): {"down_last": "2026-09-14 04:00:00",
                                               "up_last": "2026-09-14 03:00:00"}}}, f)
    if pool_stub:
        with open(os.path.join(scripts, "worker_pool.py"), "w") as f:
            f.write(pool_stub)
    if alert_stub:
        with open(os.path.join(scripts, "worker_alert.py"), "w") as f:
            f.write(alert_stub)
    if collector_stub:
        with open(os.path.join(scripts, "status_collector.py"), "w") as f:
            f.write(collector_stub)
    if journal_events:
        with open(os.path.join(state, "worker_alerts.jsonl"), "w") as f:
            for e in journal_events:
                f.write(json.dumps(e) + "\n")
    if ledger_rows:
        with open(os.path.join(state, "download_status.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["species_name", "taxid", "genus", "class", "verdict",
                                              "status", "path", "size_gb", "updated", "notes"])
            w.writeheader()
            w.writerows(ledger_rows)
    for name, content in (manifests or {}).items():
        with open(os.path.join(state, name), "w") as f:
            f.write(content)
    return {"WINDL_SCRIPTS_DIR": scripts}


EVENTS = [
    {"ts_utc8": "2026-09-14 03:00:00", "event": "UP", "port": 8766, "label": "stub-A",
     "error": "", "down_since": None, "device": "stub-dev-1", "device_state": "active",
     "listener": "listening", "others": "8767=up"},
    {"ts_utc8": "2026-09-14 04:00:00", "event": "DOWN", "port": 8767, "label": "stub-B",
     "error": "connection refused", "down_since": "2026-09-14 04:00:00", "device": "stub-dev-2",
     "device_state": "offline", "listener": "gone", "others": "8766=up"},
    {"ts_utc8": "2026-09-14 06:00:00", "event": "UP", "port": 8767, "label": "stub-B",
     "error": "", "down_since": None, "device": "stub-dev-2", "device_state": "active",
     "listener": "listening", "others": "8766=up"},
]

LEDGER = [
    {"species_name": "Danio rerio", "taxid": "7955", "genus": "Danio", "class": "Actinopteri",
     "verdict": "pass", "status": "DONE", "path": "/data/x/Danio_rerio.bam", "size_gb": "8.0",
     "updated": "2026-09-13 10:00", "notes": ""},
    {"species_name": "Danio aesculapii", "taxid": "27699", "genus": "Danio", "class": "Actinopteri",
     "verdict": "", "status": "QUEUED", "path": "", "size_gb": "", "updated": "2026-09-14 06:00",
     "notes": ""},
    {"species_name": "Gadus morhua", "taxid": "8049", "genus": "Gadus", "class": "Actinopteri",
     "verdict": "", "status": "REPAIRING", "path": "/data/x/Gadus_morhua.bam", "size_gb": "210.0",
     "updated": "2026-09-14 05:00", "notes": "2 holes"},
    {"species_name": "Gasterosteus aculeatus", "taxid": "69293", "genus": "Gasterosteus",
     "class": "Actinopteri", "verdict": "pass", "status": "REPAIRED", "path": "/data/x/Gas.bam",
     "size_gb": "190.0", "updated": "2026-09-12 09:00", "notes": ""},
    {"species_name": "Hippocampus comes", "taxid": "109280", "genus": "Hippocampus",
     "class": "Actinopteri", "verdict": "pass", "status": "REPAIRED", "path": "/data/x/Hip.bam",
     "size_gb": "170.0", "updated": "2026-09-11 09:00", "notes": ""},
    {"species_name": "Ictalurus punctatus", "taxid": "7998", "genus": "Ictalurus",
     "class": "Actinopteri", "verdict": "", "status": "REPAIR_FAIL", "path": "/data/x/Ict.bam",
     "size_gb": "175.0", "updated": "2026-09-10 09:00", "notes": "3 holes remain"},
]

POOL_STUB_OK = """#!/usr/bin/env python3
import json, sys
print(json.dumps({"registry": "fixture", "alive": [8766],
                  "ports": {"8766": {"ok": True, "port": 8766, "label": "stub-A",
                                     "latency_ms": 12.5, "error": "", "enabled": True},
                            "8767": {"ok": False, "port": 8767, "label": "stub-B",
                                     "latency_ms": None, "error": "connection refused",
                                     "enabled": True}}}))
"""

POOL_STUB_FAIL = "#!/usr/bin/env python3\nimport sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n"

ALERT_STUB = """#!/usr/bin/env python3
print("alert sentinel: running (fixture)")
"""

COLLECTOR_STUB = """#!/usr/bin/env python3
print("collector: fixture refresh ok")
"""


def test_worker_pool_status_and_pick(env, tmp):
    payload = os.path.join(tmp, "p.bin")
    out = os.path.join(tmp, "out.bin")
    md5 = make_payload(payload, 8 * MIB)
    mock = Mock(payload)
    dead = free_port()
    unregistered = free_port()
    try:
        ov = make_scripts_dir(tmp, alive_port=mock.port, dead_port=dead, journal_events=EVENTS)
        c = MCPClient(env={**env, **ov}).start()
        c.request("initialize", {"protocolVersion": "2025-06-18"})

        st = tool_payload(c.call_tool("worker_status"))
        assert st["mode"] == "pool" and st["available"] is True, st
        assert st["source"] == "probe", f"无 worker_pool.py 时应退化为薄探活: {st['source']}"
        assert st["alive"] == [mock.port], st["alive"]
        assert st["auto_endpoint"] == f"http://127.0.0.1:{mock.port}"
        by_port = {w["port"]: w for w in st["workers"]}
        assert by_port[mock.port]["ok"] is True and by_port[mock.port]["label"] == "stub-A"
        assert by_port[dead]["ok"] is False and by_port[dead]["error"], by_port[dead]
        # enabled:false 的口不进池（fixture 里登记了 dead+1）
        assert (dead + 1) not in by_port, "注册表 enabled:false 的口不应被探测"
        # 哨兵历史合并进来
        assert by_port[dead]["down_since"] == "2026-09-14 04:00:00", by_port[dead]
        assert by_port[dead]["up_last"] == "2026-09-14 03:00:00" and by_port[dead]["fail_streak"] == 7
        assert by_port[mock.port]["down_since"] is None, "活口无掉线历史（字段恒在，值为 None）"
        assert by_port[mock.port]["up_last"] is None and st["history_updated"] == "2026-09-14 06:00:00"

        no_hist = tool_payload(c.call_tool("worker_status", {"include_history": False}))
        assert all("down_since" not in w for w in no_hist["workers"]), "include_history=false 应去掉历史字段"

        # worker=auto → 挑活口（8 MiB 经 mock Worker /stream 通路）
        r = tool_payload(c.call_tool("download", {
            "url": "http://origin.invalid/data.bin", "output": out, "md5": md5, "chunk_size": 4 * MIB,
            "worker": "auto", "wait_seconds": 120}))
        assert r["state"] == "completed" and md5_of(out) == md5, r
        assert r["worker_endpoint"] == f"http://127.0.0.1:{mock.port}"
        assert r["worker_source"] == f"auto:{mock.port}", r["worker_source"]

        # worker=<注册表里的死口> → 明确报错，不回退
        resp = c.call_tool("download", {"url": "http://origin.invalid/x", "output": out + ".2",
                                       "worker": dead})
        assert resp["result"].get("isError") and "not reachable" in resp["result"]["content"][0]["text"]
        # worker=<不在注册表的端口> → 拒绝
        resp = c.call_tool("download", {"url": "http://origin.invalid/x", "output": out + ".3",
                                       "worker": unregistered})
        text = resp["result"]["content"][0]["text"]
        assert resp["result"].get("isError") and "not an enabled port" in text, text
        # worker=<任意 URL> → 拒绝（§15：不提供 arbitrary Worker URL 能力）
        resp = c.call_tool("download", {"url": "http://origin.invalid/x", "output": out + ".4",
                                       "worker": "http://192.0.2.10:8765"})
        text = resp["result"]["content"][0]["text"]
        assert resp["result"].get("isError") and "arbitrary worker URLs are not accepted" in text, text
        # via_worker=false 与 worker 互斥
        resp = c.call_tool("download", {"url": "http://origin.invalid/x", "output": out + ".5",
                                       "via_worker": False, "worker": "auto"})
        assert resp["result"].get("isError") and "cannot be combined" in resp["result"]["content"][0]["text"]
        c.close()
    finally:
        mock.stop()
    return (f"池模式：{mock.port} 活 / {dead} 死 / enabled:false 不探测；哨兵历史合并；"
            "auto 选活口下载成功；死口与未注册口与任意 URL 三种非法 worker 全部拒绝")


def test_worker_pool_prefers_pool_script(env, tmp):
    dead = free_port()
    ov = make_scripts_dir(tmp, alive_port=dead + 1, dead_port=dead, pool_stub=POOL_STUB_FAIL)
    c = MCPClient(env={**env, **ov}).start()
    c.request("initialize", {"protocolVersion": "2025-06-18"})
    st = tool_payload(c.call_tool("worker_status"))
    assert st["source"] == "probe" and st["probe_error"], f"池脚本失败应记 probe_error 并退化: {st}"
    c.close()

    ov = make_scripts_dir(tmp, alive_port=dead + 1, dead_port=dead, pool_stub=POOL_STUB_OK)
    c = MCPClient(env={**env, **ov}).start()
    c.request("initialize", {"protocolVersion": "2025-06-18"})
    st = tool_payload(c.call_tool("worker_status"))
    assert st["source"] == "worker_pool.py", f"有池脚本时应优先用它: {st['source']}"
    assert st["alive"] == [8766] and st["workers"][0]["label"] == "stub-A"
    c.close()
    return "有 worker_pool.py 时优先调用它（池的唯一事实来源）；它失败则退化薄探活并记 probe_error"


def test_alerts(env, tmp):
    ov = make_scripts_dir(tmp, alive_port=free_port(), dead_port=free_port(),
                          journal_events=EVENTS, alert_stub=ALERT_STUB)
    c = MCPClient(env={**env, **ov}).start()
    c.request("initialize", {"protocolVersion": "2025-06-18"})
    a = tool_payload(c.call_tool("alerts", {"limit": 2}))
    assert a["file_exists"] is True and a["count"] == 2, a
    assert a["events"][-1]["event"] == "UP" and a["events"][0]["event"] == "DOWN", a["events"]
    assert a["events"][0]["listener"] == "gone" and a["events"][0]["device_state"] == "offline"
    # ensure=true → 跑告警自检（幂等动作），输出带回
    a2 = tool_payload(c.call_tool("alerts", {"limit": 1, "ensure": True}))
    assert a2["ensure_rc"] == 0 and "alert sentinel: running" in a2["ensure_output"], a2
    c.close()

    # 无流水文件 → 不报错，给 hint
    c = MCPClient(env={**env, **ov, "WINDL_ALERTS_FILE": os.path.join(tmp, "nope.jsonl")}).start()
    c.request("initialize", {"protocolVersion": "2025-06-18"})
    a3 = tool_payload(c.call_tool("alerts"))
    assert a3["file_exists"] is False and a3["count"] == 0 and "hint" in a3, a3
    c.close()

    # 没有告警脚本却要求 ensure → 明确报错
    c = MCPClient(env={**env, "WINDL_SCRIPTS_DIR": os.path.join(PROJECT, "vendor")}).start()
    c.request("initialize", {"protocolVersion": "2025-06-18"})
    resp = c.call_tool("alerts", {"ensure": True})
    assert resp["result"].get("isError") and "alert script not found" in resp["result"]["content"][0]["text"]
    c.close()
    return "读流水（尾部 limit 条 + listener 诊断字段）；ensure=true 跑幂等自检并带回输出；缺文件给 hint；缺脚本明确报错"


def test_overview(env, tmp):
    ov = make_scripts_dir(tmp, alive_port=free_port(), dead_port=free_port(), ledger_rows=LEDGER,
                          manifests={"repair_manifest_demo.tsv": "a\tb\nc\td\n"},
                          collector_stub=COLLECTOR_STUB)
    c = MCPClient(env={**env, **ov}).start()
    c.request("initialize", {"protocolVersion": "2025-06-18"})
    o = tool_payload(c.call_tool("overview"))
    assert o["total"] == 6 and o["returned"] == 6, o
    assert o["counts"] == {"DONE": 1, "QUEUED": 1, "REPAIRING": 1, "REPAIRED": 2, "REPAIR_FAIL": 1}, o["counts"]
    assert o["integrity"]["repaired"] == 2 and o["integrity"]["repair_fail"] == 1
    assert o["integrity"]["repairing"] == 1 and o["updated_utc8"], o["integrity"]
    man = [m for m in o["integrity"]["manifests"] if m["file"] == "repair_manifest_demo.tsv"]
    assert man and man[0]["entries"] == 2, o["integrity"]["manifests"]
    # 过滤：counts 始终覆盖全表
    o2 = tool_payload(c.call_tool("overview", {"state": "repaired"}))
    assert o2["matched"] == 2 and o2["counts"]["DONE"] == 1, o2
    o3 = tool_payload(c.call_tool("overview", {"genus": "Danio"}))
    assert o3["matched"] == 2 and {r["status"] for r in o3["rows"]} == {"DONE", "QUEUED"}, o3["rows"]
    o4 = tool_payload(c.call_tool("overview", {"species": "gasterosteus"}))
    assert o4["matched"] == 1 and o4["rows"][0]["taxid"] == "69293", o4["rows"]
    # limit + 截断提示
    o5 = tool_payload(c.call_tool("overview", {"limit": 2}))
    assert o5["returned"] == 2 and o5["matched"] == 6 and "hint" in o5, o5
    assert o["updated_utc8"], o
    # refresh=true → 先跑 collector（幂等）
    o6 = tool_payload(c.call_tool("overview", {"refresh": True, "state": "DONE"}))
    assert o6["refresh_rc"] == 0 and "fixture refresh ok" in o6["refresh_output"] and o6["matched"] == 1, o6
    c.close()

    # 台账不存在 → 结构化提示而非崩溃
    c = MCPClient(env={**env, **ov, "WINDL_STATUS_CSV": os.path.join(tmp, "nope.csv")}).start()
    c.request("initialize", {"protocolVersion": "2025-06-18"})
    o7 = tool_payload(c.call_tool("overview"))
    assert "error" in o7 and "hint" in o7, o7
    # refresh 但缺 collector → 明确报错
    c.close()
    ov2 = {"WINDL_SCRIPTS_DIR": os.path.join(PROJECT, "vendor")}
    c = MCPClient(env={**env, **ov2}).start()
    c.request("initialize", {"protocolVersion": "2025-06-18"})
    resp = c.call_tool("overview", {"refresh": True})
    assert resp["result"].get("isError") and "collector script not found" in resp["result"]["content"][0]["text"]
    c.close()
    return "台账计数/过滤/截断提示/完整性 manifest 全部正确；refresh 跑幂等 collector；缺 CSV 与缺 collector 各有清晰反馈"


def test_download_status_with_ledger(env, tmp):
    out = os.path.join(tmp, "Gadus_morhua.bam")
    sub = os.path.join(tmp, "sub")
    os.makedirs(sub)
    rows = [dict(r) for r in LEDGER]
    rows[2]["path"] = out                                   # Gadus 行 → 本次输出（路径全等）
    rows[0]["path"] = os.path.join(sub, "Danio_rerio.bam")   # Danio 行 → 落在目录之下
    ov = make_scripts_dir(tmp, alive_port=free_port(), dead_port=free_port(), ledger_rows=rows)
    with open(out, "wb") as f:
        f.write(b"\0" * 4096)                      # 造一个「已落地」的假文件
    with open(out + ".download.json", "w") as f:   # 有 sidecar ⇒ 未完成（可续传）
        json.dump({"url": "http://x/y", "size": 8192, "chunk_size": 4096,
                   "completed_chunks": [0], "output": out}, f)
    c = MCPClient(env={**env, **ov}).start()
    c.request("initialize", {"protocolVersion": "2025-06-18"})
    st = tool_payload(c.call_tool("download_status", {"output": out, "with_ledger": True}))
    assert st["state"] == "incomplete" and st["completed_chunks"] == 1 and st["total_chunks"] == 2, st
    assert st["ledger"]["matched"] is True, st["ledger"]
    assert st["ledger"]["row"]["status"] == "REPAIRING" and st["ledger"]["row"]["genus"] == "Gadus"
    # output 传目录 → 命中该目录下的行
    st2 = tool_payload(c.call_tool("download_status", {"output": sub, "with_ledger": True}))
    assert st2["ledger"]["matched"] is True and st2["ledger"]["row"]["taxid"] == "7955", st2["ledger"]
    # 既不相等也不在目录下 → matched=false（不抓错行）
    st3 = tool_payload(c.call_tool("download_status", {"output": os.path.join(tmp, "other.bin"),
                                                      "with_ledger": True}))
    assert st3["state"] == "not_started" and st3["ledger"]["matched"] is False, st3
    c.close()
    return ("download_status 接入台账：路径全等/目录包含两种命中；不匹配时 matched=false"
            "（宁可空着也不挂错行）")


def test_config_layer(env, tmp):
    """配置链：env > 配置文件 > 默认；坏配置文件不影响启动。"""
    payload = os.path.join(tmp, "p.bin")
    out = os.path.join(tmp, "out.bin")
    make_payload(payload, 2 * MIB)
    mock = Mock(payload)
    scripts = os.path.join(tmp, "scripts")
    os.makedirs(scripts)
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w") as f:
        json.dump({"worker_url": mock.base, "scripts_dir": scripts,
                   "health_timeout": 2, "start_grace": 2}, f)
    try:
        # env 里去掉 worker 相关项 → 值应来自配置文件
        bare = {k: v for k, v in env.items()
                if k not in ("WINDL_WORKER_URL", "WINDL_HEALTH_TIMEOUT", "WINDL_SCRIPTS_DIR")}
        c = MCPClient(env={**bare, "WINDL_CONFIG": cfg_path}).start()
        r = c.request("initialize", {"protocolVersion": "2025-06-18"})["result"]
        assert r["serverInfo"]["version"].startswith("2."), r["serverInfo"]
        st = tool_payload(c.call_tool("worker_status"))
        assert st["available"] is True and st["endpoint"] == mock.base, st
        c.close()

        # env 覆盖配置文件（指向死端口 → 不可用）
        c = MCPClient(env={**bare, "WINDL_CONFIG": cfg_path,
                           "WINDL_WORKER_URL": "http://127.0.0.1:9"}).start()
        c.request("initialize", {"protocolVersion": "2025-06-18"})
        st = tool_payload(c.call_tool("worker_status"))
        assert st["available"] is False and st["endpoint"].endswith(":9"), st
        c.close()

        # 坏配置文件：不崩、照常服务（只是忽略）
        bad = os.path.join(tmp, "bad.json")
        with open(bad, "w") as f:
            f.write("{not json at all")
        c = MCPClient(env={**bare, "WINDL_CONFIG": bad}).start()
        c.request("initialize", {"protocolVersion": "2025-06-18"})
        st = tool_payload(c.call_tool("worker_status"))
        assert st["mode"] == "endpoint" and st["endpoint"] == "http://127.0.0.1:8766", st
        assert any("config file" in l for l in c.stderr), "坏配置文件应在 stderr 留一行提示"
        c.close()
        return "配置文件提供 worker_url/scripts_dir；env 覆盖文件；坏文件忽略且在 stderr 提示"
    finally:
        mock.stop()


TESTS = [
    ("§16.1/16.2 启动 + tools/list", test_startup_and_tools_list),
    ("§16.10 worker_status（可用/不可用）", test_worker_status),
    ("§16.8 非法参数", test_invalid_args),
    ("§16.3 direct 下载", test_direct_download),
    ("§16.6 connections 映射", test_connections_mapping),
    ("§16.5 resume 断点续传", test_resume),
    ("max_bytes 截断", test_max_bytes),
    ("§16.4 via_worker 通路（离线替身）", test_via_worker_path),
    ("§16.7 Worker 不可用不回退", test_worker_unavailable_no_fallback),
    ("重复 output 保护", test_duplicate_output_guard),
    ("§16.11 worker 池状态 + 选口", test_worker_pool_status_and_pick),
    ("§16.11 池状态优先用 worker_pool.py", test_worker_pool_prefers_pool_script),
    ("§16.12 alerts 告警流水 + ensure", test_alerts),
    ("§16.13 overview 台账 + 完整性", test_overview),
    ("§16.14 download_status 接入台账", test_download_status_with_ledger),
    ("§16.15 配置链（env > 文件 > 默认）", test_config_layer),
]


def main():
    base = tempfile.mkdtemp(prefix="windl_offline_")
    print(f"[离线测试] 工作目录 {base}" + ("（--keep：保留）" if KEEP else ""))
    # 离线保证：默认把 Worker 端点指向死端口，任何测试都不会碰真 Worker；用 mock 的用例自行覆盖。
    # 同时把部署配置 / scripts 目录 / 状态目录隔离掉，离线测试只读临时目录里的 fixture。
    env = {"WINDL_DOWNLOADER": os.path.join(PROJECT, "vendor", "linux_downloader.py"),
           "WINDL_PYTHON": sys.executable, "WINDL_START_GRACE": "2",
           "WINDL_WORKER_URL": "http://127.0.0.1:9", "WINDL_HEALTH_TIMEOUT": "1",
           "WINDL_CONFIG": os.path.join(base, "no_such_config.json"),
           "WINDL_SCRIPTS_DIR": os.path.join(PROJECT, "vendor")}
    # 注：不设 WINDL_STATE_DIR / WINDL_STATUS_CSV / WINDL_ALERTS_FILE ——
    # 让用例通过 WINDL_SCRIPTS_DIR 覆盖后仍走「由 scripts_dir 推导」那条默认路径。
    results, failed = [], 0
    for name, fn in TESTS:
        tmp = tempfile.mkdtemp(dir=base)
        t0 = time.time()
        try:
            detail = fn(env, tmp)
            results.append((name, "PASS", f"{time.time()-t0:.1f}s", detail))
            print(f"  PASS  {name}  ({time.time()-t0:.1f}s)\n        {detail}")
        except Exception as e:
            failed += 1
            results.append((name, "FAIL", f"{time.time()-t0:.1f}s", f"{type(e).__name__}: {e}"))
            print(f"  FAIL  {name}  ({time.time()-t0:.1f}s)\n        {type(e).__name__}: {e}")
    print("\n==== 汇总 ====")
    for name, st, dt, _ in results:
        print(f"  {st}  {name}  ({dt})")
    print(f"  {len(results)-failed}/{len(results)} PASS")
    if not KEEP and failed == 0:
        shutil.rmtree(base, ignore_errors=True)
        print(f"  已清理 {base}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
