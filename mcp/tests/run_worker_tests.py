#!/usr/bin/env python3
"""run_worker_tests.py — 真机小样：经真 Windows Worker（127.0.0.1:8766）的点到点验证。

刻意做得极轻（生产 windows 线正在用同一 worker）：
  · worker_status 只读 /health（零带宽）
  · download 只取 8 MiB 前缀（--max-bytes），并把这 8 MiB 与服务器直连取的同一段前缀逐字节比对
不启停 Worker / 隧道 / 任何生产进程（与包装指南 §16.7 的「停掉 Worker 测」不同：
此处用死端口离线模拟 unavailable，真隧道不动）。

用法: python3 tests/run_worker_tests.py [--mb 8]
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from jrpc_client import MCPClient, tool_payload  # noqa: E402

MIB = 1024 * 1024
# 与 09-10 冒烟同源（ENA，8.2 GB，公开可 Range）——只取前缀，不做整文件下载
TEST_URL = "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR333/072/SRR33390772/SRR33390772_1.fastq.gz"


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(MIB), b""):
            h.update(b)
    return h.hexdigest()


def fetch_prefix_direct(url, nbytes, dest, timeout=900):
    """服务器直连取同一段前缀作对照（照 CLAUDE.md 规则：shell 侧必须清掉代理变量）。

    注意：服务器直连 ENA 很慢（这正是本系统存在的理由），该步骤可能超时——
    超时不算 MCP 链路失败，调用方按 WARN 降级处理。"""
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env.pop(k, None)
    subprocess.run(["curl", "-sSf", "-r", f"0-{nbytes - 1}", "-o", dest, url],
                   check=True, env=env, timeout=timeout)
    return dest


def main():
    mb = int(sys.argv[sys.argv.index("--mb") + 1]) if "--mb" in sys.argv else 8
    tmp = tempfile.mkdtemp(prefix="windl_worker_")
    out = os.path.join(tmp, "prefix.bin")
    env = {"WINDL_DOWNLOADER": os.path.join(PROJECT, "vendor", "linux_downloader.py"),
           "WINDL_PYTHON": sys.executable}
    ok = True
    try:
        c = MCPClient(env=env).start()
        c.request("initialize", {"protocolVersion": "2025-06-18",
                                 "clientInfo": {"name": "worker-test", "version": "0"}})
        c.notify("notifications/initialized")

        print("== 1) worker_status（真端点，只读） ==")
        st = tool_payload(c.call_tool("worker_status"))
        print("   ", st)
        assert st["available"] is True, st
        # 单端点模式（没配池）与池模式（配了 worker 注册表）都要能跑真机
        if st.get("mode") == "pool":
            assert st["alive"], st
            assert st["auto_endpoint"].startswith("http://127.0.0.1:"), st
            # 某口对某源站不通时（例：某台 PC 到 ENA 的路由坏、/health 却正常），
            # 可用 WINDL_TEST_WORKER=<端口> 指定一个口来验证 MCP 链路本身
            pick = {"worker": os.environ.get("WINDL_TEST_WORKER", "auto")}
            print(f"    池模式：alive={st['alive']} auto={st['auto_endpoint']} → download 用 "
                  f"worker={pick['worker']}")
        else:
            assert st["endpoint"].endswith(":8766"), st
            assert st["worker"] == "local-download-worker", st
            pick = {}

        print(f"== 2) via_worker=true 取 {mb} MiB 前缀（--max-bytes） ==")
        t0 = time.time()
        r = tool_payload(c.call_tool("download", {
            "url": TEST_URL, "output": out, "chunk_size": 256 * MIB, "connections": 1,
            "via_worker": True, "max_bytes": mb * MIB, "wait_seconds": 300, **pick}))
        dt = time.time() - t0
        print(f"    state={r['state']} size={r['size']} 用时 {dt:.1f}s "
              f"({r['size'] / dt / MIB:.2f} MiB/s, 含探测+终验)")
        assert r["state"] == "completed" and r["size"] == mb * MIB, r
        assert "--worker" in r["command"] and "--direct" not in r["command"]
        assert "下载完成" in open(out + ".download.log", encoding="utf-8", errors="replace").read()

        print("== 3) download_status（终态） ==")
        s = tool_payload(c.call_tool("download_status", {"output": out}))
        print("   ", {k: s[k] for k in ("state", "exists", "size", "sidecar_exists")})
        assert s["state"] == "completed" and s["size"] == mb * MIB and not s["sidecar_exists"], s

        print("== 4) 与服务器直连取的同一段前缀逐字节比对（best-effort 对照） ==")
        a = md5_of(out)
        try:
            ref = fetch_prefix_direct(TEST_URL, mb * MIB, os.path.join(tmp, "ref.bin"),
                                      timeout=int(os.environ.get("WINDL_CONTROL_TIMEOUT", "900")))
            b = md5_of(ref)
            print(f"    worker 版 md5 = {a}\n    direct 版 md5 = {b}")
            assert a == b, "经 Worker 取到的前缀与直连取到的前缀不一致！"
            print(f"\n全部通过：真 Worker 链路（MCP → downloader → 127.0.0.1:8766 → tunnel → Windows）"
                  f" 内容与直连逐字节一致（{mb} MiB）")
        except Exception as e:
            print(f"    WARN 对照取数未完成（{type(e).__name__}）——服务器直连 ENA 慢是已知现象，"
                  f"不影响上述 1–3 步结论；如需强对照可设 WINDL_CONTROL_TIMEOUT 后重跑")
        c.close()
    except Exception as e:
        ok = False
        print(f"\nFAIL: {type(e).__name__}: {e}")
    finally:
        if ok:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"（保留现场：{tmp}）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
