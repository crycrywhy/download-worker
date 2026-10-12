#!/usr/bin/env python3
"""ssd_ledger.py — SSD 上的下载台账（**动态生成、按口分账、10 天自动清理**）。

为什么这么设计：出口侧上线后，由管家端把「接下来的工作安排」写进中转盘，
出口侧直接读它总结进度；台账随派活创建、任务彻底结束后 10 天自动清除。

为什么要放中转盘（这是整套改造的动机，别当"换个位置放文件"）：
  原来出口侧的 `status` 要 ssh 过来读管家端的清单台账，那条路走 **网络挂载** ——
  实测一次 `dw_tasks.py --json` 要 70 s，另一次跑 400 s 都没收尾
  （挂载点上 os.listdir 病态慢，实测平均 349 ms/次）。而 `st_blocks`/`st_size` 这类
  **单次 stat** 在同一个挂载上却是快的（实测 0.000 s）。
  ⇒ 「进度」必须落成**少量文件 + 单次读**的形态，而不是"每次去遍历目录"。

落盘布局（`{ledger_dir}` 默认 = STAGE_DIR 的真实父目录 = SSD 中转水池根）：

    {ledger_dir}/download.log                  全局：所有口的安排与进度（首行是 meta）
    {ledger_dir}/download_status_{port}.log    单口：`status {port}` 读它
    {ledger_dir}/download/{port}/              该口的 SSD 窗口目录（流式中转用）

文件格式 = **JSONL**（首行 meta + 每任务一行）：
    {"t":"meta",  "rev":…, "ts":…, "ports":{…}, "caps":{…}, "stage_gb":…}
    {"t":"task",  "port":"<port>", "name":…, "state":"running|done|fail", …}
追加式会无限增长且进度要原地更新 ⇒ **每次整体原子重写**（tmp + os.replace），
写入侧由 driver 的 status 线程每 STATUS_INTERVAL 调一次，全程持 flock 单飞。

GC：`state` 已是终态（done/fail）且 `ended` 超过 `ttl_days` 天 ⇒ 丢弃；
某口只剩 meta 行时连文件一起删（"任务彻底结束后自动清除"的落地）。

只读用法：
    python3 ssd_ledger.py                 # 全局人读
    python3 ssd_ledger.py --port <port>   # 单口
    python3 ssd_ledger.py --json          # 机器读（MCP / PC 侧）
退出码恒 0（观测工具，读不到就是空）。
"""
import argparse
import calendar
import fcntl
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dl_config as CFG      # noqa: E402

TTL_DAYS = float(os.environ.get('SSD_LEDGER_TTL_DAYS', '10'))
TERMINAL = ('done', 'fail', 'error')

# 配置**在模块级取好**（约束 α：函数体里不许出现配置层引用）。既有测试靠"改模块级名字"造场景，
# 取值一旦下沉进函数体，那些测试不会报错、只会**静默地不再覆盖**。realpath 仍留到调用时做
# （软链可能后建），只有"读配置"这一步被提到模块级。
CFG_LEDGER_DIR = CFG.get('paths.ssd_ledger_dir', '')
CFG_STAGE_DIR = CFG.get('paths.stage_dir', '')

# 通道代称：linux = 服务器直连流（不经 PC），其余是 worker 口。与 dl_channels.py 同一套口径。
LOCAL_PORT = 'linux'


def ledger_dir():
    """台账目录：配置优先；默认取 STAGE_DIR 的**真实**父目录。

    必须 realpath：STAGE_DIR 常态是**软链**（site.json 里写的是家目录下那个入口链，
    真身在 SSD 挂载点上）。不解析的话，写出来的路径另一侧（ssh 进来的 PC）要多穿一层软链，
    而且用户点名要的就是挂载点下那组直路径。"""
    if CFG_LEDGER_DIR:
        return CFG_LEDGER_DIR
    if CFG_STAGE_DIR:
        return os.path.dirname(os.path.realpath(CFG_STAGE_DIR))
    return ''


def global_path(d=None):
    return os.path.join(d or ledger_dir(), 'download.log')


def port_path(port, d=None):
    return os.path.join(d or ledger_dir(), 'download_status_%s.log' % port)


def window_dir(port, stage_dir=None):
    """该口的窗口目录 = `<STAGE_DIR>/<port>`。**下载一律走它**（中转盘上的缓冲）。

    直接挂在 STAGE_DIR 下（而不是再套一层），是为了与老式整份缓存件的布局
    `<STAGE_DIR>/<文件名>/<文件名>` 天然隔开：端口名全是数字或 `linux`，
    不会被误认成某个下载文件名，`_stream_eligible` 那套"目录里有没有 .aria2/.ready.json"
    的老件判据也就不会撞上新的口目录。
    """
    base = stage_dir or CFG_STAGE_DIR or os.path.join(ledger_dir(), 'download')
    # realpath：STAGE_DIR 常态是软链，解析后各口目录与台账里的绝对路径自洽，
    # 用户按台账里那组直路径就能 cd 进去。
    return os.path.join(os.path.realpath(base), str(port))


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------
def _read_file(path):
    meta, rows = None, []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue          # 半截行（写到一半被读到）跳过，不当错误
                if o.get('t') == 'meta':
                    meta = o
                elif o.get('t') == 'task':
                    rows.append(o)
    except OSError:
        return None, []
    return meta, rows


def read(port=None, d=None):
    """读台账。port=None 读全局；返回 {'meta':…, 'rows':[…]}（读不到就是空壳）。"""
    path = port_path(port, d) if port else global_path(d)
    meta, rows = _read_file(path)
    return {'meta': meta, 'rows': rows, 'path': path}


def read_all(d=None, ttl_days=TTL_DAYS, now=None):
    """所有口的行合并（全局视图）。全局文件为主，缺失的口再各自补读。

    `ttl_days=None` = **不做 TTL 过滤**（写入侧合并历史行时必须用这个：
    读回来就先滤掉终态行的话，那些行根本进不了合并循环，`gc` 计数永远是 0 ——
    第一版就是这么错的，而且症状很隐蔽：行确实消失了，只是"不是 GC 干的"。）
    """
    now = time.time() if now is None else now
    meta, rows = _read_file(global_path(d))
    if not rows:
        # 全局文件被 GC 掉或还没生成 ⇒ 退化成逐口拼（口径必须一致，否则两个视图会打架）
        seen = set()
        base = d or ledger_dir()
        rows = []
        try:
            names = sorted(os.listdir(base))
        except OSError:
            names = []
        for n in names:
            if n.startswith('download_status_') and n.endswith('.log'):
                _m, rs = _read_file(os.path.join(base, n))
                for r in rs:
                    key = (r.get('port'), r.get('name'))
                    if key not in seen:
                        seen.add(key)
                        rows.append(r)
    if ttl_days is None:
        return rows
    return [r for r in rows if not _expired(r, now, ttl_days)]


# --------------------------------------------------------------------------
# 写 / GC
# --------------------------------------------------------------------------
def _expired(row, now, ttl_days):
    """终态且结束超过 TTL 天 ⇒ 过期（该被 GC 掉）。

    判据优先用 `ended_ts`（epoch 秒，无时区歧义）。只有它缺失时才回落到解析
    `ended` 那个**人读字符串** —— 那是 UTC+8 口径，而本机系统时区是 UTC，
    拿 `mktime` 直接解会得到"比真实早 8 小时"的 epoch（第一版就栽在这，
    8 小时的偏移当成"没差多少"很容易被放过）。
    """
    if row.get('state') not in TERMINAL:
        return False
    ts = row.get('ended_ts')
    if ts is None:
        ts = _parse_ts(row.get('ended'))
    if ts is None:
        return False                  # 没有结束时间就不敢删（宁可多留）
    return (now - float(ts)) > ttl_days * 86400


def _parse_ts(text):
    """解析 `YYYY-MM-DD HH:MM:SS`（**UTC+8 口径**，台账自己写的）。"""
    if not text:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            # `calendar.timegm` 而不是 `time.mktime`：后者按**宿主机时区**解释元组，
            # 换个时区的机器算出来就不同。台账是 UTC+8 口径的字符串，先当 UTC 解再减 8 h。
            return calendar.timegm(time.strptime(str(text)[:19], fmt)) - 8 * 3600
        except ValueError:
            continue
    return None


def _atomic_write(path, lines):
    tmp = path + '.tmp.%d' % os.getpid()
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write(rows, meta=None, d=None, ttl_days=TTL_DAYS, now=None, alive=None, drv=''):
    """把 rows 落到全局 + 各口文件，顺路做 GC。

    rows 的合并口径：以 `(port, name)` 为键，**本次传入的行覆盖旧行**；
    旧行里本次没提到的照旧保留（在途任务由调用方每轮都传，完成的行就会从"本次传入"
    里消失，靠这一次合并留在盘上 —— 所以这里必须读旧文件再合并，不能直接覆盖写）。

    `alive` + `drv`：**收拾幽灵行**。调用方每轮把它**此刻还活着**的行键集合传进来，
    本函数把「`drv` 是这个调用方、状态还是 running、却不在 alive 里」的行判成 error。
    没有这一层，driver 一重启（或线被退休）就会在台账里留下一行永远 45% 的
    "正在下载"——10 天都没人清（`_expired` 只认终态行）。**只管自己 driver 的行**：
    多 driver 共存时谁也不许动别人的行（那正是"别的 driver 还在跑"的正常态）。

    返回 {'written': {port: n}, 'gc': n, 'files': [...]}。
    """
    now = time.time() if now is None else now
    base = d or ledger_dir()
    if not base:
        return {'written': {}, 'gc': 0, 'files': [], 'error': 'ledger_dir 为空（stage_dir 未配置？）'}
    os.makedirs(base, exist_ok=True)

    lock_path = os.path.join(base, '.download_ledger.lock')
    got = False
    try:
        with open(lock_path, 'w') as lk:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
            except OSError:
                # 抢不到 = 另一个写入者正在写。**直接返回，不阻塞** ——
                # 这是 driver 的 status 线程在调，卡住它就等于卡住整个状态循环。
                return {'written': {}, 'gc': 0, 'files': [], 'error': 'busy'}
            # 旧行（含各口历史）读进来做合并
            old = {}
            for r in read_all(base, ttl_days=None, now=now):
                old[(r.get('port'), r.get('name'))] = r
            merged = dict(old)
            # ---- 幽灵行清理（见 docstring）：只动本 driver 名下的 ----
            if alive is not None and drv:
                for k, r in list(merged.items()):
                    if (r.get('state') == 'running' and r.get('drv') == drv and k not in alive):
                        r = dict(r)
                        r['state'] = 'error'
                        r['note'] = r.get('note') or '线已消失（driver 重启或停线）'
                        merged[k] = r
            for r in rows:
                r = dict(r)
                r.setdefault('t', 'task')
                key = (r.get('port'), r.get('name'))
                prev = merged.get(key) or {}
                r['first_seen'] = prev.get('first_seen') or r.get('first_seen') or _now_text(now)
                if r.get('state') in TERMINAL:
                    r['ended'] = r.get('ended') or _now_text(now)
                    r['ended_ts'] = r.get('ended_ts') or now
                else:
                    r.pop('ended', None)
                    r.pop('ended_ts', None)
                # 终态行补齐进度：下载器落位时会删掉 sidecar（成功）或在失败前就没写全，
                # 所以"完成/失败"这两态从行里读不到 done；由 size 补 100% 才不会显示
                # 一份已经下完的文件停在 87%。
                if r.get('state') == 'done':
                    try:
                        _sz = int(r.get('size') or 0)
                        if _sz > 0:
                            r['done'] = _sz
                    except (TypeError, ValueError):
                        pass
                try:
                    _sz = int(r.get('size') or 0)
                    if _sz > 0:
                        r['pct'] = round(int(r.get('done') or 0) * 100.0 / _sz, 1)
                except (TypeError, ValueError):
                    pass
                merged[key] = r

            live, gc = [], 0
            for r in merged.values():
                if _expired(r, now, ttl_days):
                    gc += 1
                    continue
                live.append(r)

            by_port = {}
            for r in live:
                by_port.setdefault(str(r.get('port') or LOCAL_PORT), []).append(r)

            files, written = [], {}
            for port, rs in sorted(by_port.items()):
                rs.sort(key=lambda r: (_state_rank(r), r.get('started') or '', r.get('name') or ''))
                lines = [json.dumps(_meta_for(port, rs, now), ensure_ascii=False)]
                lines += [json.dumps(r, ensure_ascii=False) for r in rs]
                p = port_path(port, base)
                _atomic_write(p, lines)
                files.append(p)
                written[port] = len(rs)

            # 全局 = 所有口拼一起（首行 meta 带全部口的汇总）
            allrows = [r for rs in by_port.values() for r in rs]
            allrows.sort(key=lambda r: (r.get('port') or '', _state_rank(r),
                                        r.get('started') or '', r.get('name') or ''))
            glines = [json.dumps(_meta_global(by_port, now, meta), ensure_ascii=False)]
            glines += [json.dumps(r, ensure_ascii=False) for r in allrows]
            gpath = global_path(base)
            _atomic_write(gpath, glines)
            files.append(gpath)

            # 只剩 meta 的**口**文件删掉（"彻底结束后自动清除"）；全局文件始终保留
            for p in list(files):
                if p == gpath or not os.path.basename(p).startswith('download_status_'):
                    continue
                m, rs = _read_file(p)
                if not rs:
                    try:
                        os.unlink(p)
                        files.remove(p)
                    except OSError:
                        pass
            return {'written': written, 'gc': gc, 'files': files}
    finally:
        pass              # with 块退出即释放 flock（显式写出来是为了标注这个语义）


def _state_rank(r):
    return {'running': 0, 'queued': 1, 'fail': 2, 'error': 2, 'done': 3}.get(r.get('state'), 4)


def _now_text(now=None):
    """台账时间一律 **UTC+8**：这是给用户/PC 看的表，不能跟服务器的 UTC 混。

    为什么不用 `time.localtime`：本机系统时区就是 UTC（见记忆 server-identity），
    直接用会写出比用户认知早 8 小时的时间戳 —— 之前 driver 快照就是这么写的。
    """
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime((time.time() if now is None else now) + 8 * 3600))


def _meta_global(by_port, now, extra=None):
    caps = extra if isinstance(extra, dict) else {}
    m = {'t': 'meta', 'scope': 'all', 'ts': _now_text(now),
         'ports': {p: _port_summary(rs) for p, rs in sorted(by_port.items())}}
    m.update(caps)
    return m


def _meta_for(port, rs, now):
    return {'t': 'meta', 'scope': port, 'ts': _now_text(now), 'ports': {port: _port_summary(rs)}}


def _port_summary(rs):
    """该口汇总。**计数与字节数各用各的键** —— 第一版把两个都叫 `done`，
    结果"完成 700"（700 是字节数）看上去像完成了 700 个任务。"""
    running = [r for r in rs if r.get('state') == 'running']
    s = {'tasks': len(rs), 'running': len(running),
         'n_done': sum(1 for r in rs if r.get('state') == 'done'),
         'n_fail': sum(1 for r in rs if r.get('state') in ('fail', 'error'))}
    tot = sum(int(r.get('size') or 0) for r in running)
    done = sum(int(r.get('done') or 0) for r in running)
    if tot:
        s['pct'] = round(done * 100.0 / tot, 1)
        s['run_size'] = tot
        s['run_done'] = done
    dur = sum(float(r.get('win_used') or 0) for r in rs)
    if dur:
        s['win_used_gb'] = round(dur / 1e9, 2)
    return s


def _human(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return '--'
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(n) < 1024 or unit == 'TiB':
            return ('%.2f %s' % (n, unit)) if unit != 'B' else ('%d B' % n)
        n /= 1024


def render(port=None, d=None):
    if port:
        data = read(port, d)
    else:
        _m, _r = _read_file(global_path(d))
        data = {'meta': _m, 'rows': read_all(d), 'path': global_path(d)}
    rows = data['rows']
    meta = data.get('meta') or {}
    ports = (meta.get('ports') or {})
    out = []
    scope = meta.get('scope') or (port or 'all')
    out.append('== SSD 下载台账（%s）%s' % (scope, meta.get('ts') or '--'))
    for p, s in sorted(ports.items()):
        line = '  %-6s 在跑 %d / 完成 %d / 失败 %d' % (p, s.get('running', 0),
                                                    s.get('n_done', 0), s.get('n_fail', 0))
        if s.get('run_size'):
            line += '   在跑合计 %s / %s (%.1f%%)' % (_human(s.get('run_done')),
                                                    _human(s.get('run_size')), s.get('pct') or 0.0)
        if s.get('win_used_gb'):
            line += '   SSD 窗口 %.2f GB' % s['win_used_gb']
        out.append(line)
    if not ports:
        out.append('  （无）')
    if meta.get('stage_high_gb'):
        out.append('  SSD 水位上限 %.0f GB（%s）；当前占用 %s' % (
            float(meta['stage_high_gb']),
            ('随口数自动：口 %s' % meta.get('np_ports')) if meta.get('gb_auto') else '显式指定',
            _human((meta.get('stage_gb') or 0) * 1e9) if meta.get('stage_gb') is not None else '--'))
    q = meta.get('queue') or []
    if q:
        out.append('')
        out.append('  接下来的工作安排（队列队头 %d 条）：' % len(q))
        for i, e in enumerate(q, 1):
            out.append('   %2d. %-40s %-5s %-26s %s' % (
                i, (e.get('name') or '')[:40], e.get('platform') or '', (e.get('item') or '')[:26],
                e.get('requester') or ''))
    out.append('')
    if not rows:
        out.append('  没有任务行。')
    for r in rows:
        if port and str(r.get('port')) != str(port):
            continue
        pct = r.get('pct')
        pct_s = ('%5.1f%%' % pct) if isinstance(pct, (int, float)) else '  --  '
        out.append('  %-6s %-9s %-38s %s  %s/%s  %s' % (
            r.get('port') or '--', r.get('state') or '--', (r.get('name') or '')[:38], pct_s,
            _human(r.get('done')), _human(r.get('size')), r.get('requester') or ''))
        if r.get('win_used'):
            out.append('         SSD 窗口在途 %s' % _human(r['win_used']))
        if r.get('note'):
            out.append('         %s' % r['note'])
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser(description='SSD 下载台账（只读视图）')
    ap.add_argument('--port', default='', help='只看某个口（端口号或 linux）；不给=全局')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--dir', default='', help='台账目录（默认取配置）')
    a = ap.parse_args()
    d = a.dir or None
    if a.port:
        data = read(a.port, d)
        rows = [r for r in data['rows'] if not _expired(r, time.time(), TTL_DAYS)]
        data['rows'] = rows
    else:
        # 全局视图**必须带回 meta**：逐口汇总、SSD 水位、"接下来的工作安排"（队列）都只写在
        # 首行 meta 里，不带就是三样全丢。PC 侧 `status all` 与 MCP 都是读这个 `--json`，
        # 丢了 meta 它们只能显示一堆没有归属和安排的任务行。
        _m, _rows = _read_file(global_path(d))
        data = {'meta': _m, 'rows': read_all(d, ttl_days=TTL_DAYS, now=time.time()),
                'path': global_path(d)}
    if a.json:
        # Single line, not indented: this output crosses the wire inside a
        # marker-delimited line protocol, where each segment must be one line so
        # the reader never has to brace-match. A pretty-printed blob makes the
        # older readers pick up a lone "{" and drop the whole segment with only a
        # warning. Use the renderer (no --json) for human reading.
        # ensure_ascii=True：这段输出要走**标记分隔的单行协议**到对端。中文若按原字符发出去，
        # 对端中文 Windows 会按 CP936 解码，尾随的 UTF-8 前导字节会把后面的引号一起吞掉，
        # JSON 直接从结构上坏掉。转义成 \uXXXX 就只剩 ASCII，怎么解都不会错。
        print(json.dumps(data, ensure_ascii=True))
    else:
        print(render(a.port or None, d))
    return 0


if __name__ == '__main__':
    sys.exit(main())
