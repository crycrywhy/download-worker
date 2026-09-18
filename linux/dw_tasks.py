#!/usr/bin/env python3
"""dw_tasks.py - Linux 侧只读任务视图（给 Windows 的 `download-worker status` / `process` 用）。

这个脚本**只读**：不下载、不改配置、不碰任何正在写的文件。它把 Linux 侧**已经存在**的两份
产物读出来拼成 JSON，交给 Windows 侧渲染。

数据来源
--------
1. **下载台账 CSV**（`--status-csv`，不给就按 `--csv-candidates` 自动探测）
   一行一个目标文件，约定列：`species_name,taxid,genus,class,verdict,status,path,size_gb,updated,notes`
   -> 状态计数、当前在下（`DOWNLOADING` / `REPAIRING`）的任务、最近完成。
   列名认得几种常见写法（`species_name`/`species`/`name`，`size_gb`/`size`），缺列只是少一项信息。

2. **在下任务目录里的 sidecar `<输出文件>.download.json`**（下载器写的断点文件）
   `{"url":..., "size":..., "chunk_size":..., "completed_chunks":[...]}`
   -> 精确进度（已完成块 -> 字节 -> 百分比）、块计数、sidecar 新鲜度。
   找不到 sidecar 时退回**已分配块数**（`st_blocks`）。
   ⚠ 下载器一开始就把目标文件 sparse 预分配到全尺寸，所以 `ls -l` 的大小**不代表进度**，
   唯一可靠的两个来源就是 sidecar 与已分配块数。

用法
----
    python3 dw_tasks.py --json            # 一次性快照（download-worker status 用）
    python3 dw_tasks.py --watch 2         # 每 2 秒输出一行 JSON（download-worker process 用）
    python3 dw_tasks.py --text            # 人看的表格（在 Linux 上直接跑）
    python3 dw_tasks.py --limit 5         # 「最近完成」显示几条（默认 5）

`--watch` 是 **JSONL**（一行一个对象、写完即 flush），断了管道（Ctrl+C / ssh 断开）就退出。
速度（`speed_bps`）只在 `--watch` 里有：靠相邻两次采样的差值算，一次性快照给不出速度。
"""
import argparse
import csv
import glob
import json
import os
import socket
import sys
import time
from datetime import datetime

SCHEMA = 1

# 台账 CSV 的常见位置（按顺序取第一个存在的）。也可以用环境变量 DW_STATUS_CSV 指定。
CSV_CANDIDATES = [
    "~/.config/download-worker/download_status.csv",
    "~/.config/download-worker/state/download_status.csv",
    "~/.config/download-worker/daily/download_status.csv",
]

# 哪些状态算「正在派送」（要显示进度），哪些算「最近完成」
ACTIVE_STATES = ("DOWNLOADING", "REPAIRING")
DONE_STATES = ("DONE", "REPAIRED")

# sidecar 多久没动就算「停了」：windows 线每下完一个 256 MB 块就重写一次 sidecar，
# 按 1 MiB/s 算一块也不到 5 分钟。台账里带着 DOWNLOADING 但 sidecar 早就不动的行，
# 是中断残留（等下一轮重排队），不是此刻真的在传输 —— 两者必须分开显示。
LIVE_WINDOW_S = 600

COLUMN_ALIASES = {
    "species": ("species_name", "species", "name", "target"),
    "taxid": ("taxid", "tax_id", "tax"),
    "status": ("status", "state"),
    "path": ("path", "local_path", "outdir", "output"),
    "size_gb": ("size_gb", "size", "gb"),
    "updated": ("updated", "updated_at", "mtime", "time"),
    "notes": ("notes", "note", "remark", "备注"),
}


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def pick(row, key):
    """按别名从一行 CSV 里取值，取不到返回空串。"""
    for name in COLUMN_ALIASES[key]:
        if name in row and row[name] not in (None, ""):
            return str(row[name]).strip()
    return ""


def find_csv(explicit):
    if explicit:
        return explicit, []
    env = os.environ.get("DW_STATUS_CSV", "").strip()
    if env:
        return env, [os.path.expanduser(env)]      # 报错时也要说清「查过哪」
    checked = []
    for cand in CSV_CANDIDATES:
        path = os.path.expanduser(cand)
        checked.append(path)
        if os.path.isfile(path):
            return path, checked
    return "", checked


def find_active_sidecar(directory):
    """在目标目录里找**最新**的 sidecar，返回 (路径, meta, 年龄秒) 或 (None, None, None)。

    一个目录里理论上只有一个在下的文件；真出现多个就取 mtime 最新的那个
    （= 正在被写的那个；旧的要么是中断残留、要么是另一条线的半成品）。
    """
    newest = None
    try:
        for path in glob.glob(os.path.join(directory, "*.download.json")):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if newest is None or mtime > newest[0]:
                newest = (mtime, path)
    except OSError:
        return None, None, None

    if newest is None:
        return None, None, None

    mtime, path = newest
    try:
        with open(path, encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        return path, None, None
    return path, meta, max(0.0, time.time() - mtime)


def allocated_bytes(path):
    """文件实际占用的字节数（sparse 预分配下 ≈ 已下载的字节数）。取不到返回 0。"""
    try:
        st = os.stat(path)
    except OSError:
        return 0
    return getattr(st, "st_blocks", 0) * 512


def progress_of(row):
    """算一条在下任务的进度。返回 (dict, sidecar 路径)。

    优先 sidecar（精确到块），退回已分配块数；两者都拿不到就给 None。
    """
    directory = pick(row, "path")
    if not directory or not os.path.isdir(directory):
        return None, None

    sidecar, meta, age = find_active_sidecar(directory)
    info = {"sidecar": os.path.basename(sidecar) if sidecar else "",
            "sidecar_age_s": int(age) if age is not None else None}

    if meta:
        size = meta.get("size") or 0
        chunk = meta.get("chunk_size") or 0
        done = meta.get("completed_chunks") or []
        if size > 0:
            bytes_done = min(len(done) * chunk, size) if chunk else 0
            info.update({
                "percent": round(100.0 * bytes_done / size, 2),
                "bytes_done": bytes_done,
                "bytes_total": size,
                "chunks_done": len(done),
                "chunks_total": (size + chunk - 1) // chunk if chunk else 0,
                "source": "sidecar",
                "file": os.path.basename(meta.get("url", "").split("?")[0]) or "",
            })
            return info, sidecar

    # 没有可用 sidecar：用「已分配块数 / 台账大小」估一个粗略百分比
    size_gb = pick(row, "size_gb")
    if size_gb:
        try:
            total = float(size_gb) * (1024 ** 3)
        except ValueError:
            total = 0
        if total > 0:
            target = None
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                names = []
            for name in names:
                if name.startswith(".") or name.endswith(
                        (".download.json", ".aria2", ".verified.json", ".aria2__temp")):
                    continue
                target = os.path.join(directory, name)
                break
            used = allocated_bytes(target) if target else 0
            if used > 0:
                info.update({
                    "percent": round(min(100.0, 100.0 * used / total), 2),
                    "bytes_done": used,
                    "bytes_total": int(total),
                    "chunks_done": None,
                    "chunks_total": None,
                    "source": "blocks",
                    "file": os.path.basename(target),
                })
                return info, sidecar

    return info, sidecar


def load_rows(csv_path):
    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def snapshot(csv_path, limit, previous, previous_at):
    """读一次台账 + 扫一次在下任务，拼出这一帧。previous = 上一次的 {key: bytes_done}。"""
    rows = load_rows(csv_path)
    counts = {}
    active = []
    recent = []

    for row in rows:
        status = pick(row, "status").upper()
        counts[status] = counts.get(status, 0) + 1

        entry = {
            "species": pick(row, "species"),
            "taxid": pick(row, "taxid"),
            "status": status,
            "path": pick(row, "path"),
            "size_gb": pick(row, "size_gb"),
            "updated": pick(row, "updated"),
            "note": pick(row, "notes"),
        }

        if status in ACTIVE_STATES:
            info, _ = progress_of(row)
            if info:
                entry.update(info)
            age = entry.get("sidecar_age_s")
            entry["live"] = bool(age is not None and age <= LIVE_WINDOW_S)
            active.append(entry)
        elif status in DONE_STATES:
            recent.append(entry)

    # 在下任务：真在传输的排前面，其次按进度、物种名
    active.sort(key=lambda e: (not e.get("live"), -(e.get("percent") or -1), e.get("species", "")))
    # 最近完成：按台账 updated 时间倒序（字符串就是可排序的时间格式）
    recent.sort(key=lambda e: e.get("updated", ""), reverse=True)
    recent = recent[:limit]

    # 速度：只在 watch 模式下有上一帧可比
    stamp = time.time()
    if previous is not None and previous_at is not None and stamp > previous_at:
        dt = stamp - previous_at
        for entry in active:
            key = entry.get("species", "") + "|" + entry.get("path", "")
            done = entry.get("bytes_done")
            if done is None or key not in previous:
                continue
            delta = done - previous[key]
            if delta >= 0:
                entry["speed_bps"] = int(delta / dt)

    current = {e.get("species", "") + "|" + e.get("path", ""): e.get("bytes_done")
               for e in active if e.get("bytes_done") is not None}

    return {
        "ok": True,
        "schema": SCHEMA,
        "generated": now_text(),
        "host": socket.gethostname(),
        "csv": csv_path,
        "counts": counts,
        "total": len(rows),
        "done": sum(counts.get(s, 0) for s in DONE_STATES),
        "pending": sum(counts.get(s, 0) for s in ACTIVE_STATES) + counts.get("QUEUED", 0),
        "active": active,
        "active_live": sum(1 for e in active if e.get("live")),
        "recent": recent,
    }, current, stamp


def size_text(entry, width=11):
    """台账里 size_gb 可能是空、0 或非数字 —— 一律渲染成等宽的空白，不要抛异常。"""
    try:
        value = float(entry.get("size_gb") or 0)
    except (TypeError, ValueError):
        value = 0
    return f"{value:7.1f} GB" if value > 0 else " " * width


def render_text(frame):
    """人看的版本（Linux 上直接跑 dw_tasks.py --text）。"""
    print(f"== Linux 侧下载任务（{frame['generated']} @ {frame['host']}）")
    print(f"   台账：{frame['csv']}")
    line = "   ".join(f"{k}={v}" for k, v in sorted(frame["counts"].items()))
    print(f"   共 {frame['total']} 条：{line}")
    print()
    print(f"   在下 {len(frame['active'])} 个（其中正在传输 {frame['active_live']} 个）：")
    for entry in frame["active"]:
        percent = entry.get("percent")
        bar = f"{percent:5.1f}%" if percent is not None else "  ?  "
        speed = entry.get("speed_bps")
        speed_text = f"  {speed / 1048576:5.2f} MiB/s" if speed else ""
        live = ">" if entry.get("live") else " "
        print(f"   {live} {bar}  {size_text(entry)}  {entry['species'][:36]:36s} {entry['status']}{speed_text}")
        if entry.get("file"):
            print(f"              {entry['file']}")
    print()
    print(f"   最近完成 {len(frame['recent'])} 条：")
    for entry in frame["recent"]:
        print(f"     {size_text(entry)}  {entry['species'][:36]:36s} {entry['status']}  {entry['updated']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Linux 侧只读任务视图（给 download-worker 用）")
    parser.add_argument("--status-csv", default="", help="下载台账 CSV；不给则自动探测")
    parser.add_argument("--limit", type=int, default=5, help="「最近完成」显示几条（默认 5）")
    parser.add_argument("--json", action="store_true", help="输出一行 JSON（默认动作）")
    parser.add_argument("--text", action="store_true", help="输出人看的表格")
    parser.add_argument("--watch", type=int, default=0, metavar="SEC",
                        help="每 SEC 秒输出一行 JSON，直到管道断开")
    args = parser.parse_args(argv)

    csv_path, checked = find_csv(args.status_csv)
    if not csv_path or not os.path.isfile(csv_path):
        error = {
            "ok": False,
            "schema": SCHEMA,
            "generated": now_text(),
            "error": "找不到下载台账 CSV",
            "checked": checked or [os.path.expanduser(args.status_csv)],
            "hint": "用 --status-csv 指定，或设环境变量 DW_STATUS_CSV",
        }
        if args.text:
            print(f"ERROR: {error['error']}（找过：{', '.join(error['checked'])}）")
        else:
            print(json.dumps(error, ensure_ascii=False), flush=True)
        return 2

    if args.watch > 0:
        previous, previous_at = None, None
        while True:
            try:
                frame, previous, previous_at = snapshot(csv_path, args.limit, previous, previous_at)
            except Exception as error:      # 读台账失败也不能把 watch 打死：报一帧错误继续
                frame = {"ok": False, "schema": SCHEMA, "generated": now_text(),
                         "csv": csv_path, "error": repr(error)}
            print(json.dumps(frame, ensure_ascii=False), flush=True)
            try:
                time.sleep(args.watch)
            except KeyboardInterrupt:
                return 0

    frame, _, _ = snapshot(csv_path, args.limit, None, None)
    if args.text:
        render_text(frame)
    else:
        print(json.dumps(frame, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:                 # Windows 侧关了管道（Ctrl+C / ssh 断开）
        # 光 exit 不够：解释器退出时会**再 flush 一次** stdout，那时管道已经没了，
        # 于是远端 stderr 多出一行 "Exception ignored in: ... BrokenPipeError"，
        # 而 process 面板把非 JSON 行原样打出来 —— 用户按 Ctrl+C 就会看到这行噪声。
        # 把 stdout 指到 /dev/null 再退，flush 就落在空设备上。
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(0)
