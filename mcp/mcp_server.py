#!/usr/bin/env python3
"""dl_mcp.py —— 统一下载系统的 **MCP 薄前端**（顶替原 downloader MCP）。

定位（不变）：MCP 是 **Adapter，不是第二个 Downloader**。
  本文件只做三件事：参数校验 → 调既有脚本/读既有文件 → 结构化返回。
  不实现 Range/分块/重试/续传/校验/zero scan/worker 流式 —— 那些全属于 linux_downloader 与 Worker。

与"每个 agent 各自直连下载"的老做法相比
  1. **所有下载申请走统一队列**（`dl_req.py` → `inbox/` → driver 排空），MCP 自己**不再直接
     起下载进程**；口不够时按优先级排队，而不是各自抢
  2. 新增 `submit` / `queue_status` / `ctl` / `lease_status` 四个工具：提交、看队列、运行时
     控制（线数/暂停/插队/让路）、看跨 agent 的口租约与争用
  3. `download`（临时单文件）保留，但**变成租约感知**：先挑一个没被别人占的口、写 advisory
     声明再用；拿不到也不拦（**只声明、不拒绝**，与 advisory 的既有语义一致）
  4. 仍然**无状态**：状态全在文件/flock 里。多 agent 各起一份 MCP 进程也不会互相打架
     —— 这正是"统一入口"能成立的前提（旧设计的 download 是直连，多进程必然抢口）

传输：stdio。**stdout 只允许 JSON-RPC 消息**，所有日志走 stderr（stdio MCP 的硬规矩）。
仅依赖 stdlib + 系统 python3（与旧 server 同口径：不引入第三方依赖、不需要 venv）。
"""
import csv
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))       # 本文件（MCP 本体）自己的目录


def _default_worker_dir():
    """工具目录的默认位置：优先**本仓自带的 `tools/`**。

    本 MCP 与工具脚本同仓分发，所以"本体旁边就是工具目录"是最常见的情形 ——
    仓库 clone 到哪都行，不必配任何环境变量（安装路径不该要求用户记住一个约定）。

    `dl_req.py` 的存在与否用来判定"这确实是工具目录"：只看目录名 `tools/`
    会误认到无关的同名目录上。

    只有把本体单独搬走时（例如与其它 MCP 统一存放）才轮到第二个默认值。
    """
    sibling = os.path.join(os.path.dirname(HERE), 'tools')
    if os.path.isfile(os.path.join(sibling, 'dl_req.py')):
        return sibling
    return os.path.expanduser('~/download-worker/tools')


# **工具目录**：本 MCP 是"薄前端"，真正干活的脚本（dl_req/dl_ctl/worker_pool/linux_downloader/
# 状态文件…）都在工具目录里，本体自己只负责参数校验与读文件。凡是要调工具的地方一律按
# WORKER_DIR 解析 ⇒ 换机器/换目录只给 DL_WORKER_DIR 即可，不必改代码。
WORKER_DIR = os.path.abspath(os.environ.get('DL_WORKER_DIR') or _default_worker_dir())
sys.path.insert(0, WORKER_DIR)

# 平台取值**跟着 dl_req/dl_control 走**，不在这里另立一份（否则两边漂移时 MCP 会放行一个
# argparse 认不出的值，报错要到子进程里才看得见）。导入失败也只是退化成硬编码兜底。
try:
    import dl_control as _C
    _PLATFORMS = tuple(_C.PLATFORMS)
    _LEVELS = tuple(_C.level_codes())
except Exception:                       # pragma: no cover
    _PLATFORMS = ('alpha', 'beta', 'gamma')
    _LEVELS = ('p0', 'p1', 'p2', 'p3', 'p4')

if not os.path.isfile(os.path.join(WORKER_DIR, 'dl_req.py')):
    # 走 stderr：stdio MCP 的 stdout 只许放 JSON-RPC。这里只警告不退出 ——
    # 只读工具（worker_status 等）在工具目录缺席时仍有意义。
    sys.stderr.write(
        "[download-worker] tool directory looks unset: %s\n"
        "[download-worker] set DL_WORKER_DIR to the directory holding dl_req.py "
        "(this repo's tools/) and restart.\n" % WORKER_DIR)

SERVER_NAME = 'download-worker'
SERVER_VERSION = '2.0.0'
PROTOCOL_FALLBACK = '2025-06-18'

DL_REQ = os.path.join(WORKER_DIR, 'dl_req.py')
DL_CTL = os.path.join(WORKER_DIR, 'dl_ctl.py')
WORKER_POOL = os.path.join(WORKER_DIR, 'worker_pool.py')
DOWNLOADER = os.path.join(WORKER_DIR, 'linux_downloader.py')
DL_CHANNELS = os.path.join(WORKER_DIR, 'dl_channels.py')   # 按通道（linux/<隧道口>）看每条线进度
DL_INVENTORY = os.path.join(WORKER_DIR, 'inventory_refresh.py')  # data_status.csv 自管刷新
# driver 名（决定读哪份 state/driver_<name>.json / 控制文件）；本机是什么名字用 DRIVER_NAME 覆盖
DRIVER = os.environ.get('DRIVER_NAME') or 'dl_driver'
STATE_DIR = os.path.join(WORKER_DIR, 'state')

# 台账 / 告警的默认位置**不写死在本文件里**（本仓库是公开的，规矩同 linux/dw_tasks.py）：
# 依次取 环境变量 → 配置文件 → 空。留空时只有 `alerts` / `overview` 两个工具会提示怎么配，
# 其余工具照常。
CONFIG_FILE = os.environ.get('DW_TASKS_CONFIG') or os.path.expanduser(
    '~/.config/download-worker/dw_tasks.json')


def _from_config(key):
    """从 dw_tasks.json 取一个路径；列表取第一条（`status_csv_candidates` 就是列表）。"""
    try:
        with open(CONFIG_FILE, encoding='utf-8') as fh:
            cfg = json.load(fh)
        value = cfg.get(key) if isinstance(cfg, dict) else None
    except Exception:
        return ''
    if isinstance(value, list):
        value = value[0] if value else ''
    return os.path.expanduser(value) if isinstance(value, str) else ''


ALERTS_FILE = os.environ.get('DL_ALERTS_FILE') or _from_config('alerts_file')
# 台账 = 主对照表（status_collector.py 生成），不是 state/ 下的同名物
STATUS_CSV = os.environ.get('DL_STATUS_CSV') or _from_config('status_csv_candidates')
PY = os.environ.get('DL_PYTHON') or sys.executable
DEBUG = os.environ.get('DL_MCP_DEBUG') == '1'
SCRIPT_TIMEOUT = float(os.environ.get('DL_MCP_TIMEOUT', '60'))


# ---------------------------------------------------------------- 基础设施

def log(msg):
    if DEBUG:
        sys.stderr.write('[dl_mcp] %s\n' % msg)
        sys.stderr.flush()


def log_err(msg):
    sys.stderr.write('[dl_mcp] %s\n' % msg)
    sys.stderr.flush()


def write_message(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def reply(rid, result):
    write_message({'jsonrpc': '2.0', 'id': rid, 'result': result})


def reply_error(rid, code, message):
    write_message({'jsonrpc': '2.0', 'id': rid, 'error': {'code': code, 'message': message}})


def tool_ok(payload):
    """MCP 工具返回：结构化内容（文本块 + structuredContent，客户端两种都认）。"""
    return {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False, indent=1)}],
            'structuredContent': payload,
            'isError': False}


def tool_err(message):
    return {'content': [{'type': 'text', 'text': message}], 'isError': True}


def run(cmd, timeout=None, cwd=None):
    """跑既有脚本并回收 stdout/stderr/退出码。**永不抛**（任何异常都变成可读的返回）。"""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout or SCRIPT_TIMEOUT, cwd=cwd or WORKER_DIR)
        return (p.returncode, p.stdout.decode('utf-8', 'replace'),
                p.stderr.decode('utf-8', 'replace'))
    except subprocess.TimeoutExpired:
        return (124, '', '命令超时（%ss）：%s' % (timeout or SCRIPT_TIMEOUT, ' '.join(cmd[:3])))
    except Exception as e:
        return (125, '', '%s: %s' % (type(e).__name__, e))


def read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def snapshot(driver=None):
    """driver 状态快照（读不到返回 {}）：`state/driver_<name>.json`。"""
    return read_json(os.path.join(STATE_DIR, 'driver_%s.json' % (driver or DRIVER)), {}) or {}


# ---------------------------------------------------------------- 工具：submit

def _submit_batch(a, items):
    """批量提交（MCP submit 的 `items` 路径）：**逐条走 dl_req.py 的同一条路径**。

    顶层同名参数与 `defaults` 一起作为默认值（所以 300 条里每条只需写差异字段），
    单条失败不拖累整批 —— 失败明细逐条返回，调用方修好再重投那几条即可。
    """
    defaults = {k: v for k, v in a.items()
                if k not in ('items', 'defaults') and v not in (None, '')}
    if isinstance(a.get('defaults'), dict):
        defaults.update({k: v for k, v in a['defaults'].items() if v not in (None, '')})
    spec = {'defaults': defaults, 'items': items}
    tmp = os.path.join('/tmp', 'dl_mcp_batch_%d_%s.json' % (os.getpid(), time.strftime('%H%M%S')))
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(spec, f, ensure_ascii=False)
    except OSError as e:
        return tool_err('批量清单临时文件写不了：%s: %s' % (type(e).__name__, e))
    try:
        rc, out, err = run([PY, DL_REQ, 'submit', '--batch-file', tmp, '--json'], timeout=600)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    summary = None
    for line in reversed((out or '').strip().splitlines()):
        if line.startswith('{'):
            try:
                summary = json.loads(line)
                break
            except ValueError:
                pass
    if summary is None:
        return tool_err('批量提交没拿到汇总（退出码 %s）：%s' % (rc, (out or err or '')[-400:]))
    payload = {'queued': rc == 0, 'exit_code': rc, 'batch': True,
               'total': summary.get('total'), 'submitted': summary.get('submitted'),
               'failed': summary.get('failed'), 'ids': summary.get('ids'),
               'ids_truncated': summary.get('ids_truncated'), 'errors': summary.get('errors'),
               'elapsed_s': summary.get('elapsed_s'),
               'note': ('已进收件箱的会按**等级**派发（同等级内后提交的先跑）；'
                        '失败的**没有**写进收件箱，改好字段重投那几条即可')}
    if rc == 0:
        return tool_ok(payload)
    payload['hint'] = '有 %s 条没提交成功，看 errors 里的逐条原因' % payload.get('failed')
    return {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False, indent=1)}],
            'structuredContent': payload, 'isError': True}


def tool_submit(a):
    """把下载申请写进统一收件箱（**唯一的提交入口**）。给了 `items` 即**批量**。"""
    _items = a.get('items')
    if isinstance(_items, list) and _items:
        return _submit_batch(a, _items)
    name = str(a.get('name') or '').strip()
    platform = str(a.get('platform') or '').strip().lower()
    outdir = str(a.get('outdir') or '').strip()
    if not name or not platform or not outdir:
        return tool_err('submit 需要 name / platform / outdir 三个必填参数')
    if not outdir.startswith('/'):
        return tool_err('outdir 必须是绝对路径（收件箱里的申请会被 driver 直接执行）')
    cmd = [PY, DL_REQ, 'submit', '--item', name, '--platform', platform,
           '--outdir', outdir, '--owner', str(a.get('owner') or 'agent')]
    for key, flag in (('source', '--source'), ('prefix', '--prefix'), ('build', '--build'),
                      ('object_key', '--object-key'), ('url', '--url'),
                      ('note', '--note'), ('driver', '--driver'), ('id', '--id')):
        v = a.get(key)
        if v not in (None, ''):
            cmd += [flag, str(v)]
    if a.get('level') not in (None, ''):
        cmd += ['--level', str(a['level'])]          # 等级优先（与 dl_req.resolve_priority 同口径）
    elif a.get('priority') is not None:
        cmd += ['--priority', str(int(a['priority']))]
    if a.get('est_gb') is not None:
        cmd += ['--est-gb', str(float(a['est_gb']))]
    if a.get('prefer_win'):
        cmd += ['--prefer-win']
    rc, out, err = run(cmd)
    rid = None
    m = re.search(r'^已提交:\s*(\S+)', out, re.M)
    if m:
        rid = m.group(1)
    payload = {'queued': rc == 0, 'exit_code': rc, 'id': rid, 'request': a,
               'stdout': out.strip(), 'stderr': err.strip()}
    if rc != 0:
        payload['hint'] = ('申请不合法或写盘失败（退出码 4=不合法 / 2=参数错）'
                           '；driver 没在跑也不影响提交 —— 申请会留在 inbox/ 等它启动')
        return {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False, indent=1)}],
                'structuredContent': payload, 'isError': True}
    payload['note'] = ('已进收件箱，driver 每 ~5s 排空并按**等级**派发（同等级内后提交的先跑）；'
                       '被拒会移进 inbox/rejected/ 并带原因（用 dl_req.py list 看）')
    return tool_ok(payload)


# ---------------------------------------------------------------- 工具：queue_status

def tool_queue_status(a):
    """队列 + 线数 + 口 + **按通道的下载进度** 的一屏实况（只读）。"""
    rc, out, err = run([PY, DL_REQ, 'list', '--recent', str(int(a.get('recent') or 5))])
    snap = snapshot(a.get('driver'))
    ctl = snap.get('ctl') or {}
    payload = {
        'driver': snap.get('name') or DRIVER,
        'driver_alive': bool(snap.get('pid')),
        'queue_depth': snap.get('queue_depth'),
        'completed': snap.get('completed'),
        'total': snap.get('total'),
        'lines': snap.get('lines') or [],
        'workers': snap.get('workers') or {},
        'ctl': {k: ctl.get(k) for k in ('rev_applied', 'win_lines_desired', 'win_lines_live',
                                        'paused', 'skip', 'avoid_ports', 'policy_avoid_ports',
                                        'avoid_effective', 'broker_max_lines',
                                        'broker_max_lines_live', 'broker_fresh')},
        'inbox_text': out.strip(),
    }
    if rc != 0:
        payload['inbox_error'] = err.strip()
    # 按通道（linux / <隧道口>）的逐线进度：扫 /proc + 各自 sidecar，**跨 agent**（其它 agent 起的线也在内）
    rc_c, out_c, err_c = run([PY, DL_CHANNELS, '--json'], timeout=30)
    if rc_c == 0:
        try:
            ch = json.loads(out_c)
            payload['channels'] = ch.get('channels') or {}
            payload['channels_text'] = ch.get('text') or ''
        except Exception as e:
            payload['channels_error'] = '%s: %s' % (type(e).__name__, e)
    else:
        payload['channels_error'] = (err_c or out_c).strip()
    return tool_ok(payload)


# ---------------------------------------------------------------- 工具：ctl

_CTL_SUBS = ('lines', 'pause', 'resume', 'promote', 'drop', 'skip', 'unskip', 'avoid', 'unavoid')


def tool_ctl(a):
    """运行时控制（等价于在人手敲 `dl_ctl.py`）：线数 / 暂停 / 插队 / 跳过 / 让路。"""
    action = str(a.get('action') or '').strip()
    if action not in _CTL_SUBS:
        return tool_err('ctl.action 必须是 %s 之一' % '/'.join(_CTL_SUBS))
    cmd = [PY, DL_CTL, '--driver', str(a.get('driver') or DRIVER)]
    if action == 'lines':
        if a.get('n') is None:
            return tool_err('ctl lines 需要 n')
        cmd += ['lines', str(int(a['n']))]
        if a.get('grace'):
            cmd += ['--grace']
    elif action in ('promote', 'drop', 'skip', 'unskip', 'avoid', 'unavoid'):
        m = a.get('match') or a.get('ports') or []
        if isinstance(m, (str, int)):
            m = [m]
        if not m and not (action == 'unskip' and a.get('all')):
            return tool_err('ctl %s 需要 match/ports 列表' % action)
        cmd += [action] + [str(x) for x in m]
        if action == 'unskip' and a.get('all'):
            cmd += ['--all']
    else:
        cmd += [action]
    rc, out, err = run(cmd, timeout=float(a.get('wait') or 30) + 15)
    payload = {'action': action, 'exit_code': rc, 'stdout': out.strip(), 'stderr': err.strip(),
               'applied': rc == 0}
    if rc == 3:
        payload['hint'] = '司机读过 rev 但还没确认生效（可能正忙）；稍后 ctl 里再看 rev_applied'
    elif rc == 4:
        payload['hint'] = '没生效：控制文件校验失败 / 没拿到锁 / driver 明确报错（看 stderr）'
    return tool_ok(payload) if rc == 0 else {
        'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False, indent=1)}],
        'structuredContent': payload, 'isError': True}


# ---------------------------------------------------------------- 工具：lease_status

def tool_lease_status(a):
    """跨 agent 的口租约与争用观测（只读）：谁在占哪个口、我可分多少、有没有争用。"""
    cmd = [PY, DL_CTL, 'lease', 'show', '--json']
    if a.get('traffic'):
        cmd.append('--traffic')
    rc, out, err = run(cmd)
    data = None
    if out.strip():
        try:
            data = json.loads(out[out.index('{'):])
        except Exception:
            data = None
    if data is None:
        return tool_err('读租约失败（rc=%d）：%s%s' % (rc, err.strip(), out.strip()[:200]))
    return tool_ok(data)


# ---------------------------------------------------------------- 工具：worker_status

def tool_worker_status(a):
    """worker 口存活/延迟/流量（池感知）。只读既有探活层，不自己 probe。"""
    cmd = [PY, WORKER_POOL, '--json']
    if a.get('traffic') is False:
        cmd.append('--no-traffic')
    rc, out, err = run(cmd)
    if rc != 0:
        return tool_err('worker_pool 退出码 %d：%s' % (rc, err.strip()[:300]))
    try:
        data = json.loads(out)
    except Exception as e:
        return tool_err('解析 worker_pool 输出失败：%s' % e)
    if a.get('port') is not None:
        d = (data.get('ports') or {}).get(str(int(a['port'])))
        return tool_ok({'port': int(a['port']), 'detail': d}) if d else \
            tool_err('端口 %s 不在池里（注册表与扫描范围见 worker_pool.py）' % a['port'])
    return tool_ok(data)


# ---------------------------------------------------------------- 工具：alerts

def tool_alerts(a):
    """读告警流水（JSONL，倒序取最近 limit 条；可按 event/since 过滤）。"""
    limit = max(1, min(500, int(a.get('limit') or 20)))
    ev = a.get('event')
    since = a.get('since')
    if not ALERTS_FILE:
        return tool_err('没配告警流水的位置：给 DL_ALERTS_FILE，或在 %s 里写 alerts_file' % CONFIG_FILE)
    rows = []
    try:
        with open(ALERTS_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if ev and rec.get('event') != ev:
                    continue
                if since and str(rec.get('ts_utc8') or '') < str(since):
                    continue
                rows.append(rec)
    except FileNotFoundError:
        return tool_ok({'file': ALERTS_FILE, 'total': 0, 'events': [],
                        'note': '告警流水还没有（哨兵/看门狗还没报过事）'})
    except Exception as e:
        return tool_err('读告警流水失败：%s: %s' % (type(e).__name__, e))
    counts = {}
    for r in rows:
        counts[r.get('event') or '?'] = counts.get(r.get('event') or '?', 0) + 1
    return tool_ok({'file': ALERTS_FILE, 'total': len(rows), 'counts': counts,
                    'events': rows[-limit:][::-1]})


# ---------------------------------------------------------------- 工具：inventory

def tool_inventory(a):
    """data_status.csv（数据清单）的**自管**刷新（清单也交给脚本自己管）。

    driver 每 ~10 min 会自己打一次 `inventory_refresh.py --auto`（按状态决定起扫/收口/不动），
    这个工具是给会话的手动口。四种动作：
      status（默认）只读看状态；auto 按状态做该做的事；start 强制起一轮扫描；finish 强制收口。
    除 status 外一律**丢后台**（收口要跑几分钟，含 Notion 同步）——立即返回，稍后用 status 看结果。
    """
    action = (a.get('action') or 'status').strip().lower()
    if action not in ('status', 'auto', 'start', 'finish'):
        return tool_err('action 只能是 status / auto / start / finish')
    if not os.path.exists(DL_INVENTORY):
        return tool_err('本仓库不含清单刷新器 `inventory_refresh.py`（它属于"按对象记账"的'
                        '那条链，与具体数据集强耦合，未随包发布）。要用本工具，'
                        '把刷新器放到 %s 后重试 —— 其余工具不受影响。' % WORKER_DIR)
    if action == 'status':
        rc, out, err = run([PY, DL_INVENTORY, '--status', '--json'], timeout=60)
        if rc != 0:
            return tool_err('读清单状态失败：%s' % (err or out).strip())
        try:
            return tool_ok(json.loads(out))
        except Exception as e:
            return tool_err('解析失败：%s: %s / %s' % (type(e).__name__, e, out[:200]))
    rc, out, err = run([PY, DL_INVENTORY, '--' + action, '--detach'], timeout=60)
    if rc != 0:
        return tool_err('后台起 %s 失败：%s' % (action, (err or out).strip()))
    try:
        payload = json.loads(out)
    except Exception:
        payload = {'raw': out.strip()}
    payload['hint'] = ('已丢后台（收口含 Notion 同步，要几分钟）→ 稍后用 action=status 看结果，'
                       '或看 driver 自己的运行日志（路径见 drivers.*.log 配置）')
    return tool_ok(payload)


# ---------------------------------------------------------------- 工具：overview

def tool_overview(a):
    """下载台账 CSV 的汇总（状态计数 + 可选筛选）。只读，绝不写台账。"""
    path = a.get('csv') or STATUS_CSV
    if not path:
        return tool_err('没配台账位置：给 csv 参数或 DL_STATUS_CSV，'
                        '或在 %s 里写 status_csv_candidates' % CONFIG_FILE)
    if a.get('refresh'):
        _col = os.path.join(WORKER_DIR, 'status_collector.py')
        if not os.path.exists(_col):
            return tool_err('本仓库不含台账刷新器 `status_collector.py`（它属于"按对象记账"的'
                            '那条链，未随包发布）。请自备一份 CSV 用 `csv` 参数传入，'
                            '或把刷新器放到 %s 后重试 —— 其余工具不受影响。' % WORKER_DIR)
        run([PY, _col], timeout=600)
    try:
        with open(path, encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        return tool_err('读台账失败（%s）：%s: %s' % (path, type(e).__name__, e))
    if not rows:
        return tool_ok({'ledger': path, 'total': 0, 'counts': {}, 'rows': []})
    cols = list(rows[0].keys())

    def col(*names):
        """先精确后包含地找列名：台账列名可能是下划线写法（`a_b`），精确匹配找不到就
        退化成子串匹配 —— 免得台账加一列/改个名就整个工具失灵。"""
        for n in names:
            for c in cols:
                if c.lower() == n.lower():
                    return c
        for n in names:
            for c in cols:
                if n.lower() in c.lower():
                    return c
        return None

    c_status = col('status', 'state')
    c_sp = col('name', 'item', 'label')
    c_group = col('group', 'tier')
    counts = {}
    for r in rows:
        k = (r.get(c_status) or '?').strip()
        counts[k] = counts.get(k, 0) + 1
    sel = rows
    if a.get('state') and c_status:
        want = str(a['state']).upper()
        sel = [r for r in sel if (r.get(c_status) or '').upper() == want]
    if a.get('name') and c_sp:
        sel = [r for r in sel if str(a['name']).lower() in (r.get(c_sp) or '').lower()]
    if a.get('group') and c_group:
        sel = [r for r in sel if (r.get(c_group) or '') == a['group']]
    limit = max(1, min(1000, int(a.get('limit') or 50)))
    return tool_ok({'ledger': path, 'total': len(rows), 'counts': counts,
                    'matched': len(sel), 'returned': min(len(sel), limit),
                    'columns': cols, 'rows': sel[:limit]})


# ---------------------------------------------------------------- 工具：download_status

def tool_download_status(a):
    """单个输出文件的进度：读既有 sidecar（`<output>.download.json`）+ 落地大小。"""
    out_path = str(a.get('output') or '')
    if not out_path:
        return tool_err('download_status 需要 output')
    side = out_path + '.download.json'
    sc = read_json(side)
    size = None
    try:
        size = os.path.getsize(out_path)
    except OSError:
        size = None
    if sc is None and size is None:
        return tool_ok({'output': out_path, 'state': 'not_started'})
    done = sc.get('completed_chunks') if isinstance(sc, dict) else None
    total_chunks = None
    if isinstance(sc, dict) and sc.get('size') and sc.get('chunk_size'):
        total_chunks = (int(sc['size']) + int(sc['chunk_size']) - 1) // int(sc['chunk_size'])
    state = 'running_or_incomplete' if sc is not None else 'completed'
    return tool_ok({'output': out_path, 'state': state,
                    'sidecar': side if sc is not None else None,
                    'size': (sc or {}).get('size'), 'on_disk_bytes': size,
                    'chunk_size': (sc or {}).get('chunk_size'),
                    'chunks_done': (len(done) if isinstance(done, list) else done),
                    'chunks_total': total_chunks,
                    'note': 'sidecar 存在 = 未完成（重跑同一条下载命令即续传）；'
                            '校验全过之后 downloader 才会删掉 sidecar'})


# ---------------------------------------------------------------- 工具：download（租约感知）

def _pick_port(prefer=None):
    """挑一个**没被别人占**的口（cap·外部占用 通过 dl_broker_observe 的口径计算）。

    MCP 自己不 probe 内核/端口，一律复用观测半身；观测不可用时返回 (None, 原因)。
    """
    try:
        import dl_broker_observe as OBS
        cfg = OBS.load_cfg()
        if prefer is not None:
            import worker_pool as WP
            if int(prefer) not in set(WP.alive_ports()):
                return None, '你指定的口 %s 现在不存活' % prefer
        obs = OBS.observe(cfg, with_traffic=False)
    except Exception as e:
        return None, '观测不可用（%s: %s）' % (type(e).__name__, e)
    cands = []
    for pstr, rec in (obs.get('ports') or {}).items():
        p = int(pstr)
        if prefer is not None and p != int(prefer):
            continue
        if not rec.get('enabled', True):        # 注册表里关掉的口（PC 不在/人为关）不借
            continue
        if not rec.get('ss_ok'):
            continue
        # my_share 由观测层算好（= max(0, cap - 外部占用)），不在这儿重算一遍
        if int(rec.get('my_share') or 0) <= 0:
            continue
        cands.append((rec.get('traffic_mbps') or 0.0, p, rec))
    if not cands:
        return None, '没有空口（都被外面占满了或都不可用）'
    cands.sort(key=lambda t: (t[0], t[1]))          # 优先最闲的口
    return cands[0][1], cands[0][2]


def tool_download(a):
    """**临时单文件**下载：不进队列，但先声明租约、避开别人在用的口。

    适用：临时补一个文件、调试、跑单条直链。**批量/正式任务请用 `submit` 进队列**
    （排队 + 优先级 + 跨 agent 去重都在那里）。
    """
    url = str(a.get('url') or '')
    output = str(a.get('output') or '')
    if not url.startswith(('http://', 'https://')) or not output.startswith('/'):
        return tool_err('download 需要 http(s) 的 url 与绝对路径 output')
    prefer = a.get('port')
    port, why = _pick_port(prefer)
    declared = None
    if port is None and prefer is not None:
        return tool_err('指定的口不能用：%s' % why)
    if port is not None:
        try:
            import dl_lease as L
            declared = L.declare(port, {'owner': str(a.get('owner') or 'agent'),
                                        'what': 'mcp-download', 'url': url[:200]},
                                 task={'output': output})
        except Exception as e:
            log_err('advisory 声明失败（不影响下载）：%s: %s' % (type(e).__name__, e))
    cmd = [PY, DOWNLOADER, url, '-o', output, '--worker',
           ('http://127.0.0.1:%d' % port) if port else 'auto']
    if a.get('md5'):
        cmd += ['--md5', str(a['md5'])]
    if a.get('connections'):
        cmd += ['--connections', str(int(a['connections']))]
    if a.get('direct'):
        cmd += ['--direct']
    # 关键：**detached** 后台跑（长下载几十上百 GB，MCP 调用不能阻塞）
    # 需求式建目录：全新安装里 state/ 还不存在（它由 driver 首次运行时建）。
    # 本 MCP 只在这一处写自己的日志，所以就地补 —— 不在启动时建，
    # 免得"只想读状态"的安装也被动留下目录。
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError:
        pass
    logf = open(os.path.join(STATE_DIR, 'mcp_download_%d.log' % int(time.time())), 'ab')
    try:
        p = subprocess.Popen(cmd, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                             cwd=WORKER_DIR, start_new_session=True)
    except Exception as e:
        return tool_err('起下载进程失败：%s: %s' % (type(e).__name__, e))
    time.sleep(1.0)
    # 判"成没成"要看**退出码**，不能只看"进程还在不在"：
    # 小文件不到 1 秒就下完了，那时进程早已正常退出 —— 只看存活会把一次
    # 成功的下载报成失败（而且它真的下好了，用户按报错去查反而更迷惑）。
    # 退出码是可信信号：下载器只在大小/md5/卡洞全过之后才 0 退出。
    rc = p.poll()
    running = rc is None
    ok = running or rc == 0
    payload = {'started': True, 'finished': (not running), 'exit_code': rc,
               'pid': p.pid, 'url': url, 'output': output,
               'queued': False, 'via': 'mcp-direct',
               'worker': ('http://127.0.0.1:%d' % port) if port else 'auto（driver 池内自选）',
               'lease': ('已声明 advisory: %s' % declared) if declared else ('未声明：%s' % why),
               'log': logf.name,
               'note': ('这是**临时单文件**通道（不排队、不跨 agent 去重）；批量任务请用 submit 进队列'
                        + ('。已跑完并通过终验' if (not running and rc == 0) else '')),
               'status_tool': 'download_status（传同一个 output）'}
    if not ok:
        payload['note'] = '进程已退出且退出码非 0（%s）—— 看日志：%s' % (rc, logf.name)
        return {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False, indent=1)}],
                'structuredContent': payload, 'isError': True}
    if not running:                     # 秒完成：别让调用方去等一个已经结束的任务
        payload['started'] = True
        payload['done'] = True
    return tool_ok(payload)


# ---------------------------------------------------------------- 工具表

TOOLS = [
    {"name": "submit",
     "description": "提交下载申请到**统一队列**（推荐入口）。写进 inbox/，driver 每 ~5s 排空，"
                    "按优先级派发、跨 agent 去重（已在队列/在途/盘上落地的会被拒并给原因）。"
                    "**一次要投多条时用 `items` 批量**（单条则照旧给 name/platform/outdir）。",
     "inputSchema": {"type": "object",
                     "properties": {
                         "items": {"type": "array", "items": {"type": "object"},
                                   "description": "**批量提交**：一次投多条，每个元素与单条参数同名"
                                                  "（name/platform/outdir/prefix/build/url/level/note/...）。"
                                                  "顶层的同名参数与 `defaults` 会作为默认值合并进去，"
                                                  "所以每条只需写差异字段。上限 1000 条，"
                                                  "失败的那几条不会写进收件箱（逐条给原因）"},
                         "defaults": {"type": "object",
                                      "description": "批量时的公共默认值（如 platform/outdir/owner/level），"
                                                     "优先级低于每条 item 自身的字段"},
                         "name": {"type": "string", "description": "目标名（提交单条时必填）"},
                         "platform": {"type": "string", "enum": list(_PLATFORMS),
                                      "description": "取数通道（alpha=对象存储主通道，beta=对象存储副通道，"
                                                     "gamma=http(s) 直链）"},
                         "outdir": {"type": "string", "description": "落地目录（绝对路径）"},
                         "owner": {"type": "string", "description": "申请人（默认 agent）"},
                         "level": {"type": "string", "enum": list(_LEVELS),
                                   "description": "下载等级（默认 p2）：p0 加急 / p1 高 / p2 普通 / "
                                                  "p3 低 / p4 批量。**同等级内，后提交的先跑**"
                                                  "（想插队就用更高等级，或晚点再说一次）"},
                         "priority": {"type": "integer", "description": "老写法（裸数值 0-100+，"
                                                                       "≥90 插队），新提交请用 level；"
                                                                       "不传则按等级默认 50"},
                         "prefix": {"type": "string", "description": "SOURCE_A：对象前缀"},
                         "build": {"type": "string", "description": "SOURCE_A alpha：构建名"},
                         "object_key": {"type": "string", "description": "SOURCE_A beta：对象键"},
                         "url": {"type": "string", "description": "SOURCE_B：http(s) 直链"},
                         "source": {"type": "string", "description": "默认按 platform 推（SOURCE_A/SOURCE_B）"},
                         "est_gb": {"type": "number", "description": "预估大小 GB（信息性）"},
                         "prefer_win": {"type": "boolean", "description": "标注更适合 windows 线（信息性）"},
                         "note": {"type": "string", "description": "备注（写进申请单，便于追溯）"}},
                     }},
    {"name": "queue_status",
     "description": "一屏看队列与实况：排队中的申请 + driver 队列深度/在跑线/口占用/控制状态/让路"
                    " + **按通道（linux/<隧道口>）的逐线下载进度与速率**（跨 agent，其它 agent 起的线也在内）。",
     "inputSchema": {"type": "object", "properties": {
         "recent": {"type": "integer", "description": "done/rejected 各显示几条（默认 5）"},
         "driver": {"type": "string", "description": "目标 driver（默认当前）"}}}},
    {"name": "ctl",
     "description": "运行时控制 driver（等价于 dl_ctl.py）：lines/pause/resume/promote/drop/skip/unskip/avoid/unavoid。",
     "inputSchema": {"type": "object", "required": ["action"], "properties": {
         "action": {"type": "string", "enum": list(_CTL_SUBS)},
         "n": {"type": "integer", "description": "lines 用：线数"},
         "grace": {"type": "boolean", "description": "lines 用：优雅退休（跑完手头再退）"},
         "match": {"type": "array", "items": {"type": "string"},
                   "description": "promote/drop/skip/unskip 用：物种子串"},
         "ports": {"type": "array", "items": {"type": "integer"}, "description": "avoid/unavoid 用：端口"},
         "all": {"type": "boolean", "description": "unskip 用：清空跳过列表"},
         "wait": {"type": "number", "description": "等待 driver 确认生效的秒数（默认 30）"},
         "driver": {"type": "string"}}}},
    {"name": "lease_status",
     "description": "跨 agent 的口租约与争用观测（只读）：每个口谁在占（内核 ss 事实）、我可分几条、"
                    "有没有 undeclared 的外部占用。",
     "inputSchema": {"type": "object", "properties": {
         "traffic": {"type": "boolean", "description": "顺带做 2s 流量采样（慢一点）"}}}},
    {"name": "worker_status",
     "description": "worker 口存活/延迟/流量（池感知，读既有探活层）。",
     "inputSchema": {"type": "object", "properties": {
         "port": {"type": "integer", "description": "只看某口"},
         "traffic": {"type": "boolean", "description": "默认带流量标签；false = 跳过（快些）"}}}},
    {"name": "alerts",
     "description": "读 worker/broker 告警流水（下线/上线/争用/让路/SSD）。",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "最多返回多少条（默认 20，上限 500）"},
         "event": {"type": "string", "description": "只看某类事件（如 BROKER_AVOID / oversubscribed）"},
         "since": {"type": "string", "description": "只取该 UTC+8 时间戳之后的（如 'YYYY-MM-DD HH:MM:SS'）"}}}},
    {"name": "overview",
     "description": "下载台账 CSV 的汇总（状态计数 + 筛选）。只读，不写台账。",
     "inputSchema": {"type": "object", "properties": {
         "state": {"type": "string", "description": "只看某状态（DONE/QUEUED/REPAIRING…）"},
         "name": {"type": "string", "description": "按名称包含匹配"},
         "group": {"type": "string", "description": "按分组精确匹配"},
         "limit": {"type": "integer", "description": "最多返回多少行（默认 50）"},
         "refresh": {"type": "boolean", "description": "先跑 status_collector 刷新（慢，默认 false）"},
         "csv": {"type": "string", "description": "换一份台账 CSV"}}}},
    {"name": "inventory",
     "description": "**数据清单 data_status.csv 的自管刷新**（全盘扫描 → 原子换入 → 重新生成 CSV → "
                    "Notion 同步）。driver 每 ~10 min 自动跑一次；这里手动看/推。"
                    "action=status 只读看状态（默认）；auto 按状态做该做的事；"
                    "start 强制起扫（NFS 全盘 10–30 min，后台 tmux）；finish 强制收口（后台，几分钟）。"
                    "除 status 外都丢后台，立即返回。",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["status", "auto", "start", "finish"],
                    "description": "默认 status（只读）"}}}},
    {"name": "download_status",
     "description": "单个输出文件的进度（读既有 sidecar，不自己实现下载逻辑）。",
     "inputSchema": {"type": "object", "required": ["output"], "properties": {
         "output": {"type": "string", "description": "输出文件路径（下载时传的那个）"}}}},
    {"name": "download",
     "description": "**临时单文件**下载（不进队列）：租约感知（挑空口 + advisory 声明）、后台 detached 跑。"
                    "批量/正式任务请用 submit。",
     "inputSchema": {"type": "object", "required": ["url", "output"], "properties": {
         "url": {"type": "string"}, "output": {"type": "string"},
         "md5": {"type": "string", "description": "官方 md5（生物大文件强烈建议给）"},
         "connections": {"type": "integer", "description": "并发连接（1-4）"},
         "port": {"type": "integer", "description": "指定 worker 口（默认自动挑最闲的）"},
         "direct": {"type": "boolean", "description": "绕过 worker 直连（对比/小文件用）"},
         "owner": {"type": "string"}}}},
]

HANDLERS = {'submit': tool_submit, 'queue_status': tool_queue_status, 'ctl': tool_ctl,
            'lease_status': tool_lease_status, 'worker_status': tool_worker_status,
            'alerts': tool_alerts, 'overview': tool_overview, 'inventory': tool_inventory,
            'download_status': tool_download_status, 'download': tool_download}

INSTRUCTIONS = (
    '统一下载 MCP（download-worker 2.x）：所有下载申请的**统一入口**。'
    '正式/批量任务用 submit 进队列（submit 带 level 等级：p0 加急 / p1 高 / p2 普通（默认）/ '
    'p3 低 / p4 批量；**同等级内后提交的先跑**；driver 按等级派发、跨 agent 去重、口不够时自动排队）；'
    'queue_status / lease_status / worker_status / alerts / overview 是只读实况；'
    'ctl 用来改线数/暂停/插队/让路；download 只用于临时单文件（不进队列，但会先声明租约、避开别人在用的口）。'
    '本 MCP 无状态：状态都在共享文件与 flock 里，多 agent 各起一份也不会互相抢口。'
)


# ---------------------------------------------------------------- 主循环

def handle(msg):
    if not isinstance(msg, dict):
        return
    method, rid = msg.get('method'), msg.get('id')
    if method == 'initialize':
        params = msg.get('params') or {}
        pv = params.get('protocolVersion')
        log('initialize: client protocolVersion=%r' % (pv,))
        reply(rid, {'protocolVersion': pv if isinstance(pv, str) and pv else PROTOCOL_FALLBACK,
                    'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
                    'instructions': INSTRUCTIONS})
    elif method == 'tools/list':
        reply(rid, {'tools': TOOLS})
    elif method == 'tools/call':
        params = msg.get('params') or {}
        name, args = params.get('name'), params.get('arguments') or {}
        h = HANDLERS.get(name)
        if h is None:
            reply_error(rid, -32602, 'unknown tool: %r' % name)
            return
        log('tools/call %s args=%s' % (name, json.dumps(args, ensure_ascii=False)[:200]))
        try:
            result = h(args)
        except Exception as e:
            log_err('tool %s crashed: %s: %s' % (name, type(e).__name__, e))
            result = tool_err('internal error in %s: %s: %s' % (name, type(e).__name__, e))
        reply(rid, result)
    elif method == 'ping':
        reply(rid, {})
    elif rid is None:
        pass                      # 通知（notifications/initialized 等）不应答
    else:
        reply_error(rid, -32601, 'method not found: %r' % method)


def serve():
    log_err('started v%s | mcp=%s | tools=%s | driver=%s | python=%s | pid=%d'
            % (SERVER_VERSION, HERE, WORKER_DIR, DRIVER, PY, os.getpid()))
    while True:
        line = sys.stdin.readline()
        if not line:
            log('stdin EOF, exiting')
            return
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            reply_error(None, -32700, 'parse error: %s' % e)
            continue
        handle(msg)


if __name__ == '__main__':
    try:
        serve()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    except Exception as e:
        log_err('fatal: %s: %s' % (type(e).__name__, e))
        sys.exit(1)
