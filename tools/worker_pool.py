import argparse
import contextlib
import fcntl
import http.client
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time

DEFAULT_TRAFFIC_WINDOW = 2.0
TRAFFIC_MIN_BYTES = 64 * 1024

POOL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, POOL_DIR)
import dl_config as CFG
WORKERS_JSON = os.environ.get('WORKERS_JSON', os.path.join(POOL_DIR, 'workers.json'))
STATUS_JSON = os.environ.get('WORKER_STATUS_JSON', os.path.join(POOL_DIR, 'worker_status.json'))
STATUS_LOCK_TIMEOUT = 10.0

DEFAULT_REGISTRY = CFG.get_map('endpoints.default_registry', {
    "scan_range": [],
    "workers": {},
    "speed_test_url": "",
})
TS_BIN = CFG.get('tools.tailscale_bin', '')
TS_SOCK = CFG.get('tools.tailscale_sock', '')
DEFAULT_TIMEOUT = 2.5
DEFAULT_DEADLINE = 5.5
FAIL_STREAK = 3
UP_STREAK = 2



def _norm_workers(ws):
    out = {}
    if not isinstance(ws, dict):
        return out
    for k, v in ws.items():
        try:
            port = int(k)
        except (TypeError, ValueError):
            continue
        if not (1 <= port <= 65535) or not isinstance(v, dict):
            continue
        out[port] = {'label': str(v.get('label', f'port{port}')),
                     'enabled': bool(v.get('enabled', True)),
                     'device': str(v.get('device', ''))}
    return out


def load_registry(path=None):
    path = path or WORKERS_JSON
    reg = dict(DEFAULT_REGISTRY)
    reg['workers'] = _norm_workers(reg.get('workers'))
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError('registry not a dict')
    except Exception:
        return reg
    workers = _norm_workers(raw.get('workers'))
    if workers:
        reg['workers'] = workers
    rng = raw.get('scan_range')
    if (isinstance(rng, (list, tuple)) and len(rng) == 2
            and all(isinstance(x, int) for x in rng) and 1 <= rng[0] <= rng[1] <= 65535):
        reg['scan_range'] = [int(rng[0]), int(rng[1])]
    if isinstance(raw.get('speed_test_url'), str) and raw['speed_test_url']:
        reg['speed_test_url'] = raw['speed_test_url']
    return reg


def set_enabled(port, enabled, path=None):
    path = path or WORKERS_JSON
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError('registry not a dict')
    except Exception as e:
        print(f'[set-enabled] 读取 {path} 失败：{e!r}')
        return 2
    ws = raw.get('workers')
    if not isinstance(ws, dict):
        print(f'[set-enabled] {path} 里没有 workers 段')
        return 2
    key = str(int(port))
    if key not in ws or not isinstance(ws[key], dict):
        print(f"[set-enabled] 注册表里没有 {key} 这个口（现有：{', '.join(sorted(ws))}）")
        return 2
    old = bool(ws[key].get('enabled', True))
    ws[key]['enabled'] = bool(enabled)
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
            f.write('\n')
        os.replace(tmp, path)
    except OSError as e:
        print(f'[set-enabled] 写 {path} 失败：{e!r}')
        return 2
    print(f"[set-enabled] {key}({ws[key].get('label', '')}) enabled: {old} → {bool(enabled)}"
          f"（{'启用' if enabled else '停用'}）")
    print('[set-enabled] 生效：哨兵/探活下一轮（60s 内）跳过该口；driver 下轮探活收回/移除该线')
    return 0


def parse_range(spec, reg=None, include_registry=True):
    reg = reg or load_registry()
    ports = []
    if spec:
        for part in str(spec).split(','):
            part = part.strip()
            if not part:
                continue
            if '-' in part:
                try:
                    a, b = part.split('-', 1)
                    ports += list(range(int(a), int(b) + 1))
                except ValueError:
                    pass
            else:
                try:
                    ports.append(int(part))
                except ValueError:
                    pass
    else:
        ports = sorted(reg['workers'].keys())
    if include_registry:
        ports += sorted(reg['workers'].keys())
    seen, out = set(), []
    for p in ports:
        if 1 <= p <= 65535 and p not in seen:
            seen.add(p)
            out.append(p)
    return out



def ts_devices():
    try:
        r = subprocess.run([TS_BIN, f'--socket={TS_SOCK}', 'status'],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
    except Exception:
        return None
    out = {}
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if len(parts) < 4:
            continue
        name = parts[1]
        st = (parts[4].lower() if len(parts) > 4 else '-')
        if st.startswith('active') or st.startswith('idle'):
            out[name] = True
        elif st.startswith('offline'):
            out[name] = False
        else:
            out[name] = None
    return out


def ts_device_offline(reg, port):
    dev = (reg.get('workers', {}).get(port, {}) or {}).get('device', '')
    if not dev:
        return False
    st = ts_devices()
    if st is None or st.get(dev) is None:
        return False
    return st[dev] is False


def health_check(port, label=None, timeout=DEFAULT_TIMEOUT, attempts=2):
    addr = f'127.0.0.1:{port}'
    err = 'no attempt'
    for _ in range(max(1, attempts)):
        c = None
        t0 = time.time()
        try:
            c = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
            c.request('GET', '/health')
            r = c.getresponse()
            body = r.read(512)
            if r.status == 200:
                try:
                    j = json.loads(body.decode('utf-8', 'replace'))
                except ValueError:
                    j = {}
                if j.get('status') == 'ok':
                    return {'ok': True, 'port': port, 'label': label,
                            'latency_ms': round((time.time() - t0) * 1000, 1), 'error': ''}
                err = f"bad /health payload: {body[:60]!r}"
            else:
                err = f"HTTP {r.status}"
        except socket.timeout:
            err = f'timeout (>{timeout}s)'
        except ConnectionRefusedError:
            err = 'connection refused'
        except OSError as e:
            err = str(e)[:80]
        finally:
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass
        time.sleep(0.3)
    return {'ok': False, 'port': port, 'label': label, 'latency_ms': None, 'error': err}


def _ss_snapshot():
    try:
        r = subprocess.run(['ss', '-tin'], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    out, conns = {}, {}
    lines = r.stdout.splitlines()
    for i, ln in enumerate(lines):
        f = ln.split()
        if len(f) < 5 or 'LISTEN' in ln or ':' not in f[3]:
            continue
        try:
            port = int(f[3].rsplit(':', 1)[1])
        except ValueError:
            continue
        blk = ln if 'bytes_' in ln else ln + ' ' + (lines[i + 1] if i + 1 < len(lines) else '')
        b = 0
        for tok in blk.replace('\t', ' ').split():
            if tok.startswith('bytes_sent:') or tok.startswith('bytes_received:'):
                try:
                    b += int(tok.split(':', 1)[1])
                except ValueError:
                    pass
        out[port] = out.get(port, 0) + b
        conns[port] = conns.get(port, 0) + 1
    return out, conns


def traffic_batch(ports, window=DEFAULT_TRAFFIC_WINDOW):
    ports = [int(p) for p in ports]
    a = _ss_snapshot()
    if a is None:
        return {p: {'traffic': None, 'traffic_mbps': None, 'traffic_bytes': None,
                    'traffic_conns': None} for p in ports}
    t0 = time.time()
    time.sleep(max(0.2, float(window)))
    b = _ss_snapshot()
    dt = max(1e-3, time.time() - t0)
    bsum, bconn = b if b else ({}, {})
    out = {}
    for p in ports:
        d = bsum.get(p, 0) - a[0].get(p, 0)
        if d < 0:
            d = 0
        out[p] = {'traffic': d >= TRAFFIC_MIN_BYTES,
                  'traffic_mbps': round(d / dt / 1048576.0, 2),
                  'traffic_bytes': d,
                  'traffic_conns': bconn.get(p, 0)}
    return out


def probe_all(ports=None, spec=None, timeout=DEFAULT_TIMEOUT, deadline=None,
              reg=None, skip_busy=(), attempts=2, include_registry=True, traffic=False):
    reg = reg or load_registry()
    plist = ports if ports is not None else parse_range(spec, reg, include_registry=include_registry)
    timeout = max(0.5, float(timeout))
    deadline = deadline if deadline is not None else timeout + 3.0
    skip_busy = set(skip_busy or ())
    out, threads = {}, []

    def _run(p, lab):
        try:
            out[p] = health_check(p, label=lab, timeout=timeout, attempts=attempts)
        except Exception as e:
            out[p] = {'ok': False, 'port': p, 'label': lab, 'latency_ms': None,
                      'error': f'{type(e).__name__}: {str(e)[:60]}'}

    enabled_ports = [p for p in plist if reg['workers'].get(p, {}).get('enabled', True)]
    ts_st = ts_devices() if plist else None
    manual_off = set()
    for p in plist:
        meta = reg['workers'].get(p, {})
        lab = meta.get('label', '')
        if p in skip_busy:
            out[p] = {'ok': None, 'port': p, 'label': lab, 'latency_ms': None,
                      'error': 'busy (skipped)', 'skipped': True, 'enabled': True}
            continue
        if meta and not meta.get('enabled', True):
            manual_off.add(p)
        dev = meta.get('device', '')
        if dev and ts_st is not None and ts_st.get(dev) is False:
            out[p] = {'ok': False, 'port': p, 'label': lab, 'latency_ms': None,
                      'error': f'tailscale device offline ({dev})', 'enabled': True}
            continue
        t = threading.Thread(target=_run, args=(p, lab), daemon=True)
        t.start()
        threads.append(t)
    t0 = time.time()
    for t in threads:
        t.join(timeout=max(0.1, deadline - (time.time() - t0)))
    for p in plist:
        if p not in out:
            lab = reg['workers'].get(p, {}).get('label', '')
            out[p] = {'ok': False, 'port': p, 'label': lab, 'latency_ms': None,
                      'error': f'no result within {deadline:.1f}s',
                      'enabled': reg['workers'].get(p, {}).get('enabled', True)}
    for p, d in out.items():
        d.setdefault('enabled', reg['workers'].get(p, {}).get('enabled', True))
        if p in manual_off:
            d['manual_off'] = True
            d['enabled'] = False
    if traffic:
        live = [p for p in out]
        try:
            tb = traffic_batch(live)
        except Exception:
            tb = {}
        for p, t in tb.items():
            if p in out:
                out[p].update(t)
    return out


def alive_ports(ports=None, spec=None, **kw):
    res = probe_all(ports=ports, spec=spec, **kw)
    return sorted(p for p, d in res.items() if d.get('ok') is True)


def alive_urls(ports=None, spec=None, **kw):
    return [f'http://127.0.0.1:{p}' for p in alive_ports(ports=ports, spec=spec, **kw)]


def pick_alive(ports=None, spec=None, **kw):
    urls = alive_urls(ports=ports, spec=spec, **kw)
    return random.choice(urls) if urls else None



def _now_str():
    local = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    utc8 = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() + 8 * 3600))
    return local, utc8


@contextlib.contextmanager
def _status_lock(path, timeout=STATUS_LOCK_TIMEOUT):
    lp = str(path) + '.lock'
    fd = os.open(lp, os.O_RDWR | os.O_CREAT, 0o644)
    end = time.time() + max(0.0, float(timeout))
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= end:
                    raise TimeoutError(f'没拿到 {lp} 的锁（等 {timeout}s）')
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def update_status(res, path=None, timeout=STATUS_LOCK_TIMEOUT):
    path = path or STATUS_JSON
    try:
        with _status_lock(path, timeout=timeout):
            return _update_status_locked(res, path)
    except TimeoutError as e:
        print(f'[worker_pool] 跳过本次状态写入：{e}', file=sys.stderr)
        return []


def _update_status_locked(res, path=None):
    path = path or STATUS_JSON
    local, utc8 = _now_str()
    try:
        with open(path, encoding='utf-8') as f:
            st = json.load(f)
        if not isinstance(st, dict):
            st = {}
    except Exception:
        st = {}
    prev_workers = st.get('workers', {})
    events = st.get('events', {})
    changed = []
    new_workers = {}
    for p in sorted(res.keys()):
        d = res[p]
        ps = str(p)
        old = prev_workers.get(ps, {})
        if d.get('skipped'):
            new_workers[ps] = dict(old, label=d.get('label', old.get('label', '')),
                                   enabled=d.get('enabled', True), note=d.get('error', ''))
            continue
        old.pop('note', None)
        if 'was_down' not in old:
            old['was_down'] = None
        if d.get('ok'):
            old['fail_streak'] = 0
            old['ok_streak'] = old.get('ok_streak', 0) + 1
            if old.get('was_down') is True and old['ok_streak'] >= UP_STREAK:
                events.setdefault(ps, {})['up_last'] = utc8
                old['down_since'] = None
                changed.append((ps, 'UP'))
                old['was_down'] = False
            old.update({'label': d.get('label', ''), 'enabled': d.get('enabled', True),
                        'last_ok_utc8': utc8, 'last_ok_local': local,
                        'latency_ms': d.get('latency_ms'), 'error': ''})
            if d.get('traffic') is not None:
                old['traffic'] = d['traffic']
                old['traffic_mbps'] = d.get('traffic_mbps')
                old['traffic_conns'] = d.get('traffic_conns')
                if d['traffic']:
                    old['traffic_seen_utc8'] = utc8
        else:
            old['ok_streak'] = 0
            old['fail_streak'] = old.get('fail_streak', 0) + 1
            if old['fail_streak'] >= FAIL_STREAK and old.get('was_down') is not True:
                events.setdefault(ps, {})['down_last'] = utc8
                old['down_since'] = utc8
                changed.append((ps, 'DOWN'))
                old['was_down'] = True
            old.update({'label': d.get('label', ''), 'enabled': d.get('enabled', True),
                        'last_fail_utc8': utc8, 'last_fail_local': local,
                        'error': (d.get('error') or '')[:120]})
            if old.get('was_down') is True and not old.get('down_since'):
                old['down_since'] = utc8
        old['ok'] = not (old.get('was_down') is True)
        if old.get('ok') and old.get('was_down') is False:
            old['error'] = ''
        new_workers[ps] = old
    st['updated_local'], st['updated_utc8'] = local, utc8
    st['_note'] = ('本文件由 worker_pool.py 哨兵/探活自动维护（每 60s 一轮，双时间戳）。'
                   'agent 查端口可用性看这里即可：workers.<port>.ok + last_ok/error/down_since + events.<port>.{up,down}_last。'
                   'traffic = **探测型标签**：内核 socket 字节计数有增量 ⇒ true（该口正在传），'
                   '无增量 ⇒ false（空闲），true 时附 traffic_mbps 与 traffic_seen_utc8；'
                   'manual_off = 注册表标了 enabled=false（仅静默告警，不再拦接活）。')
    st['workers'] = new_workers
    st['events'] = events
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass
    return changed


def watch(interval=60, timeout=DEFAULT_TIMEOUT):
    print(f"[sentinel] 探活哨兵启动：每 {interval}s 一轮，状态文件 {STATUS_JSON}（Ctrl+C 停）", flush=True)
    first = True
    while True:
        reg = load_registry()
        try:
            res = probe_all(reg=reg, timeout=timeout, traffic=True)
        except Exception as e:
            print(f"[sentinel] 探测异常(下轮再试): {e!r}", flush=True)
            time.sleep(interval)
            continue
        changed = update_status(res)
        local, _ = _now_str()
        if changed:
            for port, what in changed:
                d = res.get(int(port), {})
                icon = '✅ 上线' if what == 'UP' else '⚠️ 下线'
                detail = f'{d.get("latency_ms")} ms' if what == 'UP' else (d.get('error') or '')
                print(f'[{local}] {icon}: {port}({d.get("label","")}) {detail}', flush=True)
        elif first:
            alive = [p for p, d in res.items() if d.get('ok') is True]
            print(f'[{local}] [sentinel] 基线：存活 {sorted(str(p) for p in alive)}', flush=True)
        first = False
        time.sleep(interval)



def speed_test(worker_url, test_url, mib=16, timeout=30):
    port = int(worker_url.rsplit(':', 1)[-1].rstrip('/'))
    from urllib.parse import quote
    path = '/stream?url=' + quote(test_url, safe='')
    want = int(mib * 1024 * 1024)
    c = None
    try:
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
        c.request('GET', path, headers={'Range': f'bytes=0-{want-1}'})
        r = c.getresponse()
        if r.status not in (200, 206):
            return {'ok': False, 'error': f'HTTP {r.status}', 'mib': 0.0, 'seconds': 0.0}
        got, t0 = 0, time.time()
        while got < want:
            b = r.read(min(1 << 20, want - got))
            if not b:
                break
            got += len(b)
        dt = max(1e-3, time.time() - t0)
        return {'ok': got > 0, 'mib': round(got / 1048576, 1), 'seconds': round(dt, 2),
                'mib_per_s': round(got / 1048576 / dt, 2), 'error': ''}
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {str(e)[:60]}', 'mib': 0.0, 'seconds': 0.0}
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass



def _traffic_text(d):
    t = d.get('traffic')
    if t is True:
        return f'在传 {d.get("traffic_mbps", 0):.2f} MB/s'
    if t is False:
        return '空闲'
    return '流量未知'


def _fmt_row(d):
    p, lab = d['port'], (d.get('label') or '')
    name = f'{p}({lab})' if lab else str(p)
    if d.get('skipped'):
        return f'  {name:<18} BUSY（本进程在用，跳过探测）'
    if d['ok']:
        row = f'  {name:<18} ✅ {d["latency_ms"]} ms  {_traffic_text(d)}'
        if d.get('manual_off'):
            row += '（注册表标了停用，但口活着 → 仍可用）'
        return row
    return f'  {name:<18} ❌ {d.get("error", "down")}'


def main(argv=None):
    ap = argparse.ArgumentParser(description='Windows worker 池探活/发现（多 PC）')
    ap.add_argument('--json', action='store_true', help='JSON 输出（agent 用）')
    ap.add_argument('--ports', action='store_true', help='只打存活端口 URL（一行一个）')
    ap.add_argument('--speed', action='store_true', help='对存活口追加测速（占满其上行数秒）')
    ap.add_argument('--speed-mib', type=int, default=16, help='每次测速拉取量 MiB（默认 16）')
    ap.add_argument('--range', default=None, help='临时扫描范围，如 <起始>-<结束>（默认=注册表端口）')
    ap.add_argument('--no-traffic', action='store_true',
                    help='跳过探测型流量标签（默认探；每轮多花 ~2s 采样窗口）')
    ap.add_argument('--no-update', action='store_true',
                    help='只探不写 worker_status.json（纯查询/不想参与事件记账时用；哨兵与 driver 别加）')
    ap.add_argument('--no-registry', action='store_true', help='只探 --range 指定端口，不并入注册表端口')
    ap.add_argument('--status', action='store_true', help='显示上次哨兵写入的状态文件（含最近上线/下线时间）')
    ap.add_argument('--watch', nargs='?', type=int, const=60, default=None, metavar='SEC',
                    help='常驻哨兵：每 SEC 秒探活（默认 60），状态变化写 worker_status.json')
    ap.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT, help=f'单口 /health 超时秒（默认 {DEFAULT_TIMEOUT}）')
    ap.add_argument('--set-enabled', nargs=2, default=None, metavar=('PORT', 'TRUE|FALSE'),
                    help='改注册表某口的 enabled（Windows 侧 download-worker on/off 经 ssh 调用）')
    ap.add_argument('--get-enabled', type=int, default=None, metavar='PORT',
                    help='打印某口 enabled（true/false/unknown，机器可读）')
    a = ap.parse_args(argv)

    if a.set_enabled:
        port_text, value_text = a.set_enabled
        value = str(value_text).strip().lower()
        if value in ('true', '1', 'on', 'yes'):
            return set_enabled(int(port_text), True)
        if value in ('false', '0', 'off', 'no'):
            return set_enabled(int(port_text), False)
        print(f"[set-enabled] 第二个参数应为 true/false，收到 {value_text!r}")
        return 2

    if a.get_enabled is not None:
        reg = load_registry()
        w = reg['workers'].get(int(a.get_enabled))
        print('unknown' if w is None else ('true' if w.get('enabled', True) else 'false'))
        return 0

    if a.status:
        try:
            with open(STATUS_JSON, encoding='utf-8') as f:
                st = json.load(f)
        except Exception:
            print(f'(无状态文件 {STATUS_JSON}；哨兵未运行过？)')
            return 1
        if a.json:
            print(json.dumps(st, ensure_ascii=False, indent=2))
            return 0
        print(f"worker 池状态（{STATUS_JSON}）更新于 {st.get('updated_utc8')} UTC+8")
        ws = st.get('workers', {})
        ev = st.get('events', {})
        for p in sorted(ws, key=int):
            d = ws[p]
            name = f"{p}({d.get('label','')})" if d.get('label') else p
            if d.get('ok'):
                tt = ('在传 %.2f MB/s' % d['traffic_mbps']) if d.get('traffic') is True \
                    else ('空闲' if d.get('traffic') is False else '流量未知')
                if d.get('traffic_seen_utc8') and d.get('traffic') is not True:
                    tt += f"（最近一次看到流量 {d['traffic_seen_utc8']} UTC+8）"
                print(f"  {name:<18} ✅ {d.get('latency_ms')} ms  {tt}"
                      f"（最近成功 {d.get('last_ok_utc8')}）")
            else:
                since = d.get('down_since') or ''
                print(f"  {name:<18} ❌ {d.get('error','')}（下线自 {since} UTC+8）"
                      f"{'；最近成功 ' + d['last_ok_utc8'] if d.get('last_ok_utc8') else ''}")
            e = ev.get(p)
            if e:
                bits = []
                if e.get('down_last'):
                    bits.append(f"最近下线 {e['down_last']}")
                if e.get('up_last'):
                    bits.append(f"最近上线 {e['up_last']}")
                if bits:
                    print(f"      └ {' | '.join(bits)} UTC+8")
        return 0

    if a.watch is not None:
        watch(interval=max(10, a.watch), timeout=a.timeout)
        return 0

    reg = load_registry()
    res = probe_all(spec=a.range, timeout=a.timeout, reg=reg,
                    include_registry=not a.no_registry, traffic=not a.no_traffic)
    if not a.no_update:
        update_status(res)

    if a.speed:
        for p, d in sorted(res.items()):
            if d.get('ok'):
                r = speed_test(f'http://127.0.0.1:{p}', reg['speed_test_url'], mib=a.speed_mib)
                d['speed'] = r

    if a.ports:
        for p in sorted(p for p, d in res.items() if d.get('ok') is True):
            print(f'http://127.0.0.1:{p}')
        return 0

    if a.json:
        print(json.dumps({
            'registry': WORKERS_JSON,
            'alive': sorted(p for p, d in res.items() if d.get('ok') is True),
            'ports': {str(p): d for p, d in sorted(res.items())},
        }, ensure_ascii=False, indent=2))
        return 0

    alive = [p for p, d in res.items() if d.get('ok') is True]
    print(f'worker 池（注册表 {WORKERS_JSON}）: {len(alive)} 个存活')
    for p, d in sorted(res.items()):
        print(_fmt_row(d))
        if d.get('speed'):
            s = d['speed']
            if s.get('ok'):
                print(f'      └ 测速: {s["mib"]} MiB / {s["seconds"]}s = {s["mib_per_s"]} MiB/s')
            else:
                print(f'      └ 测速失败: {s.get("error")}')
    return 0 if alive else 1


if __name__ == '__main__':
    sys.exit(main())
