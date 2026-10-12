#!/usr/bin/env python3
"""dw_tasks.py - Linux 侧只读任务视图（给 Windows 的 `download-worker status` / `process` 用）。

这个脚本**只读**：不下载、不改配置、不碰任何正在写的文件。它把 Linux 侧**已经存在**的两份
产物读出来拼成 JSON，交给 Windows 侧渲染。

数据来源
--------
0. **预计算快照**（可选；配了 `cache_path` 才启用）—— 就是下面 1–3 全部算完之后的那一帧，
   存在**本地快盘**上的一个文件里（`{written_epoch, written, frame}`）。命中且新鲜就直接回放，
   **一次网络挂载都不碰**。

   为什么要有这一层：一帧的代价几乎全在「在下」那一列 —— 每条都要进目标目录扫 sidecar
   （`glob` + 逐个 `stat`），一帧 200+ 次 stat。挂载病态时实测 **32.9 s（其中 stat 独占 33.1 s，
   CPU 合计 0.23 s）**，而远端 `status` 的 ssh 段上限只有 60 s：读得出也等不起。
   把整帧挪到快盘上算一次，谁来读都是毫秒级。

   本脚本对快照**只读**：它由另一侧的常驻程序定期烤（谁烤、多久烤一次是那边的事）。
   快照过期或坏掉就退回现算，绝不会拿一份馊的顶上。

1. **下载台账 CSV**（`--status-csv`，不给就按 `--csv-candidates` 自动探测）
   一行一个目标文件，约定列：`name,key,group,tier,status,path,size_gb,updated,notes`
   -> 状态计数、当前在下（`DOWNLOADING` / `REPAIRING`）的任务、最近完成。
   列名认得几种常见写法（`name`/`item`/`target`，`size_gb`/`size`），缺列只是少一项信息。

2. **在下任务目录里的 sidecar `<输出文件>.download.json`**（下载器写的断点文件）
   `{"url":..., "size":..., "chunk_size":..., "completed_chunks":[...]}`
   -> 精确进度（已完成块 -> 字节 -> 百分比）、块计数、sidecar 新鲜度。
   找不到 sidecar 时退回**已分配块数**（`st_blocks`）。
   ⚠ 下载器一开始就把目标文件 sparse 预分配到全尺寸，所以 `ls -l` 的大小**不代表进度**，
   唯一可靠的两个来源就是 sidecar 与已分配块数。

3. **别的 driver 的台账 CSV**（`EXTRA_LEDGERS`，见下）—— 几路 driver 各写各的台账时，
   想在**一个监控面**里看全部下载：把它们的 state 台账并进同一帧，归属标签挂在名字前面
   （`<owner>·<name>`），Windows 侧渲染不用改。

   | 归属 | 台账 | 列 |
   |---|---|---|
   | `<owner>` | 配置文件里给的 glob | `accession,state,out,size,md5,ts` |

   列名口径与主台账不同，`normalize_extra()` 归一化：名字从 `out` 路径反推
   （`<...>/<group>/<name>_<key>/<file>`，即 `paths.outdir_template` 的布局），状态映射
   `downloading->DOWNLOADING / downloaded->DONE / failed->FAILED`。
   这类行的 `ts` 也当一路活性信号用（见 `is_live`）。

   **时间口径统一成 UTC+8**：主台账按 +8 写、别的台账可能按 UTC 写 —— 每份台账在
   配置文件 / `MAIN_TZ_H` 里声明自己的偏移，换算成 epoch 后
   **排序用 epoch、显示用 UTC+8**，多份混排才是实际时间顺序。

**本机的具体路径都不写在这个文件里**（它是公开的），一律放配置文件
（`~/.config/download-worker/dw_tasks.json`，可用环境变量 `DW_TASKS_CONFIG` 换位置）：
`status_csv_candidates`（主台账候选路径）、`extra_ledgers`（别的 driver 的台账）、`main_tz_h`、
`cache_path` / `cache_max_age_s`（预计算快照的位置与保质期；不配 = 不启用这一层）、
`ledger_path`（**中转盘上的下载台账**：在下的任务先问它，命中就一次挂载都不碰）、
`scan_budget_s`（兜底扫目录的时间预算；到点就跳过剩余扫描，宁可标 skipped 也不拖死）。
文件不存在就用中性默认，功能照常、只是少几路台账。

用法
----
    python3 dw_tasks.py --json            # 一次性快照（download-worker status 用）
    python3 dw_tasks.py --watch 2         # 每 2 秒输出一行 JSON（download-worker process 用）
    python3 dw_tasks.py --no-cache        # 忽略快照，强制现算（排查「快照是不是馊了」用）
    python3 dw_tasks.py --text            # 人看的表格（在 Linux 上直接跑）
    python3 dw_tasks.py --limit 5         # 「最近完成」显示几条（默认 5）
    python3 dw_tasks.py --channel <口>    # 只看这个通道口（某台 PC 的隧道口）在下的任务

`--watch` 是 **JSONL**（一行一个对象、写完即 flush），断了管道（Ctrl+C / ssh 断开）就退出。
速度（`speed_bps`）只在 `--watch` 里有：靠相邻两次采样的差值算，一次性快照给不出速度。

**JSON 一律 ASCII 上线**（`ensure_ascii=True`，别改回 False）：这份输出是走 ssh 送到
Windows 控制台的，那一边按**本地代码页**解码。raw 非 ASCII 会被拆坏 —— 实测中文字节的
尾字节会**把紧随其后的引号一起吃掉**，于是 JSON 结构破损、调用方只看到"取不到 Linux 侧任务"
（备注里一个奇数长度的中文串就足以触发）。转义成 `\\uXXXX` 后线路上**全是 ASCII**，
任何代码页解出来都一样，解析端（ConvertFrom-Json / json.loads）会还原成正确中文。
`--text` 是给人看的、不走这条路，保持中文原文。
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
#   cache_path            : 预计算快照的文件（本地快盘上）；配了才启用，缺省不用
#   cache_max_age_s       : 600             快照多久算过期（过期就现算，不拿馊的顶上）
#   ledger_path           : 中转台账（快盘上）；有它的行不再去扫网络挂载
#   scan_budget_s         : 0               兜底扫目录的时间预算（秒）；0 = 不限
DEFAULT_CONFIG_PATH = "~/.config/download-worker/dw_tasks.json"


def load_config(path=None):
    """读配置文件；没有 / 读不动就返回中性默认（功能照常，只是少几路台账）。"""
    cfg = {"status_csv_candidates": None, "main_tz_h": None, "extra_ledgers": None,
           "cache_path": None, "cache_max_age_s": None,
           "ledger_path": None, "scan_budget_s": None}
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
    "name": ("name", "item", "target", "label"),
    "key": ("key", "item_id", "id"),
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
# 混排时不声明就会排错序（实测：两边的钟不一样，看着像时间倒流）。
EXTRA_LEDGERS = []
for _spec in (CONFIG["extra_ledgers"] or []):
    if isinstance(_spec, dict) and _spec.get("glob"):
        EXTRA_LEDGERS.append((_spec["glob"], _spec.get("owner") or "other",
                              _spec.get("kind") or "extra", _spec.get("tz_h", 0.0)))
    elif isinstance(_spec, (list, tuple)) and _spec:      # 也认 [glob, owner, kind, tz_h]
        EXTRA_LEDGERS.append(tuple(_spec))

# 主台账时间列的时区偏移（配置文件可覆盖）
MAIN_TZ_H = 8 if CONFIG["main_tz_h"] is None else CONFIG["main_tz_h"]

# 预计算快照：空串 = 不启用（老行为，一帧全现算）。
# 保质期给得比「烤的快照间隔」宽（烤 3 分钟一次、这里 10 分钟）—— 烤的那头偶尔打个盹，
# 读的人不该立刻掉回 30 s 的现算；真过期了就现算，宁可慢也不馊。
CACHE_PATH = (CONFIG["cache_path"] or "").strip()
CACHE_MAX_AGE_S = 600 if CONFIG["cache_max_age_s"] is None else float(CONFIG["cache_max_age_s"])

# 中转台账（驱动器写、放本地快盘上的 JSONL：首行 meta，其后一行一个任务）。有它的行
# **不再扫目标目录** —— 那才是这一帧真正贵的地方（目标目录在网络挂载上，一次 200+ stat，
# 实测单次 stat 0.16 s）。空串 = 不启用（老行为：每条都自己去扫）。
LEDGER_PATH = (CONFIG["ledger_path"] or "").strip()
# 兜底扫描的时间预算（秒）：没有台账命中的行仍要扫目录，NFS 病态时一条就 1 s+。给个上限，
# 超预算的行就不扫了（照常出现在列表里，只是没有当场测出来的百分比）。0 = 不限（老行为）。
SCAN_BUDGET_S = 0.0 if CONFIG["scan_budget_s"] is None else float(CONFIG["scan_budget_s"])

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

# 中转台账里**还在跑、却没有任何一行代表它**的件，补出来的行在 note 打这个记号。
# 措辞与管家端同一份实现保持一致，两侧读到的帧才是同一种说法。
LIVE_NOTE = "驱动在跑(台账补行)"


def now_text():
    """帧里的 `generated` 时间戳。**必须走 DISPLAY_TZ**：本机系统时区是 UTC，
    裸 `now()` 写出的是 UTC，与同一帧里其余（已换算成 UTC+8 的）时间差 8 小时。"""
    return datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")


def channel_from_argv(argv):
    """一条 `linux_downloader.py` 的命令行 → 通道名（`'linux'` / `'<口>'` …）。

    判据：看 `--worker` 的 URL 里的端口（`--direct` 没有这个参数 ⇒ 算 linux 直连）。
    端口**不写死**：取 URL 末尾那段当通道名，将来多一台 PC 也不用改这里。
    **纯函数**，单测直接打它。
    """
    worker = ""
    for index, arg in enumerate(argv):
        if arg == "--worker" and index + 1 < len(argv):
            worker = argv[index + 1]
        elif arg.startswith("--worker="):
            worker = arg.split("=", 1)[1]
    if worker:
        head, _, tail = worker.rstrip("/").rpartition(":")
        if head and tail.isdigit():
            return tail
    return "linux"


def running_channels():
    """{输出文件绝对路径: 通道} —— 扫 /proc，看**此刻**哪条通道在写哪个文件。

    只做「谁在写哪个文件」这一件事（进度/速率另有工具）：本文件对外是单文件发布的，
    所以这里自带一份精简扫描、不外引模块。任何异常一律当"扫不到"（返回 {}）——
    通道归属只是锦上添花，绝不能拖垮主视图。
    """
    found = {}
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return found
    for pid in pids:
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as handle:
                argv = [part.decode("utf-8", "replace")
                        for part in handle.read().split(b"\0") if part]
        except OSError:
            continue
        if not any(arg.endswith("linux_downloader.py") for arg in argv):
            continue
        out = ""
        for index, arg in enumerate(argv):
            if arg == "-o" and index + 1 < len(argv):
                out = argv[index + 1]
            elif arg.startswith("-o="):
                out = arg.split("=", 1)[1]
        if out:
            found[out] = channel_from_argv(argv)
    return found


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
    """这条最近一次「动过」是多久以前（秒）；几路都没有就给一个很大的数（排最后）。

    `ledger_age_s`：进度是从中转台账取的（那条线此刻真在写它）—— 台账刚写 = 这条活着。
    """
    ages = [entry.get(k) for k in ("sidecar_age_s", "mtime_age_s", "ledger_age_s", "updated_age_s")]
    ages = [age for age in ages if age is not None]
    return min(ages) if ages else 10 ** 9


def is_live(entry):
    """三路取最新：sidecar 新鲜度 / 目标文件 mtime / 台账 ts。

    任一路落在 LIVE_WINDOW_S 内就算「此刻真在传输」。台账里挂着 DOWNLOADING 但三路都停了的，
    是中断残留 —— 两者必须分开显示。
    """
    return activity_age(entry) <= LIVE_WINDOW_S


def name_from_out(out):
    """Recover an item name from a landing path such as
    `<...>/<group>/<name>_<key>/<file>` -- the layout given by `paths.outdir_template`.

    The directory is read as `<token>_<token>[_<key>]`: a trailing numeric key is
    dropped and the remaining tokens are joined with spaces.  Returns an empty
    string when the path does not look like that, and the caller falls back.
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

    name = name_from_out(out)
    raw_ts = (row.get("ts") or "").strip()
    entry = {
        "name": ("%s·%s" % (owner, name)) if name else owner,
        "status": EXTRA_STATES.get(state, state.upper()),
        "path": os.path.dirname(out),
        "size_gb": size_gb,
        "updated": raw_ts,
        "notes": os.path.basename(out),
        "owner": owner,
        "accession": (row.get("accession") or "").strip(),
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


def load_ledger(path):
    """读中转台账（JSONL），返回 ({目标目录: [进度信息, ...]}, 台账年龄秒)。

    只取 `state == "running"` 的行：终态行里的 `pct` 是「结束时下到哪」，混进来会把
    「已经完成的」显示成「正在下」。读不动 / 没配就返回 `({}, None)`，退回挨个扫目录的老路
    —— 慢，但不丢信息。

    **值一律是列表**：一个目标目录下**可能同时有多条在跑的** —— 同物种并发两份是常态，
    而台账行是**逐文件**的。给「一个物种一行」的台账行找进度时取第一条；**剩下的那条由
    `live_entries()` 补成独立的行**，否则它在帧里根本没有行（用户报的「在下载不显示」
    「某个口又显示无下载任务」就是这里漏的）。

    年龄按**文件 mtime** 算：台账由驱动侧每轮原子重写（tmp + rename），mtime 就是它
    最后一次现身的时间；这个数直接当「这条此刻真在写」的活性信号用。
    """
    path = os.path.expanduser(path)
    try:
        age = max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return {}, None
    out = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("state") != "running":
                    continue
                target = row.get("outdir")
                size = row.get("size") or 0
                done = row.get("done") or 0
                if not target or not size:
                    continue
                out.setdefault(target, []).append({
                    # 夹住：块粒度进位会让 `done` 略微超过声明的 size（最后一块不满也按整块算），
                    # 不夹就会显示成「100.1%」这种没意义的数。
                    "percent": round(min(100.0, 100.0 * done / size), 2),
                    "bytes_done": done,
                    "bytes_total": size,
                    "source": "ledger",
                    "port": str(row.get("port") or ""),
                    "file": row.get("name") or "",
                    "item": str(row.get("item") or ""),     # 目标名：补行时不必再从路径反推
                })
    except OSError:
        return {}, None
    return out, age


def progress_of(row, ledger=None, ledger_age=None, deadline=None):
    """算一条在下任务的进度。返回 (dict, sidecar 路径)。

    顺序：**中转台账 → sidecar → 已分配块数**。

    台账那一支是唯一**不碰网络挂载**的 —— 目标目录在挂载上，连 `isdir` 都是一次
    0.16 s 的 stat，一条就能顶掉整帧的预算。所以命中台账就直接返回，连目录存在与否都不问
    （此刻真正在写的其实是中转窗口，那个目录在本地盘上）。

    后两支要扫目录，受 `deadline` 约束：超预算就不扫了，行照常出现在列表里，
    只是没有当场测出来的百分比（`progress_source = "skipped"`）。
    """
    directory = pick(row, "path")
    if not directory:
        return None, None

    hits = (ledger or {}).get(directory)
    if hits:
        # 同目录并发两份时这里只认领第一条；**第二条不丢** —— snapshot() 末尾用
        # live_entries() 把它补成独立的行（少了那一步，它在帧里就是隐形的）。
        info = dict(hits[0])
        info["sidecar"] = ""
        info["sidecar_age_s"] = None
        info["mtime_age_s"] = None
        info["ledger_age_s"] = ledger_age
        return info, None

    if deadline is not None and time.time() > deadline:
        return {"progress_source": "skipped", "sidecar": "",
                "sidecar_age_s": None, "mtime_age_s": None}, None

    if not os.path.isdir(directory):
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
            # 独立提交（急件线，chunk_commit=independent）sidecar 直接给了 done_bytes
            # （已 fsync 的块长度之和）⇒ 用它，末块不满时比 len×chunk 准。
            # 老 sidecar 没有这个键，照旧按块数乘。
            bytes_done = (meta.get("done_bytes") if meta.get("done_bytes") is not None
                          else (min(len(done) * chunk, size) if chunk else 0))
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


def active_key(entry):
    """一帧里「同一条在下任务」的键：目标名 + 目标目录 + **件名**。

    **件名不能省**：同一个目标、同一个目录下可以并发两份，只按「目标名 + 目录」当键，
    两条会共用一份速度基线、续帧时还会互相覆盖（补 `live_entries()` 时一并修）。
    """
    return "|".join((entry.get("name", ""), entry.get("path", ""), entry.get("file", "")))


def live_entries(ledger, ledger_age, channels, by_name, seen):
    """中转台账里**还在跑、却没有任何一行代表它**的件 → 补成帧里独立的 entry。

    为什么必须补：台账 CSV 是**一个目标一行**、`progress_of()` 每个目录也只认领一条 ——
    同一个目录**并发两份**时，**第二条在帧里完全没有行**；不光看不见，按口过滤
    （`--channel`，对端 `status` 就是这么读的）时它还会被整条丢掉 ⇒ 一个正在以几 MiB/s
    下载的口显示成「在下 0 个 / 此刻没有在下的任务」。

    台账行是**逐文件**的事实，由驱动侧每轮重写、幽灵行另有存活清理兜底，拿它补齐最省事：
    不碰网络挂载、不另探活、也不用猜。`seen` = 上面已认领的「目标全路径」集合，
    命中即跳过 —— **绝不与既有行重复**。

    口：优先按 `/proc` 的活体线认（那是「此刻真在写」），认不出才退回台账自己记的口。
    """
    out = []
    for folder, infos in sorted((ledger or {}).items()):
        for info in infos:
            name = info.get("file") or ""
            if not name:
                continue
            target = os.path.normpath(os.path.join(folder, name))
            if target in seen:
                continue
            seen.add(target)
            label = info.get("item") or name_from_out(target) or name
            entry = {
                "name": label,
                "key": "",
                "status": "DOWNLOADING",
                "path": folder,
                "file": name,
                "size_gb": ("%.3f" % (info["bytes_total"] / (1024 ** 3))
                            if info.get("bytes_total") else ""),
                "updated": "",
                "note": LIVE_NOTE,
                "channel": channels.get(target) or by_name.get(name) or (info.get("port") or ""),
                # 台账里还是 running ⇒ 它此刻在跑（幽灵行已由驱动侧清理），活性不靠 mtime 猜
                "live": True,
                "source": "ledger-live",                        # 与 progress_source 区分：这条是补的
                "progress_source": info.get("source") or "ledger",
                "ledger_age_s": int(ledger_age) if ledger_age is not None else None,
            }
            for field in ("percent", "bytes_done", "bytes_total"):
                if info.get(field) is not None:
                    entry[field] = info[field]
            out.append(entry)
    return out


def load_rows(csv_path):
    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load_cache(path, max_age_s):
    """读预计算快照，返回 (帧, 年龄秒, 写入时刻)；没有 / 过期 / 坏了都返回 (None, None, None)。

    年龄按**写它的那一刻**算（`written_epoch`，由烤的那侧写进来），不看文件 mtime ——
    快照被搬运或复制过一次，mtime 就变成搬运时间，用它判会把一份隔夜的数据当成刚出炉的。
    """
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return None, None, None
    if not isinstance(raw, dict):
        return None, None, None
    frame, written = raw.get("frame"), raw.get("written_epoch")
    if not isinstance(frame, dict) or not frame.get("ok"):
        return None, None, None
    if not isinstance(written, (int, float)):
        return None, None, None
    age = max(0.0, time.time() - written)
    if max_age_s and age > max_age_s:
        return None, None, None
    return frame, age, float(written)


def apply_view(frame, channel, limit):
    """把一帧裁成调用方要的口径：通道过滤 + 「最近完成」条数。

    快照存的是**全量**帧（谁读都得能用），所以回放时要按调用方的参数再裁一次。口径与
    snapshot() 末尾那段一致：`active_total` / `active_live_total` 保持全量（它们是「全局」
    的注脚，跟着裁就等于报错数），只裁 `active` 与 `recent`。
    """
    active = list(frame.get("active") or [])
    if channel:
        active = [e for e in active if (e or {}).get("channel") == channel]
    frame["active"] = active
    frame["active_live"] = sum(1 for e in active if (e or {}).get("live"))
    frame["channel"] = channel
    recent = list(frame.get("recent") or [])
    frame["recent"] = recent[:limit] if limit and limit > 0 else recent
    return frame


def frame_speed(frame, previous, previous_written, written):
    """给回放出来的帧补 `speed_bps`，返回本帧的 {key: bytes_done}。

    间隔取**快照自己的写入时刻之差**，不是两次回放的间隔：同一条数据被回放十次，按回放间隔
    算出来的速度会放大十倍 —— 那是在测回放频率，不是传输速率。快照没换（写入时刻没往前走）
    就不补速度：宁可这一帧不显示速度，也不显示一个编出来的。
    """
    stamp = time.time()
    active = frame.get("active") or []
    current = {active_key(e): e.get("bytes_done")
               for e in active if e.get("bytes_done") is not None}
    dt = (written - previous_written) if (written is not None and previous_written is not None) else 0
    if previous is not None and dt > 0:
        for entry in active:
            key = active_key(entry)
            done = entry.get("bytes_done")
            if done is None or key not in previous:
                continue
            delta = done - previous[key]
            if delta >= 0:
                entry["speed_bps"] = int(delta / dt)
    return current, stamp


def serve_frame(csv_path, limit, channel, cache_path, previous, previous_at, previous_written):
    """取一帧：有新鲜快照就回放，没有/过期就现算。

    返回 (帧, 本帧 bytes 映射, 时刻, 快照写入时刻)。回放那一支会在帧里加 `cache`
    字段说明「这是回放、数据多旧」—— **不把一份快照伪装成现算的**。
    """
    if cache_path:
        frame, age, written = load_cache(cache_path, CACHE_MAX_AGE_S)
        if frame is not None:
            frame = apply_view(frame, channel, limit)
            frame["cache"] = {"path": os.path.expanduser(cache_path),
                              "age_s": int(age), "written_epoch": int(written)}
            current, stamp = frame_speed(frame, previous, previous_written, written)
            return frame, current, stamp, written
    frame, current, stamp = snapshot(csv_path, limit, previous, previous_at, channel)
    return frame, current, stamp, None


def snapshot(csv_path, limit, previous, previous_at, channel=None):
    """读一次台账 + 扫一次在下任务，拼出这一帧。previous = 上一次的 {key: bytes_done}。

    主台账 + 所有 EXTRA_LEDGERS（别的 driver 的线）合成一个列表处理：计数、在下、最近完成
    都是「全部下载」的口径，归属只体现在物种名的 `<owner>·` 前缀与 `owner` 字段上。
    """
    rows = load_rows(csv_path)
    extra, ledgers = extra_rows()
    # 进度来源优先级：中转台账（本地快盘，不碰网络挂载）→ 扫目标目录（贵，受时间预算约束）。
    ledger, ledger_age = load_ledger(LEDGER_PATH) if LEDGER_PATH else ({}, None)
    deadline = (time.time() + SCAN_BUDGET_S) if SCAN_BUDGET_S > 0 else None
    counts = {}
    active = []
    recent = []

    for row in rows + extra:
        status = pick(row, "status").upper()
        counts[status] = counts.get(status, 0) + 1

        entry = {
            "name": pick(row, "name"),
            "key": pick(row, "key"),
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
            info, _ = progress_of(row, ledger=ledger, ledger_age=ledger_age, deadline=deadline)
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

    # 通道归属：此刻哪条通道在写哪个文件，扫 /proc 得到。只有**正在传**的才可能有归属；
    # 台账里挂着的残留没有归属（要 driver 真派给某个口才算那个口的）。匹配先按
    # 「目标目录 + 文件名」，不中再退化为只比文件名（下载器可能先落 SSD 中转再搬走）。
    # 三路都不中，最后退到台账自己记的口 —— 终验/搬运两个阶段根本没有下载进程，
    # 前两路必然全空，没有这一路它们就成了「无归属」，按口看时整条消失。
    channels = running_channels()
    by_name = {}
    for out_path, chan in channels.items():
        by_name.setdefault(os.path.basename(out_path), chan)
    seen = set()
    for entry in active:
        target = os.path.normpath(os.path.join(entry.get("path") or "", entry.get("file") or ""))
        entry["channel"] = (channels.get(target) or by_name.get(entry.get("file") or "")
                            or (str(entry.get("port") or "") or None))
        if entry.get("file"):
            seen.add(target)

    # 台账里还在跑、上面却没有任何一行认领的件，补成独立的行 —— 同目录并发两份时，
    # 第二条只存在于台账里，不补就在帧里隐身（见 live_entries 的说明）。
    active.extend(live_entries(ledger, ledger_age, channels, by_name, seen))

    # 在下任务：真在传输的排前面；其次按「最近动过」（三路活性里最新的一路，即实际顺序），
    # 再次按进度、目标名。三路都没动过的排最后 —— 它们是待重排队的残留。
    # **排在补行之后**：补进来的行同样要参与排序，先排就等于把它们全摁在队尾。
    active.sort(key=lambda e: (not e.get("live"), activity_age(e), -(e.get("percent") or -1),
                               e.get("name", "")))

    # 最近完成：按 epoch 倒序。**不能比字符串** —— 一路台账写 +8、另一路写 UTC，
    # 混排时两边的钟不一样，看着像时间倒流。
    recent.sort(key=lambda e: e.get("updated_epoch") or 0, reverse=True)
    recent = recent[:limit]

    # 速度：只在 watch 模式下有上一帧可比
    stamp = time.time()
    if previous is not None and previous_at is not None and stamp > previous_at:
        dt = stamp - previous_at
        for entry in active:
            key = active_key(entry)
            done = entry.get("bytes_done")
            if done is None or key not in previous:
                continue
            delta = done - previous[key]
            if delta >= 0:
                entry["speed_bps"] = int(delta / dt)

    current = {active_key(e): e.get("bytes_done")
               for e in active if e.get("bytes_done") is not None}

    # 通道过滤放在最后：速度计算与续帧（current）用的都是过滤前的全表。全局口径（在下总数 /
    # 正在传输数）另存 active_total / active_live_total —— 免得调用方把「本机 1 个」
    # 误读成「全局只有 1 个」。
    active_total = len(active)
    active_live_total = sum(1 for e in active if e.get("live"))
    if channel:
        active = [e for e in active if e.get("channel") == channel]

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
        "active_total": active_total,              # 过滤前的全局条数（channel 为空时 == len(active)）
        "active_live_total": active_live_total,
        "channel": channel,                        # 本帧的通道过滤口径（None = 不过滤）
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
    if frame.get("channel"):
        # 本机口径 + 全局口径都给：只有「本机 1 个」会被误读成「全局只剩 1 个」。
        print(f"   本机通道 {frame['channel']}：在下 {len(frame['active'])} 个"
              f"（其中正在传输 {frame['active_live']} 个）")
        print(f"   全局在下 {frame.get('active_total', 0)} 个"
              f"（其中正在传输 {frame.get('active_live_total', 0)} 个）：")
    else:
        print(f"   在下 {len(frame['active'])} 个（其中正在传输 {frame['active_live']} 个）：")
    for entry in frame["active"]:
        percent = entry.get("percent")
        bar = f"{percent:5.1f}%" if percent is not None else "  ?  "
        speed = entry.get("speed_bps")
        speed_text = f"  {speed / 1048576:5.2f} MiB/s" if speed else ""
        live = ">" if entry.get("live") else " "
        chan = f"{entry.get('channel') or '-':<5s}"      # 哪条通道在传（linux / <隧道口>）
        print(f"   {live} {chan} {bar}  {size_text(entry)}  {entry['name'][:36]:36s} {entry['status']}{speed_text}")
        if entry.get("file"):
            print(f"              {entry['file']}")
    print()
    print(f"   最近完成 {len(frame['recent'])} 条：")
    for entry in frame["recent"]:
        print(f"     {size_text(entry)}  {entry['name'][:36]:36s} {entry['status']}  {entry['updated']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Linux 侧只读任务视图（给 download-worker 用）")
    parser.add_argument("--status-csv", default="", help="下载台账 CSV；不给则自动探测")
    parser.add_argument("--limit", type=int, default=5, help="「最近完成」显示几条（默认 5）")
    parser.add_argument("--channel", default="", metavar="PORT",
                        help="只看该通道的在下载任务（本机隧道口，如 <隧道口>；不给 = 全部）")
    parser.add_argument("--json", action="store_true", help="输出一行 JSON（默认动作）")
    parser.add_argument("--text", action="store_true", help="输出人看的表格")
    parser.add_argument("--watch", type=int, default=0, metavar="SEC",
                        help="每 SEC 秒输出一行 JSON，直到管道断开")
    parser.add_argument("--cache", default="", metavar="PATH",
                        help="预计算快照（本地快盘上）；新鲜就用它，省掉对网络挂载的扫描")
    parser.add_argument("--no-cache", action="store_true", help="忽略快照，强制现算")
    args = parser.parse_args(argv)

    # 通道口径：容忍 `win:<口>` / 大写 / 空格；空串 = 不过滤（老调用方一字不改照常工作）
    channel = (args.channel or "").strip().lower().replace("win:", "") or None
    # 快照口径：命令行给了用命令行的，否则看配置；--no-cache 一律现算。
    cache_path = "" if args.no_cache else (args.cache or CACHE_PATH)

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
            print(json.dumps(error, ensure_ascii=True), flush=True)     # ASCII 上线：见模块文档
        return 2

    if args.watch > 0:
        previous, previous_at, previous_written = None, None, None
        while True:
            try:
                frame, previous, previous_at, previous_written = serve_frame(
                    csv_path, args.limit, channel, cache_path,
                    previous, previous_at, previous_written)
            except Exception as error:      # 读台账失败也不能把 watch 打死：报一帧错误继续
                frame = {"ok": False, "schema": SCHEMA, "generated": now_text(),
                         "csv": csv_path, "error": repr(error)}
            print(json.dumps(frame, ensure_ascii=True), flush=True)     # ASCII 上线：见模块文档
            try:
                time.sleep(args.watch)
            except KeyboardInterrupt:
                return 0

    frame, _, _, _ = serve_frame(csv_path, args.limit, channel, cache_path, None, None, None)
    if args.text:
        render_text(frame)
    else:
        print(json.dumps(frame, ensure_ascii=True), flush=True)         # ASCII 上线：见模块文档
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
