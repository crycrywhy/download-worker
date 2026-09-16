#!/usr/bin/env python3
"""run_live_smoke.py — 真机只读冒烟：对**已部署**的下载系统跑一遍 5 个工具（不改任何状态）。

与 run_worker_tests.py 的分工：
  · 本脚本：worker_status / alerts / overview / download_status 四个**只读**工具（+ --ensure 时才跑一次幂等自检）
  · run_worker_tests.py：download（真 Worker 取 8 MiB 前缀并逐字节对照）

依赖部署侧环境（脚本本身不含任何机器特定路径，§12）：
    WINDL_SCRIPTS_DIR   （或配置文件/环境里的等价项）指向真实 scripts 目录
可选参数：--ensure（额外验证 alerts 的幂等自检：哨兵不在则拉起、在则报告）

用法: WINDL_SCRIPTS_DIR=<真实 scripts 目录> python3 tests/run_live_smoke.py [--ensure]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from jrpc_client import MCPClient, tool_payload  # noqa: E402

ENSURE = "--ensure" in sys.argv


def main():
    ok = True
    c = MCPClient().start()          # 不注入 env 覆盖：走部署侧真实配置
    c.request("initialize", {"protocolVersion": "2025-06-18",
                            "clientInfo": {"name": "live-smoke", "version": "0"}})
    c.notify("notifications/initialized")
    try:
        print("== 1) worker_status（只读：各口 /health + 哨兵历史） ==")
        st = tool_payload(c.call_tool("worker_status"))
        print(f"    mode={st.get('mode')} source={st.get('source')} available={st.get('available')} "
              f"alive={st.get('alive')} auto={st.get('auto_endpoint')}")
        if st.get("mode") == "pool":
            for w in st["workers"]:
                print(f"      :{w['port']:<5} {str(w.get('label')):<12} ok={w.get('ok')!s:<5} "
                      f"latency={w.get('latency_ms')} down_since={w.get('down_since')} "
                      f"last_ok={w.get('last_ok')}")
            assert st["workers"], "池模式应至少列出一个口"
            print(f"    history_updated={st.get('history_updated')}")
        else:
            print(f"    endpoint={st.get('endpoint')} worker={st.get('worker')} "
                  f"latency={st.get('latency_ms')}")
        if st.get("probe_error"):
            print(f"    probe_error={st['probe_error']}")

        print("== 2) alerts（只读：告警流水尾部） ==")
        al = tool_payload(c.call_tool("alerts", {"limit": 5}))
        print(f"    file={al['file']} exists={al['file_exists']} count={al['count']}")
        for e in al["events"]:
            print(f"      {e.get('ts_utc8')} {e.get('event'):<5} :{e.get('port')} "
                  f"device={e.get('device_state')} listener={e.get('listener')} err={e.get('error')}")
        assert al["file_exists"] is not False or "hint" in al, "缺流水时应给 hint"

        print("== 3) overview（只读：台账计数 + 完整性 manifest） ==")
        ov = tool_payload(c.call_tool("overview", {"limit": 3}))
        assert "total" in ov, ov
        print(f"    ledger={ov['ledger']}\n    updated={ov.get('updated_utc8')} total={ov['total']} "
              f"counts={ov['counts']}")
        integ = ov.get("integrity", {})
        print(f"    integrity: repaired={integ.get('repaired')} repairing={integ.get('repairing')} "
              f"repair_fail={integ.get('repair_fail')} manifests={len(integ.get('manifests') or [])}")
        for m in (integ.get("manifests") or [])[:3]:
            print(f"      {m['file']}  entries={m['entries']}  mtime_utc8={m['mtime_utc8']}")

        print("== 4) download_status（只读：取一条 DONE 行的真实落地文件） ==")
        cand = None
        rows = tool_payload(c.call_tool("overview", {"state": "DONE", "limit": 1000}))["rows"]
        for r in rows:
            p = (r.get("path") or "").strip()
            if p and not p.startswith("(") and os.path.exists(p):
                cand = p
                break
        if cand is None:
            print("    SKIP：台账里没有可直接读的落地文件（不影响结论）")
        else:
            s = tool_payload(c.call_tool("download_status", {"output": cand, "with_ledger": True}))
            print(f"    {cand}\n      state={s['state']} size={s['size']} sidecar={s['sidecar_exists']} "
                  f"ledger_matched={s['ledger']['matched']}")
            assert s["state"] == "completed" and s["ledger"]["matched"] is True, s

        if ENSURE:
            print("== 5) alerts --ensure（幂等自检：哨兵不在则拉起，在则报告） ==")
            al2 = tool_payload(c.call_tool("alerts", {"limit": 3, "ensure": True}))
            print(f"    ensure_rc={al2['ensure_rc']}\n    {al2['ensure_output'][:600]}")
            assert al2["ensure_rc"] == 0, al2
        else:
            print("== 5) alerts --ensure（未跑；需要时加 --ensure） ==")
        c.close()
    except Exception as e:
        ok = False
        print(f"\nFAIL: {type(e).__name__}: {e}")
        c.close()
    print("\n" + ("真机只读冒烟通过" if ok else "真机只读冒烟失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
