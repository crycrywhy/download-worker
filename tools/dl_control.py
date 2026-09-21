import contextlib
import fcntl
import json
import os
import tempfile
import time

CONTROL_VERSION = 1
SOURCE_KINDS = ('SOURCE_A', 'SOURCE_B')
PLATFORMS = ('alpha', 'beta', 'gamma')
PENDING_OPS = ('promote', 'drop', 'add')
STATE_OPS = ('skip', 'unskip', 'pause', 'resume', 'lines', 'avoid', 'unavoid')
ALL_OPS = PENDING_OPS + STATE_OPS

WIN_LINES_HARD_MAX = 32

CAPS_BASE = ('lines', 'add', 'promote', 'drop', 'skip', 'unskip', 'pause', 'resume')
CAPS_AVOID = ('avoid', 'unavoid')
ALL_CAPS = CAPS_BASE + CAPS_AVOID


LEVELS = (
    ('p0', 90, '加急', 'urgent'),
    ('p1', 70, '高',   'high'),
    ('p2', 50, '普通', 'normal'),
    ('p3', 30, '低',   'low'),
    ('p4', 10, '批量', 'bulk'),
)
DEFAULT_LEVEL = 'p2'
DEFAULT_PRIORITY = 50
PRIORITY_FRONT = 90

LEVEL_BY_NAME = {}
for _code, _val, _zh, _en in LEVELS:
    LEVEL_BY_NAME[_code] = _val
    LEVEL_BY_NAME[_en] = _val
    LEVEL_BY_NAME[_zh] = _val


def level_codes():
    return tuple(code for code, _v, _z, _e in LEVELS)


def parse_level(s, default=None):
    if s is None or s == '' or isinstance(s, bool):
        return default
    if isinstance(s, int):
        return int(s)
    t = str(s).strip()
    if t.lower() in LEVEL_BY_NAME:
        return LEVEL_BY_NAME[t.lower()]
    try:
        return int(float(t))
    except (TypeError, ValueError):
        return default


def level_name(prio):
    try:
        p = int(prio)
    except (TypeError, ValueError):
        return '?'
    for code, val, _zh, _en in LEVELS:
        if p >= val:
            return code
    return LEVELS[-1][0]


def fmt_hm(ts):
    s = str(ts or '')
    return s[11:16] if (len(s) >= 16 and s[10] == 'T') else (s[:5] or '--:--')


def level_desc():
    return '；'.join('%s=%s(%d)%s' % (c, z, v, '（默认）' if c == DEFAULT_LEVEL else '')
                     for c, v, z, _e in LEVELS)



def load_ctl(path):
    try:
        with open(path) as f:
            spec = json.load(f)
    except Exception:
        return None
    if not isinstance(spec, dict):
        return None
    for k, v in new_spec().items():
        spec.setdefault(k, v)
    return spec


def save_ctl(path, spec):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    spec = dict(spec)
    spec['v'] = CONTROL_VERSION
    spec['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.ctl-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(spec, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return spec


def new_spec(win_lines=None, driver=None):
    return {'v': CONTROL_VERSION, 'rev': 0, 'driver': driver,
            'win_lines': (None if win_lines is None else int(win_lines)),
            'paused': False, 'skip': [], 'ops': [], 'last_ops': [],
            'avoid_ports': [], 'policy_avoid_ports': [], 'broker_max_lines': None}


def bump(spec, ops, **fields):
    spec = dict(spec or new_spec())
    spec['rev'] = int(spec.get('rev') or 0) + 1
    spec['ops'] = list(ops or [])
    spec.update(fields)
    return spec


def bump_fields(spec, **fields):
    spec = dict(spec or new_spec())
    spec['rev'] = int(spec.get('rev') or 0) + 1
    spec['ops'] = list(spec.get('ops') or [])
    spec.update(fields)
    return spec


def lock_file_for(ctl_path):
    d = os.path.dirname(os.path.abspath(ctl_path))
    return os.path.join(d, '.' + os.path.basename(ctl_path) + '.lock')


@contextlib.contextmanager
def locked(ctl_path, timeout=10.0):
    lk = lock_file_for(ctl_path)
    os.makedirs(os.path.dirname(lk), exist_ok=True)
    fd = os.open(lk, os.O_RDWR | os.O_CREAT, 0o664)
    got = False
    try:
        end = time.time() + max(0.0, float(timeout))
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.time() >= end:
                    raise TimeoutError(f'等控制文件锁超时（{timeout}s）：{lk}')
                time.sleep(0.05)
        yield fd
    finally:
        if got:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)



def _aslist(v):
    return v if isinstance(v, list) else []


def _is_matcher(op):
    m = op.get('match')
    if not isinstance(m, list) or not m:
        return False, 'match 必须是非空列表'
    for s in m:
        if not isinstance(s, str) or not s.strip():
            return False, 'match 里必须是非空字符串'
    return True, 'ok'


def _check_ports(v, allow_empty=True):
    if not isinstance(v, list):
        return False, 'ports 必须是列表'
    if not v and not allow_empty:
        return False, 'ports 不能为空'
    for p in v:
        if isinstance(p, bool) or not isinstance(p, int):
            return False, f'ports 里必须都是整数（收到 {p!r}）'
        if not (1 <= p <= 65535):
            return False, f'端口越界: {p}'
    return True, 'ok'


def validate_op(op):
    if not isinstance(op, dict):
        return False, 'op 不是对象'
    kind = op.get('op')
    if kind not in ALL_OPS:
        return False, f'未知 op: {kind!r}'
    if kind in ('promote', 'drop', 'skip', 'unskip'):
        ok, msg = _is_matcher(op)
        if not ok and not (kind == 'unskip' and op.get('all')):
            return False, f'{kind}: {msg}'
        if kind == 'promote' and op.get('limit') is not None:
            try:
                if int(op['limit']) < 0:
                    return False, 'promote: limit 不能为负'
            except (TypeError, ValueError):
                return False, 'promote: limit 不是整数'
    elif kind == 'lines':
        try:
            n = int(op.get('n'))
        except (TypeError, ValueError):
            return False, 'lines: n 不是整数'
        if not 0 <= n <= WIN_LINES_HARD_MAX:
            return False, f'lines: n={n} 超出 0..{WIN_LINES_HARD_MAX}'
    elif kind in ('avoid', 'unavoid'):
        ok, msg = _check_ports(op.get('ports'))
        if not ok:
            return False, f'{kind}: {msg}'
    elif kind == 'add':
        entries = op.get('entries')
        if not isinstance(entries, list) or not entries:
            return False, 'add: entries 必须是非空列表'
        for i, e in enumerate(entries):
            try:
                build_task(e)
            except ValueError as ex:
                return False, f'add: entries[{i}] {ex}'
    return True, 'ok'


def validate(spec):
    if not isinstance(spec, dict):
        return False, '顶层不是对象'
    try:
        if int(spec.get('v') or CONTROL_VERSION) != CONTROL_VERSION:
            return False, f"结构版本不符: v={spec.get('v')}"
    except (TypeError, ValueError):
        return False, 'v 不是整数'
    wl = spec.get('win_lines')
    if wl is not None:
        try:
            n = int(wl)
        except (TypeError, ValueError):
            return False, 'win_lines 不是整数'
        if not 0 <= n <= WIN_LINES_HARD_MAX:
            return False, f'win_lines={n} 超出 0..{WIN_LINES_HARD_MAX}'
    for fld in ('avoid_ports', 'policy_avoid_ports'):
        if spec.get(fld) is not None:
            ok, msg = _check_ports(spec.get(fld))
            if not ok:
                return False, f'{fld}: {msg}'
    if spec.get('broker_max_lines') is not None:
        n = spec['broker_max_lines']
        if isinstance(n, bool) or not isinstance(n, int):
            return False, f'broker_max_lines 必须是整数或 null（收到 {n!r}）'
        if not 0 <= n <= WIN_LINES_HARD_MAX:
            return False, f'broker_max_lines={n} 超出 0..{WIN_LINES_HARD_MAX}'
    if not isinstance(spec.get('skip') or [], list) or not all(isinstance(s, str) for s in (spec.get('skip') or [])):
        return False, 'skip 必须是字符串列表'
    ops = spec.get('ops') or []
    if not isinstance(ops, list):
        return False, 'ops 不是列表'
    for i, op in enumerate(ops):
        ok, msg = validate_op(op)
        if not ok:
            return False, f'ops[{i}] {msg}'
    return True, 'ok'


def caps_of(snapshot):
    ctl = (snapshot or {}).get('ctl') or {}
    caps = ctl.get('caps')
    if isinstance(caps, list) and caps:
        return set(c for c in caps if isinstance(c, str))
    return set(CAPS_BASE)



def build_task(entry):
    if not isinstance(entry, dict):
        raise ValueError('entry 不是对象')
    name = str(entry.get('name') or entry.get('item') or '').strip()
    sp = name
    if not sp:
        raise ValueError('缺 item')
    outdir = str(entry.get('outdir') or '').strip()
    if not outdir.startswith('/'):
        raise ValueError('outdir 必须是绝对路径')
    platform = str(entry.get('platform') or '').strip().lower()
    if platform not in PLATFORMS:
        raise ValueError('platform must be one of alpha/beta/gamma')
    source = str(entry.get('source') or '').strip().upper()
    if not source:
        source = 'SOURCE_B' if platform == 'gamma' else 'SOURCE_A'
    if source not in SOURCE_KINDS:
        raise ValueError('source 必须是 SOURCE_A 或 SOURCE_B')
    if source == 'SOURCE_B':
        url = str(entry.get('url') or entry.get('url') or '').strip()
        if not url.startswith('http'):
            raise ValueError('SOURCE_B 需要 http(s) 的 url')
        if platform != 'gamma':
            raise ValueError('a SOURCE_B task must carry platform=gamma')
        return (sp, 'SOURCE_B', url, outdir, 'gamma')
    prefix = str(entry.get('prefix') or '').strip()
    if not prefix:
        raise ValueError('SOURCE_A 需要 prefix')
    build = str(entry.get('build') or '').strip()
    object_key = entry.get('object_key') or None
    if platform == 'beta':
        if not isinstance(object_key, str) or not object_key.strip():
            raise ValueError('beta 平台需要 object_key（对象键）')
        object_key = object_key.strip()
    else:
        if not build:
            raise ValueError('alpha 平台需要 build（构建名）')
    return (sp, 'SOURCE_A', (prefix, build, object_key), outdir, platform)


def payload_ok(t):
    try:
        if not isinstance(t, (tuple, list)) or len(t) != 5:
            return False
        sp, source, payload, outdir, platform = t
        if not isinstance(sp, str) or not sp.strip():
            return False
        if not isinstance(outdir, str) or not outdir.strip():
            return False
        if platform not in PLATFORMS:
            return False
        if source == 'SOURCE_B':
            return isinstance(payload, str) and payload.startswith('http')
        if source == 'SOURCE_A':
            return (isinstance(payload, (tuple, list)) and len(payload) == 3
                    and isinstance(payload[0], str) and bool(payload[0])
                    and (payload[2] is None or isinstance(payload[2], str)))
        return False
    except Exception:
        return False


def key_of(t):
    try:
        if isinstance(t, (tuple, list)) and len(t) == 2:
            return (str(t[0]), str(t[1]))
        return (str(t[0]), str(t[4]))
    except Exception:
        return (repr(t), '?')


def _hit(t, match):
    sp = str(t[0]).lower()
    return any(m.lower() in sp for m in match)



def apply_ops(pending, ops, inflight=()):
    pend = list(pending)
    reports, added, dups = [], [], []
    promoted, added_front = [], None
    infl = set()
    for t in inflight or ():
        infl.add(key_of(t))
    for op in (ops or []):
        kind = (op or {}).get('op')
        if kind == 'promote':
            match = op.get('match') or []
            limit = op.get('limit')
            hit = [t for t in pend if _hit(t, match)]
            if not hit:
                reports.append(f"promote: 无匹配（{'/'.join(match)}）")
                continue
            if limit is not None and int(limit) < len(hit):
                moved = hit[:int(limit)]
            else:
                moved = hit
            rest = [t for t in pend if t not in moved]
            pend = moved + rest
            promoted.extend(key_of(t) for t in moved)
            reports.append(f"promote: {len(moved)} 个任务提到队头（第一个 {moved[0][0]}）")
        elif kind == 'drop':
            match = op.get('match') or []
            dropped = [t for t in pend if _hit(t, match)]
            pend = [t for t in pend if not _hit(t, match)]
            if dropped:
                reports.append(f"drop: 移出 {len(dropped)} 个（{ '、'.join(t[0] for t in dropped[:4]) }"
                               f"{'…' if len(dropped) > 4 else ''}）")
            else:
                reports.append(f"drop: 无匹配（{'/'.join(match)}）")
        elif kind == 'add':
            keep = []
            for e in (op.get('entries') or []):
                t = build_task(e)
                k = key_of(t)
                dup = (not op.get('force')) and (k in infl or any(key_of(x) == k for x in pend + keep))
                if dup:
                    dups.append(k)
                    continue
                keep.append(t)
                added.append(k)
            added_front = bool(op.get('front'))
            if op.get('front'):
                pend = keep + pend
            else:
                pend = pend + keep
            if keep:
                reports.append(f"add: 注入 {len(keep)} 个任务（{'、'.join('%s[%s]' % (k[0], k[1]) for k in added)}）"
                               + ('→队头' if op.get('front') else '→队尾'))
            if dups:
                reports.append(f"add: 跳过重复 {len(dups)} 个（已在队列或在途；--force 可越过）："
                               + '、'.join('%s[%s]' % k for k in dups))
        elif kind in ('skip', 'unskip'):
            if kind == 'skip':
                reports.append(f"skip: 新增跳过 {'/'.join(op.get('match') or [])}")
            elif op.get('all'):
                reports.append('unskip: 清空跳过表')
            else:
                reports.append(f"unskip: 移除跳过 {'/'.join(op.get('match') or [])}")
        elif kind == 'pause':
            reports.append('pause: 暂停取新任务（在途跑完）')
        elif kind == 'resume':
            reports.append('resume: 恢复取任务')
        elif kind == 'lines':
            reports.append("lines: 线数 → %s%s" % (op.get('n'), '（立刻中断）' if op.get('now') else '（优雅退休）'))
        elif kind == 'avoid':
            reports.append("avoid: 绕开 %s（该口被外部占满，我不再去抢）"
                           % ','.join(str(p) for p in op.get('ports') or []))
        elif kind == 'unavoid':
            reports.append("unavoid: 恢复使用 %s"
                           % (','.join(str(p) for p in op.get('ports') or []) or '（全部）'))
    return {'pending': pend, 'reports': reports, 'added': added, 'dups': dups,
            'promoted': promoted, 'added_front': added_front}


def summarize(spec):
    if not spec:
        return '(无控制文件)'
    return ("rev=%s win_lines=%s paused=%s skip=%s ops=%d updated=%s by=%s"
            % (spec.get('rev'), '-' if spec.get('win_lines') is None else spec.get('win_lines'),
               bool(spec.get('paused')),
               ','.join(spec.get('skip') or []) or '-', len(spec.get('ops') or []),
               spec.get('updated') or '-', spec.get('by') or '-'))
