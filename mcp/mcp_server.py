#!/usr/bin/env python3
"""mcp_server.py — Linux 侧 stdio MCP Server：把现有下载系统包装成 MCP Tools。

定位（包装指南 §2）：MCP 是 Adapter，不是第二个 Downloader。
  本文件只做四件事：参数校验 → 组装并启动 linux_downloader.py 子进程 → 读既有状态
  （sidecar / 端口注册表 / 告警流水 / 下载台账 CSV）→ 结构化返回。
  不实现 Range / Content-Range 校验 / chunk 调度 / retry / resume / checksum / zero scan /
  worker streaming / 写文件 —— 这些全部属于 downloader 与 Worker（§2 禁止清单）。
  多口探活、告警判定、台账生成同样**不重复实现**：优先调用本项目的既有脚本，其次只读它们写出的文件。

传输：stdio（§3）。stdout 只允许 JSON-RPC 消息（§4），所有日志走 stderr。
Tools：download / download_status / worker_status / alerts / overview。

配置（§12：代码内无机器特定硬编码）。优先级：
    环境变量  >  配置文件（$WINDL_CONFIG，默认 ~/.config/downloader_mcp/config.json）  >  内置默认值

    WINDL_DOWNLOADER       downloader 脚本路径（默认 ./vendor/linux_downloader.py）
    WINDL_PYTHON           运行 downloader / 辅助脚本的解释器（默认本进程解释器）
    WINDL_WORKER_URL       单端点 Worker（默认 http://127.0.0.1:8766）
    WINDL_SCRIPTS_DIR      存放 worker_pool.py / worker_alert.py / status_collector.py 的目录
                           （默认 = downloader 所在目录）
    WINDL_REGISTRY         端口注册表 JSON（默认 <scripts_dir>/workers.json）
    WINDL_WORKER_STATUS    探活哨兵写的状态 JSON（默认 <scripts_dir>/worker_status.json）
    WINDL_STATE_DIR        修复/完整性状态目录（默认 <scripts_dir>/state）
    WINDL_STATUS_CSV       下载台账 CSV（默认 <state_dir>/download_status.csv）
    WINDL_ALERTS_FILE      告警流水 JSONL（默认 <state_dir>/worker_alerts.jsonl）
    WINDL_HEALTH_TIMEOUT   GET /health 超时秒（默认 8）
    WINDL_SCRIPT_TIMEOUT   辅助脚本子进程超时秒（默认 45）
    WINDL_START_GRACE      启动观察窗秒（默认 3）
    WINDL_DEBUG=1          打开 stderr 诊断日志

仅依赖 stdlib + 系统 python3。
"""
import csv
import http.client
import json
import math
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

SERVER_NAME = "downloader"
SERVER_VERSION = "2.0.0"
PROTOCOL_FALLBACK = "2024-11-05"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DOWNLOADER = os.path.join(PROJECT_DIR, "vendor", "linux_downloader.py")
DEFAULT_CONFIG = os.path.join("~", ".config", "downloader_mcp", "config.json")
# 人类可读时间戳一律 UTC+8：台账 CSV 的 updated 列、哨兵状态文件的 updated_utc8 都是这个口径，
# 这里跟着走，免得同一个页面里出现两种时间基准（服务器本机时区是 UTC）。
TZ_OFFSET_HOURS = 8

INSTRUCTIONS = (
    "Adapter over an existing Linux download system (a downloader script driven through a "
    "local tunnel endpoint that fronts one or more download workers). This server does not "
    "reimplement any download logic: downloads run as detached background processes and "
    "`download` returns a handle immediately; `download_status` reports progress from the "
    "existing `<output>.download.json` sidecar (re-running the same download resumes "
    "automatically); `worker_status` reports which worker endpoints are reachable (pool-aware); "
    "`alerts` reads the existing worker alert journal (down/up events); `overview` summarizes "
    "the download ledger CSV (status counts, integrity/repair state). Long-lived downloads "
    "(tens to hundreds of GB) are expected."
)

DEBUG = os.environ.get("WINDL_DEBUG") == "1"


def log(msg):
    if DEBUG:
        sys.stderr.write(f"[downloader-mcp] {msg}\n")
        sys.stderr.flush()


def log_err(msg):
    sys.stderr.write(f"[downloader-mcp] {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- 配置

_CONFIG_CACHE = {}


def _load_config_file():
    """部署配置文件（不进仓库）：$WINDL_CONFIG > ~/.config/downloader_mcp/config.json。

    显式设置了 $WINDL_CONFIG 但文件不存在 → 不回退默认路径（显式即显式）。
    """
    explicit = os.environ.get("WINDL_CONFIG")
    path = os.path.abspath(os.path.expanduser(explicit)) if explicit else \
        os.path.abspath(os.path.expanduser(DEFAULT_CONFIG))
    if path in _CONFIG_CACHE:
        return _CONFIG_CACHE[path]
    data = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
            else:
                log_err(f"config file {path}: expected a JSON object, ignored")
        except Exception as e:
            log_err(f"config file {path}: {type(e).__name__}: {e} (ignored)")
    _CONFIG_CACHE[path] = data
    return data


def cfg():
    """部署配置：环境变量 > 配置文件 > 内置默认（§12）。"""
    f = _load_config_file()

    def pick(env_key, *file_keys, default=None):
        for k in (env_key, *file_keys):
            v = os.environ.get(k) if k == env_key else None
            if v not in (None, ""):
                return v
        for k in file_keys:
            v = f.get(k)
            if v not in (None, ""):
                return v
        return default

    def as_int(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    downloader = os.path.abspath(os.path.expanduser(
        str(pick("WINDL_DOWNLOADER", "downloader", default=DEFAULT_DOWNLOADER))))
    scripts_dir = os.path.abspath(os.path.expanduser(
        str(pick("WINDL_SCRIPTS_DIR", "scripts_dir", default=os.path.dirname(downloader)))))
    state_dir = os.path.abspath(os.path.expanduser(
        str(pick("WINDL_STATE_DIR", "state_dir", default=os.path.join(scripts_dir, "state")))))
    return {
        "downloader": downloader,
        "python": str(pick("WINDL_PYTHON", "python", default=sys.executable or "python3")),
        "worker_url": str(pick("WINDL_WORKER_URL", "worker_url",
                               default="http://127.0.0.1:8766")).rstrip("/"),
        "health_timeout": float(pick("WINDL_HEALTH_TIMEOUT", "health_timeout", default=8)),
        "script_timeout": float(pick("WINDL_SCRIPT_TIMEOUT", "script_timeout", default=45)),
        "start_grace": float(pick("WINDL_START_GRACE", "start_grace", default=3)),
        "scripts_dir": scripts_dir,
        "registry": os.path.abspath(os.path.expanduser(
            str(pick("WINDL_REGISTRY", "registry", default=os.path.join(scripts_dir, "workers.json"))))),
        "worker_status_file": os.path.abspath(os.path.expanduser(
            str(pick("WINDL_WORKER_STATUS", "worker_status_file",
                     default=os.path.join(scripts_dir, "worker_status.json"))))),
        "state_dir": state_dir,
        "status_csv": os.path.abspath(os.path.expanduser(
            str(pick("WINDL_STATUS_CSV", "status_csv",
                     default=os.path.join(state_dir, "download_status.csv"))))),
        "alerts_file": os.path.abspath(os.path.expanduser(
            str(pick("WINDL_ALERTS_FILE", "alerts_file",
                     default=os.path.join(state_dir, "worker_alerts.jsonl"))))),
        "pool_script": os.path.join(scripts_dir, "worker_pool.py"),
        "alert_script": os.path.join(scripts_dir, "worker_alert.py"),
        "collector_script": os.path.join(scripts_dir, "status_collector.py"),
    }


# ---------------------------------------------------------------- JSON-RPC stdio

def write_message(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(rid, result):
    write_message({"jsonrpc": "2.0", "id": rid, "result": result})


def reply_error(rid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    write_message({"jsonrpc": "2.0", "id": rid, "error": err})


def tool_ok(payload):
    """成功结果：text（同一份 JSON，供所有客户端）+ structuredContent。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "structuredContent": payload}


def tool_err(message):
    """工具级错误（参数/执行），§14：人类可读、不带 traceback。"""
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ---------------------------------------------------------------- 底层查询（只读）

def worker_health(endpoint, timeout):
    """GET {endpoint}/health。裸 http.client：与 downloader 一致，不走 env 代理。"""
    u = urlparse(endpoint)
    if u.scheme not in ("http", "https") or not u.hostname:
        return {"available": False, "error": f"invalid worker endpoint: {endpoint!r}"}
    conn = (http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout)
            if u.scheme == "https"
            else http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout))
    t0 = time.time()
    try:
        conn.request("GET", "/health")
        r = conn.getresponse()
        body = r.read(4096)
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()
    latency = round((time.time() - t0) * 1000, 1)
    if r.status != 200:
        return {"available": False, "error": f"GET /health -> HTTP {r.status}: {body[:200]!r}"}
    try:
        info = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        info = {}
    if not isinstance(info, dict):
        info = {}
    return {"available": True, "worker": info.get("worker") or info.get("status") or "ok",
            "status": info.get("status"), "latency_ms": latency}


def fmt_time(epoch):
    """epoch 秒 → 'YYYY-MM-DD HH:MM'（UTC+8，见 TZ_OFFSET_HOURS）。"""
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(epoch + TZ_OFFSET_HOURS * 3600))


def read_json(path):
    """读 JSON 文件；不存在/不可解析 → None（调用方给出提示，不抛）。"""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log(f"read_json({path}): {type(e).__name__}: {e}")
        return None


def run_script(cmd, timeout):
    """跑本项目的既有辅助脚本（只读或幂等动作）。返回 (rc|None, stdout, stderr)。"""
    log(f"run_script: {' '.join(shlex.quote(x) for x in cmd)} (timeout {timeout}s)")
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return None, "", f"timeout after {timeout:g}s"
    except Exception as e:
        return None, "", f"{type(e).__name__}: {e}"


def read_sidecar(output):
    """读现有 <output>.download.json（§9：不另建状态系统）。"""
    path = output + ".download.json"
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            d = json.load(f)
    except Exception as e:
        return {"path": path, "unreadable": f"{type(e).__name__}: {e}"}
    if not isinstance(d, dict):
        return {"path": path, "unreadable": "sidecar is not a JSON object"}
    size, cs = d.get("size"), d.get("chunk_size")
    done = len(d.get("completed_chunks") or [])
    total = int(math.ceil(size / cs)) if (size and cs) else None
    return {"path": path, "url": d.get("url"), "size": size, "chunk_size": cs,
            "completed_chunks": done, "total_chunks": total,
            "progress": round(done / total, 4) if total else None}


def tail_lines(path, n=8, max_bytes=16384):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
        lines = [l for l in data.splitlines() if l.strip()]
        return lines[-n:]
    except Exception as e:
        return [f"<cannot read log: {type(e).__name__}: {e}>"]


def tail_jsonl(path, n):
    """读 JSONL 文件末尾 n 条（返回解析后的对象；坏行原样包成 {"raw": ...}）。"""
    if not path or not os.path.exists(path):
        return []
    out = []
    for line in tail_lines(path, n=n, max_bytes=262144):
        try:
            obj = json.loads(line)
            out.append(obj if isinstance(obj, dict) else {"raw": line})
        except Exception:
            out.append({"raw": line})
    return out


# ---------------------------------------------------------------- Worker 池（不重复实现：优先用既有脚本）

def _registry_workers(c):
    """读端口注册表。返回 (ports, labels, registry_dict)；无注册表 → (None, {}, None)。"""
    reg = read_json(c["registry"])
    if not isinstance(reg, dict):
        return None, {}, None
    workers = reg.get("workers")
    if not isinstance(workers, dict):
        return None, {}, reg
    ports, labels = [], {}
    for k, v in workers.items():
        try:
            p = int(k)
        except (TypeError, ValueError):
            continue
        v = v if isinstance(v, dict) else {}
        if v.get("enabled", True) is False:      # enabled:false = 临时停用（哨兵与池都跳过）
            continue
        ports.append(p)
        if v.get("label"):
            labels[p] = v["label"]
    return sorted(ports), labels, reg


def _sentinel_history(c):
    """探活哨兵写的状态文件：down_since / 上下线事件 / 最近失败（只读）。"""
    st = read_json(c["worker_status_file"])
    if not isinstance(st, dict):
        return {}, None
    hist = {}
    for k, v in (st.get("workers") or {}).items():
        try:
            p = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, dict):
            hist[p] = {"down_since": v.get("down_since"), "last_ok": v.get("last_ok_utc8"),
                       "last_fail": v.get("last_fail_utc8"), "fail_streak": v.get("fail_streak"),
                       "note": v.get("note")}
    for k, v in (st.get("events") or {}).items():
        try:
            p = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, dict) and p in hist:
            hist[p]["up_last"] = v.get("up_last")
            hist[p]["down_last"] = v.get("down_last")
    return hist, st.get("updated_utc8")


def _port_endpoint(port):
    return f"http://127.0.0.1:{int(port)}"


def pool_snapshot(c):
    """端口池快照：注册表端口 + 实探活 + 哨兵历史。

    优先调用既有 worker_pool.py（池的唯一事实来源，含其自身的判定逻辑）；
    不可用时退化为本进程直接 GET /health（薄探活，仅 /health）。
    无注册表 → 单端点模式（v1 行为）。
    """
    ports, labels, reg = _registry_workers(c)
    if ports is None and reg is None:
        ports = []
    snap = {"registry": c["registry"] if reg is not None else None,
            "ports": {}, "alive": [], "source": None, "probe_error": None}
    if ports and os.path.exists(c["pool_script"]):
        rc, out, err = run_script([c["python"], c["pool_script"], "--json"], c["script_timeout"])
        data = None
        if rc == 0:
            try:
                data = json.loads(out)
            except Exception as e:
                snap["probe_error"] = f"worker_pool.py --json: unparsable output ({e})"
        else:
            snap["probe_error"] = f"worker_pool.py --json rc={rc}: {(err or '').strip()[:200]}"
        if isinstance(data, dict):
            snap["source"] = "worker_pool.py"
            for k, v in (data.get("ports") or {}).items():
                try:
                    p = int(k)
                except (TypeError, ValueError):
                    continue
                v = v if isinstance(v, dict) else {}
                snap["ports"][p] = {"port": p, "label": v.get("label"), "enabled": v.get("enabled", True),
                                    "ok": bool(v.get("ok")), "latency_ms": v.get("latency_ms"),
                                    "error": (v.get("error") or "")}
            for x in (data.get("alive") or []):
                try:
                    snap["alive"].append(int(x))
                except (TypeError, ValueError):
                    pass
    if not snap["ports"] and ports:                       # 退化：本进程薄探活
        snap["source"] = "probe"
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(ports)))) as ex:
            for p, h in ex.map(lambda p: (p, worker_health(_port_endpoint(p), c["health_timeout"])), ports):
                snap["ports"][p] = {"port": p, "label": labels.get(p), "enabled": True,
                                    "ok": bool(h.get("available")), "latency_ms": h.get("latency_ms"),
                                    "error": h.get("error") or "", "worker": h.get("worker")}
                if h.get("available"):
                    snap["alive"].append(p)
    if not snap["ports"] and not ports:                   # 无注册表 → 单端点
        snap["source"] = "endpoint"
        h = worker_health(c["worker_url"], c["health_timeout"])
        snap["single"] = h
        snap["endpoint"] = c["worker_url"]
    snap["alive"].sort(key=lambda p: (snap["ports"].get(p, {}).get("latency_ms") or 1e9, p))
    hist, updated = _sentinel_history(c)
    snap["history_updated"] = updated
    for p, item in snap["ports"].items():      # 历史字段恒定存在（无历史 → None），形状稳定
        item.setdefault("down_since", None)
        item.setdefault("last_ok", None)
        item.setdefault("last_fail", None)
        item.setdefault("fail_streak", None)
        item.setdefault("up_last", None)
        item.setdefault("down_last", None)
        item.update(hist.get(p, {}))
    return snap


def resolve_worker(c, name):
    """把 download 的 worker 参数解析成本地端点。

    允许值：省略/None（用配置端点）｜"auto"（从池里挑一个活口）｜注册表里的端口号。
    **不接受任意 URL**（§15：不提供 arbitrary Worker URL 能力）。
    返回 (endpoint, source) 或 (None, error_message)。
    """
    if name in (None, "", "default"):
        h = worker_health(c["worker_url"], c["health_timeout"])   # §11：先查活，不可用就明确报错
        if h.get("available"):
            return c["worker_url"], "configured"
        return None, (f"worker endpoint {c['worker_url']} is not reachable ({h.get('error')})")
    if isinstance(name, bool) or not isinstance(name, (str, int)):
        return None, "worker must be \"auto\" or a port number"
    s = str(name).strip()
    if s.lower() == "auto":
        snap = pool_snapshot(c)
        if snap.get("single") is not None:                # 没配池 → 退回单端点
            if snap["single"].get("available"):
                return c["worker_url"], "configured"
            return None, (f"worker endpoint {c['worker_url']} is unavailable "
                          f"({snap['single'].get('error')})")
        if not snap["alive"]:
            err = snap.get("probe_error") or "no reachable worker in the pool"
            return None, f"no alive worker in the pool ({err})"
        chosen = snap["alive"][0]
        return _port_endpoint(chosen), f"auto:{chosen}"
    if s.isdigit():
        port = int(s)
        ports, labels, reg = _registry_workers(c)
        if ports is None:
            return None, (f"worker {port} requested but no registry is configured "
                          f"(set WINDL_REGISTRY; arbitrary worker URLs are not accepted)")
        if port not in ports:
            return None, (f"worker {port} is not an enabled port in the registry "
                          f"(enabled ports: {', '.join(str(p) for p in ports) or 'none'})")
        h = worker_health(_port_endpoint(port), c["health_timeout"])
        if not h.get("available"):
            return None, (f"worker {port} ({labels.get(port, '')}) is not reachable at "
                          f"{_port_endpoint(port)} ({h.get('error')})")
        return _port_endpoint(port), f"registry:{port}"
    return None, ("worker must be omitted (configured endpoint), \"auto\", or a port number "
                  "from the registry; arbitrary worker URLs are not accepted")


# ---------------------------------------------------------------- 下载台账（只读既有 CSV）

LEDGER_FIELDS = ("species_name", "taxid", "genus", "class", "verdict", "status",
                 "path", "size_gb", "updated", "notes")


def read_ledger(path):
    """读下载台账 CSV（由既有 collector 脚本生成；本 server 不生成、不改写）。返回 (rows, error)。"""
    if not path or not os.path.exists(path):
        return None, f"ledger CSV not found: {path}"
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        return None, f"cannot read ledger CSV: {type(e).__name__}: {e}"
    return rows, None


def ledger_for_output(rows, output):
    """在台账里找某个输出路径对应的行。

    只有两种情形算命中：路径完全相同，或台账里的文件就落在 output 这个目录之下
    （output 传目录时）。其余一律不猜——宁可 matched=false，也不给用户挂错行。
    """
    out = os.path.abspath(output)
    for r in rows or []:
        p = (r.get("path") or "").strip()
        if not p or p.startswith("("):          # 占位值（如「排队/未落地」）不算路径
            continue
        ap = os.path.abspath(p)
        if out == ap or ap.startswith(out + os.sep):
            return r
    return None


def integrity_summary(c):
    """完整性/修复状态：台账状态计数 + 既有 state 目录里的修复 manifest（只读概览）。"""
    details = {"state_dir": c["state_dir"], "manifests": []}
    try:
        if os.path.isdir(c["state_dir"]):
            for name in sorted(os.listdir(c["state_dir"])):
                p = os.path.join(c["state_dir"], name)
                if not os.path.isfile(p):
                    continue
                if not (name.endswith(".tsv") or "manifest" in name.lower()):
                    continue
                try:
                    with open(p, "rb") as f:
                        lines = sum(1 for _ in f)
                except Exception:
                    lines = None
                details["manifests"].append({"file": name, "entries": lines,
                                             "mtime_utc8": fmt_time(os.path.getmtime(p))})
    except Exception as e:
        details["error"] = f"{type(e).__name__}: {e}"
    return details


# ---------------------------------------------------------------- 子进程登记（仅内存，无持久化）

_CHILDREN = {}  # abs output -> {proc, pid, started, cmd, log}（仅内存；sidecar 才是持久状态）
_MAX_CHILDREN = 200


def _remember(out, entry):
    """登记子进程；长驻 server 只保留有限条已完成记录，避免无界增长。"""
    _CHILDREN[out] = entry
    if len(_CHILDREN) > _MAX_CHILDREN:
        for k in [k for k, v in _CHILDREN.items() if v["proc"].poll() is not None][:len(_CHILDREN) - _MAX_CHILDREN]:
            _CHILDREN.pop(k, None)


def _build_status(output):
    out = os.path.abspath(output)
    st = {"output": out, "exists": os.path.exists(out)}
    # output 也可能是目录（台账里一笔数据常记为目录）：此时不给 size——目录的 st_size 没有意义
    st["is_dir"] = os.path.isdir(out)
    st["size"] = (os.path.getsize(out) if (st["exists"] and not st["is_dir"]) else None)
    sc = read_sidecar(out)
    st["sidecar_exists"] = sc is not None
    if sc:
        st["sidecar"] = sc
        if "unreadable" not in sc:
            st["completed_chunks"] = sc["completed_chunks"]
            st["total_chunks"] = sc["total_chunks"]
            st["progress"] = sc["progress"]
    entry = _CHILDREN.get(out)
    if entry:
        rc = entry["proc"].poll()
        st["pid"] = entry["pid"]
        st["elapsed_s"] = round(time.time() - entry["started"], 1)
        st["log"] = entry["log"]
        if rc is None:
            st["state"] = "running"
        elif rc == 0:
            st["state"], st["exit_code"] = "completed", 0
        elif sc:
            # 非零退出但 sidecar 仍在 = 被中断/失败的断点：可直接重跑同参数续传
            st["state"], st["exit_code"], st["resumable"] = "incomplete", rc, True
            st["log_tail"] = tail_lines(entry["log"])
            st["hint"] = "re-running download with the same url/output resumes automatically"
        else:
            st["state"], st["exit_code"] = "failed", rc
            st["log_tail"] = tail_lines(entry["log"])
    else:
        if sc:
            st["state"] = "incomplete"  # 有 sidecar 但非本进程所起：断点（可重跑同参数续传）
        elif st["exists"]:
            st["state"] = "completed"
            st["note"] = ("no sidecar: the downloader deletes it only after final verification "
                          "(size / md5 / zero-scan) passes"
                          + ("; output is a directory (a download may land several files) — size is "
                             "not reported" if st["is_dir"] else ""))
        else:
            st["state"] = "not_started"
    return st


# ---------------------------------------------------------------- 参数校验（§14 invalid argument）

def _opt_int(args, key, minimum, allow_size_string=False):
    if args.get(key) is None:
        return None, None
    v = args[key]
    if isinstance(v, bool):
        return None, f"{key} must be an integer"
    if isinstance(v, int):
        return (v, None) if v >= minimum else (None, f"{key} must be >= {minimum}")
    if allow_size_string and isinstance(v, str):
        s = v.strip()
        if s[:-1].isdigit() and s[-1:].lower() in ("k", "m", "g"):
            return s, None
        if s.isdigit():
            return s, None
    return None, f"{key} must be an integer" + (" or a size string like '256M'" if allow_size_string else "")


def _opt_str(args, key):
    v = args.get(key)
    if v is None:
        return None, None
    if not isinstance(v, str) or not v.strip():
        return None, f"{key} must be a non-empty string"
    return v.strip(), None


def _opt_bool(args, key, default):
    v = args.get(key, default)
    if not isinstance(v, bool):
        return None, f"{key} must be a boolean"
    return v, None


# ---------------------------------------------------------------- Tools

def tool_download(args):
    if not isinstance(args, dict):
        return tool_err("invalid argument(s):\n- arguments must be a JSON object")
    c = cfg()
    errors = []

    url, e = _opt_str(args, "url")
    if not url:
        errors.append(e or "url is required (absolute http(s) URL)")
    else:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            errors.append(f"url must be an absolute http(s) URL (got {url!r})")
    output, e = _opt_str(args, "output")
    if not output:
        errors.append(e or "output is required (local file path)")
    md5, e = _opt_str(args, "md5")
    if e:
        errors.append(e)
    chunk_size, e = _opt_int(args, "chunk_size", 1, allow_size_string=True)
    if e:
        errors.append(e)
    connections, e = _opt_int(args, "connections", 1)
    if e:
        errors.append(e)
    retries, e = _opt_int(args, "retries", 0)
    if e:
        errors.append(e)
    max_bytes, e = _opt_int(args, "max_bytes", 1, allow_size_string=True)
    if e:
        errors.append(e)
    wait_seconds, e = _opt_int(args, "wait_seconds", 0)
    if e:
        errors.append(e)
    if wait_seconds and wait_seconds > 3600:
        errors.append("wait_seconds must be <= 3600")
    via_worker, e = _opt_bool(args, "via_worker", True)
    if e:
        errors.append(e)
    worker_arg = args.get("worker")
    if worker_arg is not None and not isinstance(worker_arg, (str, int)):
        errors.append("worker must be \"auto\" or a port number (integer or string)")
    if via_worker is False and worker_arg not in (None, ""):
        errors.append("worker cannot be combined with via_worker=false (direct download has no worker)")
    if errors:
        return tool_err("invalid argument(s):\n- " + "\n- ".join(errors))

    if not os.path.exists(c["downloader"]):
        return tool_err(f"downloader script not found: {c['downloader']}\n"
                        f"Set WINDL_DOWNLOADER to the actual linux_downloader.py path.")
    out = os.path.abspath(output)

    entry = _CHILDREN.get(out)
    if entry and entry["proc"].poll() is None:
        return tool_err(f"a download for this output is already running in this MCP server "
                        f"(pid {entry['pid']}). Use download_status(output=...) to monitor it.")

    worker_endpoint, worker_source = None, None
    if via_worker:  # §11：要求走 Worker 而 Worker 不可用 → 明确报错，不偷偷回退 direct
        worker_endpoint, worker_source = resolve_worker(c, worker_arg)
        if worker_endpoint is None:
            return tool_err(
                f"Worker is unavailable: {worker_source}\n"
                f"Please check the worker service and the tunnel for that endpoint "
                f"(or try worker=\"auto\" / another registered port). "
                f"Do NOT fall back silently: pass via_worker=false to download via the direct path instead.")

    cmd = [c["python"], c["downloader"], url, "-o", out]
    if md5:
        cmd += ["--md5", md5]
    if chunk_size:
        cmd += ["--chunk-size", str(chunk_size)]
    if connections:
        cmd += ["--connections", str(connections)]
    if retries is not None:
        cmd += ["--max-retries", str(retries)]
    if max_bytes:
        cmd += ["--max-bytes", str(max_bytes)]
    cmd += ["--worker", worker_endpoint] if via_worker else ["--direct"]

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    log_path = out + ".download.log"
    with open(log_path, "a") as lf:
        lf.write(f"\n===== [{time.strftime('%Y-%m-%d %H:%M:%S')}] downloader-mcp: "
                 f"{' '.join(shlex.quote(x) for x in cmd)}\n")
    logf = open(log_path, "ab")
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf,
                                stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    finally:
        logf.close()  # 子进程持有自己的 fd；父进程侧句柄可关
    started = time.time()
    _remember(out, {"proc": proc, "pid": proc.pid, "started": started, "cmd": cmd, "log": log_path})
    log(f"spawned pid={proc.pid} out={out} cmd={cmd}")

    # 启动观察窗：立即失败（参数错/URL 错/权限错）当次调用就报出来
    grace_deadline = time.time() + c["start_grace"]
    while time.time() < grace_deadline and proc.poll() is None:
        time.sleep(0.2)

    if wait_seconds:  # 可选阻塞等待（默认 0 = 立即返回；长下载请用 download_status 轮询）
        wait_deadline = time.time() + wait_seconds
        while time.time() < wait_deadline and proc.poll() is None:
            time.sleep(0.5)

    rc = proc.poll()
    payload = {
        "started": True,
        "output": out,
        "url": url,
        "via_worker": via_worker,
        "worker_endpoint": worker_endpoint if via_worker else None,
        "worker_source": worker_source if via_worker else None,
        "pid": proc.pid,
        "sidecar": out + ".download.json",
        "log": log_path,
        "command": cmd,
        "effective": {"chunk_size": chunk_size, "connections": connections,
                      "retries": retries, "max_bytes": max_bytes, "md5": md5},
        "elapsed_s": round(time.time() - started, 1),
    }
    if rc is None:
        payload["state"] = "running"
        payload["hint"] = "long download: poll download_status(output=...); re-running download resumes automatically"
    elif rc == 0:
        payload["state"] = "completed"
        payload["exit_code"] = 0
        payload["size"] = os.path.getsize(out) if os.path.exists(out) else None
    else:
        tail = "\n".join(tail_lines(log_path))
        return tool_err(f"download failed immediately (exit code {rc}).\n"
                        f"Log: {log_path}\n--- last output ---\n{tail}")
    return tool_ok(payload)


def tool_download_status(args):
    if not isinstance(args, dict):
        return tool_err("invalid argument(s):\n- arguments must be a JSON object")
    output, e = _opt_str(args, "output")
    if not output:
        return tool_err("invalid argument(s):\n- " + (e or "output is required (local file path)"))
    with_ledger, e = _opt_bool(args, "with_ledger", False)
    if e:
        return tool_err("invalid argument(s):\n- " + e)
    st = _build_status(output)
    if with_ledger:
        c = cfg()
        rows, err = read_ledger(c["status_csv"])
        if rows is None:
            st["ledger"] = {"error": err}
        else:
            row = ledger_for_output(rows, st["output"])
            st["ledger"] = {"file": c["status_csv"], "matched": row is not None, "row": row}
    return tool_ok(st)


def tool_worker_status(args):
    args = args if isinstance(args, dict) else {}
    include_history, e = _opt_bool(args, "include_history", True)
    if e:
        return tool_err("invalid argument(s):\n- " + e)
    c = cfg()
    snap = pool_snapshot(c)
    if snap.get("single") is not None:                    # 单端点模式：保持 v1 返回形状
        h = snap["single"]
        payload = {"available": h["available"], "mode": "endpoint",
                   "source": "endpoint", "endpoint": c["worker_url"]}
        if h["available"]:
            payload["worker"] = h["worker"]
            payload["latency_ms"] = h["latency_ms"]
        else:
            payload["error"] = h["error"]
            payload["hint"] = ("Worker unavailable: check the worker service and the tunnel. "
                               "Downloads with via_worker=false are unaffected.")
        return tool_ok(payload)
    workers = []
    for p, item in sorted(snap["ports"].items()):
        row = dict(item)
        if not include_history:
            for k in ("down_since", "last_ok", "last_fail", "fail_streak", "up_last", "down_last", "note"):
                row.pop(k, None)
        workers.append(row)
    alive = [p for p in snap["alive"] if snap["ports"].get(p, {}).get("ok")]
    payload = {
        "available": bool(alive),
        "mode": "pool",
        "source": snap["source"],                 # worker_pool.py / probe
        "registry": snap["registry"],
        "auto_endpoint": _port_endpoint(alive[0]) if alive else None,
        "alive": alive,
        "workers": workers,
    }
    if snap.get("probe_error"):
        payload["probe_error"] = snap["probe_error"]
    if include_history and snap.get("history_updated"):
        payload["history_updated"] = snap["history_updated"]
    if not alive:
        payload["hint"] = ("no worker endpoint is reachable; downloads with via_worker=false are "
                           "unaffected. Check the worker service and its tunnel for the listed ports.")
    return tool_ok(payload)


def tool_alerts(args):
    args = args if isinstance(args, dict) else {}
    limit, e = _opt_int(args, "limit", 1)
    if e:
        return tool_err("invalid argument(s):\n- " + e)
    limit = min(limit or 20, 500)
    ensure, e = _opt_bool(args, "ensure", False)
    if e:
        return tool_err("invalid argument(s):\n- " + e)
    c = cfg()
    payload = {"file": c["alerts_file"]}
    if ensure:
        if not os.path.exists(c["alert_script"]):
            return tool_err(f"alert script not found: {c['alert_script']}\n"
                            f"Set WINDL_SCRIPTS_DIR to the directory holding worker_alert.py.")
        rc, out, err = run_script([c["python"], c["alert_script"], "--ensure"], c["script_timeout"])
        payload["ensure_rc"] = rc
        payload["ensure_output"] = (out or err or "").strip()[-4000:]
    events = tail_jsonl(c["alerts_file"], limit)
    payload["file_exists"] = os.path.exists(c["alerts_file"])
    payload["events"] = events
    payload["count"] = len(events)
    if not payload["file_exists"]:
        payload["hint"] = ("no alert journal yet; set WINDL_ALERTS_FILE to the journal written by the "
                           "alert sentinel, or call this tool with ensure=true to start the sentinel.")
    return tool_ok(payload)


def tool_overview(args):
    args = args if isinstance(args, dict) else {}
    limit, e = _opt_int(args, "limit", 1)
    if e:
        return tool_err("invalid argument(s):\n- " + e)
    limit = min(limit or 50, 1000)
    refresh, e = _opt_bool(args, "refresh", False)
    if e:
        return tool_err("invalid argument(s):\n- " + e)
    state, e = _opt_str(args, "state")
    if e:
        return tool_err("invalid argument(s):\n- " + e)
    species, e = _opt_str(args, "species")
    if e:
        return tool_err("invalid argument(s):\n- " + e)
    genus, e = _opt_str(args, "genus")
    if e:
        return tool_err("invalid argument(s):\n- " + e)
    c = cfg()
    payload = {"ledger": c["status_csv"]}
    if refresh:
        if not os.path.exists(c["collector_script"]):
            return tool_err(f"collector script not found: {c['collector_script']}\n"
                            f"Set WINDL_SCRIPTS_DIR to the directory holding status_collector.py.")
        rc, out, err = run_script([c["python"], c["collector_script"]], c["script_timeout"])
        payload["refresh_rc"] = rc
        payload["refresh_output"] = (out or err or "").strip()[-2000:]
    rows, err = read_ledger(c["status_csv"])
    if rows is None:
        payload["error"] = err
        payload["hint"] = ("set WINDL_STATUS_CSV to the download ledger CSV produced by the "
                           "collector script (the server never writes the ledger).")
        return tool_ok(payload)

    def match(r):
        if state and (r.get("status") or "").upper() != state.upper():
            return False
        if species and species.lower() not in (r.get("species_name") or "").lower():
            return False
        if genus and (r.get("genus") or "").lower() != genus.lower():
            return False
        return True

    counts = {}
    for r in rows:
        s = (r.get("status") or "").strip() or "UNKNOWN"
        counts[s] = counts.get(s, 0) + 1
    shown = [r for r in rows if match(r)]
    payload.update({
        "updated_utc8": fmt_time(os.path.getmtime(c["status_csv"])),
        "total": len(rows),
        "counts": counts,
        "matched": len(shown),
        "returned": min(len(shown), limit),
        "rows": shown[:limit],
        "integrity": {
            "repaired": counts.get("REPAIRED", 0),
            "repairing": counts.get("REPAIRING", 0),
            "repair_fail": counts.get("REPAIR_FAIL", 0),
            **integrity_summary(c),
        },
    })
    if len(shown) > limit:
        payload["hint"] = (f"{len(shown)} rows matched, {limit} returned; narrow with state/species/genus "
                           f"or raise limit (max 1000).")
    return tool_ok(payload)


TOOLS = [
    {
        "name": "download",
        "description": (
            "Start a download with the existing Linux downloader (thin adapter over linux_downloader.py; "
            "the downloader itself is NOT reimplemented here). Runs as a detached background process and "
            "returns a handle immediately — use download_status to poll progress. "
            "RE-RUNNING THE SAME url/output/parameters RESUMES AUTOMATICALLY from the existing "
            "`<output>.download.json` sidecar (completed chunks are skipped); there is no separate resume flag. "
            "Final verification (size, md5 if given, zero-scan of >=64KB zero runs) always runs; only after it "
            "passes is the sidecar deleted. Do not lower chunk_size: each request has a fixed overhead of "
            "roughly ten seconds. Biomedical/bioinformatics large files: pass md5 when an official checksum "
            "is available."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute http(s) URL to download."},
                "output": {"type": "string",
                           "description": "Local output file path (used as-is; parent dirs are created)."},
                "md5": {"type": "string",
                        "description": "Official whole-file md5; verified in final validation if given."},
                "chunk_size": {"type": "integer",
                               "description": "Chunk size in bytes. Default 268435456 (256 MiB). Do not lower it."},
                "connections": {"type": "integer", "minimum": 1,
                                "description": "Parallel Range connections. Omit to use the downloader default "
                                               "(currently 2). 1 = serial; 4 is useful for large files."},
                "retries": {"type": "integer", "minimum": 0,
                            "description": "Max retries per chunk. Omit to use the downloader default (3)."},
                "via_worker": {"type": "boolean",
                               "description": "true (default): download through a worker reached via a local "
                                              "tunnel endpoint. false: direct from this host (--direct). If the "
                                              "worker is unavailable with via_worker=true, the call fails "
                                              "explicitly instead of falling back."},
                "worker": {"type": ["string", "integer"],
                           "description": "\"auto\" (pick a reachable worker from the pool) or a port number "
                                          "listed by worker_status. Omit to use the configured endpoint. "
                                          "Arbitrary worker URLs are not accepted."},
                "max_bytes": {"type": "integer",
                              "description": "Download only the first N bytes (test truncation; requires Range "
                                             "support). Not for production downloads."},
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 3600,
                                 "description": "Block up to this many seconds waiting for completion before "
                                                "returning (default 0 = return immediately). Useful for small files."}
            },
            "required": ["url", "output"]
        },
        "outputSchema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "started": {"type": "boolean"},
                "state": {"type": "string"},
                "output": {"type": "string"},
                "url": {"type": "string"},
                "pid": {"type": "integer"},
                "size": {"type": ["integer", "null"]},
                "via_worker": {"type": "boolean"},
                "worker_endpoint": {"type": ["string", "null"]},
                "worker_source": {"type": ["string", "null"]},
                "sidecar": {"type": "string"},
                "log": {"type": "string"},
                "command": {"type": "array", "items": {"type": "string"}},
                "elapsed_s": {"type": "number"}
            }
        }
    },
    {
        "name": "download_status",
        "description": (
            "Report the state of a download by reading `<output>` and the existing `<output>.download.json` "
            "sidecar (no separate job database). States: not_started / running / incomplete (sidecar present, "
            "resumable — includes interrupted or failed-but-partial downloads) / completed / failed (no sidecar "
            "to resume from). For downloads started by this server the pid, elapsed time, log path and the log "
            "tail are included. With with_ledger=true the matching row of the download ledger CSV is attached."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "output": {"type": "string", "description": "Output file path used in the download call."},
                "with_ledger": {"type": "boolean",
                                "description": "Attach the matching ledger row (status/verdict/notes) if a "
                                               "ledger CSV is configured. Default false."}
            },
            "required": ["output"]
        },
        "outputSchema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "output": {"type": "string"},
                "state": {"type": "string"},
                "exists": {"type": "boolean"},
                "is_dir": {"type": "boolean"},
                "size": {"type": ["integer", "null"]},
                "sidecar_exists": {"type": "boolean"},
                "completed_chunks": {"type": "integer"},
                "total_chunks": {"type": "integer"},
                "progress": {"type": ["number", "null"]},
                "pid": {"type": "integer"},
                "log": {"type": "string"},
                "ledger": {"type": ["object", "null"]}
            }
        }
    },
    {
        "name": "worker_status",
        "description": (
            "Report which download worker endpoints are reachable through their local tunnel endpoints "
            "(GET /health), pool-aware: when a worker registry is configured, every enabled port is listed "
            "with ok/latency plus, when the sentinel status file is available, down_since and last up/down "
            "times. Read-only diagnostics; it does not manage tunnels, workers or the pool, and it never "
            "touches the network layer beyond the local endpoints."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_history": {"type": "boolean",
                                    "description": "Include sentinel history (down_since, last up/down times). "
                                                   "Default true."}
            }
        },
        "outputSchema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "available": {"type": "boolean"},
                "mode": {"type": "string"},
                "worker": {"type": "string"},
                "latency_ms": {"type": "number"},
                "error": {"type": "string"},
                "alive": {"type": "array", "items": {"type": "integer"}},
                "auto_endpoint": {"type": ["string", "null"]},
                "workers": {"type": "array", "items": {"type": "object"}}
            }
        }
    },
    {
        "name": "alerts",
        "description": (
            "Read the worker alert journal (JSONL written by the existing alert sentinel): down/up events with "
            "timestamps, the failing endpoint, the last error and (when the sentinel records it) whether the "
            "local listener was still present — which distinguishes a dropped tunnel from an unresponsive "
            "worker. With ensure=true it first runs the sentinel's idempotent self-check (starts the sentinel if "
            "it is not running, then reports reachability and the most recent alerts). Read-only otherwise."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 500,
                          "description": "How many of the most recent events to return. Default 20."},
                "ensure": {"type": "boolean",
                           "description": "Run the alert sentinel's self-check first (idempotent: it starts the "
                                          "sentinel only if it is not already running). Default false."}
            }
        },
        "outputSchema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "file": {"type": "string"},
                "file_exists": {"type": "boolean"},
                "count": {"type": "integer"},
                "events": {"type": "array", "items": {"type": "object"}},
                "ensure_output": {"type": "string"}
            }
        }
    },
    {
        "name": "overview",
        "description": (
            "Summarize the download ledger CSV produced by the existing collector script: status counts "
            "(queued / downloading / done / repairing / repaired / repair_fail), optional filtering by state, "
            "species substring or genus, and the integrity section (repair counts plus the repair manifests "
            "found in the state directory). With refresh=true it first re-runs the collector (idempotent, may "
            "take a few seconds on large trees) and then reads the CSV. The server never writes the ledger."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean",
                            "description": "Re-run the collector script before reading the CSV. Default false."},
                "state": {"type": "string",
                          "description": "Only rows with this status (e.g. DONE, QUEUED, REPAIRING)."},
                "species": {"type": "string", "description": "Only rows whose species name contains this text."},
                "genus": {"type": "string", "description": "Only rows with exactly this genus."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000,
                          "description": "Maximum rows to return (default 50). Counts always cover all rows."}
            }
        },
        "outputSchema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "ledger": {"type": "string"},
                "updated": {"type": "string"},
                "total": {"type": "integer"},
                "counts": {"type": "object"},
                "matched": {"type": "integer"},
                "returned": {"type": "integer"},
                "rows": {"type": "array", "items": {"type": "object"}},
                "integrity": {"type": "object"}
            }
        }
    }
]

HANDLERS = {"download": tool_download, "download_status": tool_download_status,
            "worker_status": tool_worker_status, "alerts": tool_alerts, "overview": tool_overview}


# ---------------------------------------------------------------- 主循环

def handle(msg):
    if not isinstance(msg, dict):
        return
    method, rid = msg.get("method"), msg.get("id")
    if method == "initialize":
        params = msg.get("params") or {}
        pv = params.get("protocolVersion")
        log(f"initialize: client protocolVersion={pv!r} client={json.dumps(params.get('clientInfo'))[:120]}")
        reply(rid, {"protocolVersion": pv if isinstance(pv, str) and pv else PROTOCOL_FALLBACK,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": INSTRUCTIONS})
    elif method == "tools/list":
        reply(rid, {"tools": TOOLS})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            reply_error(rid, -32602, f"unknown tool: {name!r}")
            return
        log(f"tools/call {name} args={json.dumps(args, ensure_ascii=False)[:200]}")
        try:
            result = handler(args)
        except Exception as e:  # 任何意外都转成清晰的工具错误（§14）
            log_err(f"tool {name} crashed: {type(e).__name__}: {e}")
            result = tool_err(f"internal error in {name}: {type(e).__name__}: {e}")
        reply(rid, result)
    elif method == "ping":
        reply(rid, {})
    elif rid is None:
        pass  # 通知（notifications/initialized、notifications/cancelled 等）无需应答
    else:
        reply_error(rid, -32601, f"method not found: {method!r}")


def serve():
    c = cfg()
    log_err(f"started v{SERVER_VERSION} | downloader={c['downloader']} | worker={c['worker_url']} "
            f"| scripts_dir={c['scripts_dir']} | ledger={c['status_csv']} | python={c['python']} "
            f"| pid={os.getpid()}")
    while True:
        line = sys.stdin.readline()
        if not line:  # EOF：客户端关闭
            log("stdin EOF, exiting")
            return
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            reply_error(None, -32700, f"parse error: {e}")
            continue
        handle(msg)


if __name__ == "__main__":
    try:
        serve()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    except Exception as e:
        log_err(f"fatal: {type(e).__name__}: {e}")
        sys.exit(1)
