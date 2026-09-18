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

3. **别的 driver 的台账 CSV**（`EXTRA_LEDGERS`，见下）—— 几路 driver 各写各的台账时，
   想在**一个监控面**里看全部下载：把它们的 state 台账并进同一帧，归属标签挂在物种名前面
   （`<owner>·<species>`），Windows 侧渲染不用改。

   | 归属 | 台账 | 列 |
   |---|---|---|
   | `<owner>` | 配置文件里给的 glob | `genbank,state,out,size,md5,ts` |

   列名口径与主台账不同，`normalize_extra()` 归一化：物种名从 `out` 路径反推
   （`<...>/genome/<Genus>/<Genus_species_taxid>/<file>`），状态映射
   `downloading->DOWNLOADING / downloaded->DONE / failed->FAILED`。
   这类行的 `ts` 也当一路活性信号用（见 `is_live`）。

   **时间口径统一成 UTC+8**：主台账按 +8 写、别的台账可能按 UTC 写 —— 每份台账在
   配置文件 / `MAIN_TZ_H` 里声明自己的偏移，换算成 epoch 后
   **排序用 epoch、显示用 UTC+8**，多份混排才是实际时间顺序。

**本机的具体路径都不写在这个文件里**（它是公开的），一律放配置文件
（`~/.config/download-worker/dw_tasks.json`，可用环境变量 `DW_TASKS_CONFIG` 换位置）：
`status_csv_candidates`（主台账候选路径）、`extra_ledgers`（别的 driver 的台账）、`main_tz_h`。
文件不存在就用中性默认，功能照常、只是少几路台账。

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
import calendar
import csv
import glob
import json
import os
import socket
import sys
import time
from datetime import datetime, timedelta, timezone

SCHEMA = 1

# 本机的具体路径走配置文件，**不写死在这个（公开的）文件里**。
# 位置：`~/.config/download-worker/dw_tasks.json`，可用 DW_TASKS_CONFIG 覆盖。
# 认得的键（都可以缺，缺了就用中性默认）：
#   status_csv_candidates : [路径, ...]     主台账 CSV 候选（按顺序取第一个存在的）
#   main_tz_h             : 8               主台账时间列的时区偏移（相对 UTC）
#   extra_ledgers         : [{glob, owner, kind, tz_h}, ...]   别的 driver 的台账
DEFAULT_CONFIG_PATH = "~/.config/download-worker/dw_tasks.json"


def load_config(path=None):
    """读配置文件；没有 / 读不动就返回中性默认（功能照常，只是少几路台账）。"""
    cfg = {"status_csv_candidates": None, "main_tz_h": None, "extra_ledgers": None}
    path = path or os.environ.get("DW_TASKS_CONFIG") or DEFAULT_CONFIG_PATH
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return cfg
    if isinstance(raw, dict):
        for key in cfg:
            if key in raw:
                cfg[key] = raw[key]
    return cfg


CONFIG = load_config()

# 台账 CSV 的常见位置（按顺序取第一个存在的）。也可以用环境变量 DW_STATUS_CSV 指定，
# 或在配置文件里用 status_csv_candidates 覆盖。
CSV_CANDIDATES = CONFIG["status_csv_candidates"] or [
    DEFAULT_CONFIG_PATH.rsplit("/", 1)[0] + "/download_status.csv",
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

# 别人的 driver 台账： (glob, 归属, 列口径, 该台账时间列的时区偏移[小时, 相对 UTC])。
# 「一个监控面看全部下载」：这些行并进同一帧的 active / recent / counts，只多了个归属前缀。
# 条目来自配置文件的 extra_ledgers（公开代码里默认为空）；kind 目前只认 "extra"。
#
# ⚠ 时间口径必须声明对：几路台账默认写得不一样（一路按 +8 写、另一路按 UTC 写是常事）。
# 混排时不声明就会排错序（实测：18:41(+8) = 10:41Z 被排到 15:58Z 前面，看着像时间倒流）。
EXTRA_LEDGERS = []
for _spec in (CONFIG["extra_ledgers"] or []):
    if isinstance(_spec, dict) and _spec.get("glob"):
        EXTRA_LEDGERS.append((_spec["glob"], _spec.get("owner") or "other",
                              _spec.get("kind") or "extra", _spec.get("tz_h", 0.0)))
    elif isinstance(_spec, (list, tuple)) and _spec:      # 也认 [glob, owner, kind, tz_h]
        EXTRA_LEDGERS.append(tuple(_spec))

# 主台账时间列的时区偏移（配置文件可覆盖）
MAIN_TZ_H = 8 if CONFIG["main_tz_h"] is None else CONFIG["main_tz_h"]

# 显示口径：一律 UTC+8（和主台账、和给用户的所有报告一致）
DISPLAY_TZ = timezone(timedelta(hours=8))

# 别的 driver 台账里的 state 取值 -> 本脚本的状态口径
EXTRA_STATES = {
    "downloading": "DOWNLOADING",
    "downloaded": "DONE",
    "failed": "FAILED",
    "queued": "QUEUED",
    "pending": "QUEUED",
}

# 扫目录找「在写文件」时要跳过的后缀（sidecar / aria2 痕迹自己不是下载目标）
SKIP_SUFFIXES = (".download.json", ".aria2", ".verified.json", ".aria2__temp")


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


def text_epoch(text, tz_h=0.0):
    """台账里的时间文本 -> epoch 秒；认不出来返回 None。

    tz_h = **该台账时间列的时区偏移**（相对 UTC 的小时数）。先用 timegm 按 UTC 解析再减偏移，
    所以与进程 TZ 无关；两份台账口径不同也各自算得对（见 EXTRA_LEDGERS 的说明）。
    """
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return calendar.timegm(time.strptime(text, fmt)) - tz_h * 3600.0
        except ValueError:
            continue
    return None


def cst_text(epoch):
    """epoch -> UTC+8 的 'YYYY-MM-DD HH:MM'（显示口径，和主台账一致）。"""
    return datetime.fromtimestamp(epoch, DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")


def newest_file_age(directory):
    """目录里最新那个「下载目标」的 mtime 年龄（秒）；没有返回 None。

    这是 sidecar 之外的第二路活性信号：下载器每写完一块就写文件，文件 mtime 跟着动。
    某些时候 sidecar 还没落（刚起、或上一轮刚被终验删掉）而文件在长，光看 sidecar 会误判成「没在传」。
    """
    newest = None
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for name in names:
        if name.startswith(".") or name.endswith(SKIP_SUFFIXES):
            continue
        try:
            mtime = os.path.getmtime(os.path.join(directory, name))
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return None if newest is None else max(0.0, time.time() - newest)


def activity_age(entry):
    """这条最近一次「动过」是多久以前（秒）；三路都没有就给一个很大的数（排最后）。"""
    ages = [entry.get(k) for k in ("sidecar_age_s", "mtime_age_s", "updated_age_s")]
    ages = [age for age in ages if age is not None]
    return min(ages) if ages else 10 ** 9


def is_live(entry):
    """三路取最新：sidecar 新鲜度 / 目标文件 mtime / 台账 ts。

    任一路落在 LIVE_WINDOW_S 内就算「此刻真在传输」。台账里挂着 DOWNLOADING 但三路都停了的，
    是中断残留 —— 两者必须分开显示。
    """
    return activity_age(entry) <= LIVE_WINDOW_S


def species_from_out(out):
    """从 `<...>/genome/<Genus>/<Genus_species_taxid>/<file>` 反推物种名。

    目录名形如 `Genus_species_0000`（属_种_taxid）=> `Genus species`。
    反推不出来就返回空串（调用方退回 accession）。
    """
    parts = os.path.normpath(out or "").split(os.sep)
    for chunk in reversed(parts[:-1]):          # 从文件名往上找第一个像物种目录的
        bits = chunk.split("_")
        if len(bits) > 2 and bits[-1].isdigit():
            bits = bits[:-1]
        if len(bits) >= 2 and all(bits):
            return " ".join(bits)
    return ""


def normalize_extra(row, owner, source_tz=0.0):
    """别的 driver 台账的一行 -> 主台账那套列名，让下面的循环认不出来差别。

    时间统一换算成显示口径（UTC+8）：源台账写的是 UTC，转过来「最近完成」才排得对、标得一致。
    """
    out = (row.get("out") or "").strip()
    state = (row.get("state") or "").strip().lower()
    size = (row.get("size") or "").strip()

    size_gb = ""
    if size.isdigit() and int(size) > 0:
        size_gb = "%.3f" % (int(size) / (1024 ** 3))

    species = species_from_out(out)
    raw_ts = (row.get("ts") or "").strip()
    entry = {
        "species_name": ("%s·%s" % (owner, species)) if species else owner,
        "status": EXTRA_STATES.get(state, state.upper()),
        "path": os.path.dirname(out),
        "size_gb": size_gb,
        "updated": raw_ts,
        "notes": os.path.basename(out),
        "owner": owner,
        "genbank": (row.get("genbank") or "").strip(),
    }
    epoch = text_epoch(raw_ts, source_tz)
    if epoch is not None:
        entry["updated"] = cst_text(epoch)              # 显示统一 UTC+8
        entry["updated_epoch"] = int(epoch)
        entry["updated_age_s"] = int(max(0.0, time.time() - epoch))
    return entry


def extra_rows():
    """读所有 EXTRA_LEDGERS，返回 (归一化后的行, 来源清单)。读不到某份就跳过，不影响主台账。"""
    rows, ledgers = [], []
    for spec in EXTRA_LEDGERS:
        pattern, owner, kind = spec[0], spec[1], spec[2]
        source_tz = spec[3] if len(spec) > 3 else 0.0
        for path in sorted(glob.glob(os.path.expanduser(pattern))):
            try:
                raw = load_rows(path)
            except (OSError, UnicodeDecodeError):
                continue
            mapped = ([normalize_extra(r, owner, source_tz) for r in raw]
                      if kind == "extra" else [])
            rows.extend(mapped)
            ledgers.append({"csv": path, "owner": owner, "rows": len(mapped)})
    return rows, ledgers


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
    file_age = newest_file_age(directory)
    info = {"sidecar": os.path.basename(sidecar) if sidecar else "",
            "sidecar_age_s": int(age) if age is not None else None,
            "mtime_age_s": int(file_age) if file_age is not None else None}

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
                if name.startswith(".") or name.endswith(SKIP_SUFFIXES):
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
    """读一次台账 + 扫一次在下任务，拼出这一帧。previous = 上一次的 {key: bytes_done}。

    主台账 + 所有 EXTRA_LEDGERS（别的 driver 的线）合成一个列表处理：计数、在下、最近完成
    都是「全部下载」的口径，归属只体现在物种名的 `<owner>·` 前缀与 `owner` 字段上。
    """
    rows = load_rows(csv_path)
    extra, ledgers = extra_rows()
    counts = {}
    active = []
    recent = []

    for row in rows + extra:
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
        if row.get("owner"):
            entry["owner"] = row["owner"]
        if row.get("updated_age_s") is not None:
            entry["updated_age_s"] = row["updated_age_s"]

        # 时间：extra 行在 normalize 时已换算好；主台账的行在这里按 MAIN_TZ_H 换算。
        # 两边的 epoch 都拿到，「最近完成」才排得出实际顺序（+8 与 UTC 比字符串会倒序）。
        epoch = row.get("updated_epoch")
        if epoch is None and entry["updated"]:
            epoch = text_epoch(entry["updated"], MAIN_TZ_H)
        if epoch is not None:
            entry["updated_epoch"] = int(epoch)

        if status in ACTIVE_STATES:
            info, _ = progress_of(row)
            if info:
                entry.update(info)
                # 台账 size 列是空的（在下中的行往往还没量过），就用 sidecar 报的全文件大小补上，
                # Windows 侧那一列才不是空白。
                if not entry.get("size_gb") and info.get("bytes_total"):
                    entry["size_gb"] = "%.3f" % (info["bytes_total"] / (1024 ** 3))
            entry["live"] = is_live(entry)
            active.append(entry)
        elif status in DONE_STATES:
            recent.append(entry)

    # 在下任务：真在传输的排前面；其次按「最近动过」（三路活性里最新的一路，即实际顺序），
    # 再次按进度、物种名。三路都没动过的排最后 —— 它们是待重排队的残留。
    active.sort(key=lambda e: (not e.get("live"), activity_age(e), -(e.get("percent") or -1),
                               e.get("species", "")))
    # 最近完成：按 epoch 倒序。**不能比字符串** —— 一路台账写 +8、另一路写 UTC，
    # 混排时 18:41(CST)=10:41Z 会排到 15:58Z 前面，看着像时间倒流。
    recent.sort(key=lambda e: e.get("updated_epoch") or 0, reverse=True)
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
        "ledgers": ledgers,
        "counts": counts,
        "total": len(rows) + len(extra),
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
    for ledger in frame.get("ledgers") or []:
        print(f"   + {ledger['owner']} 台账：{ledger['csv']}（{ledger['rows']} 行）")
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
