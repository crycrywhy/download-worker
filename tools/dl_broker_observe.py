import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dl_lease as L
import dl_config as CFG

STATE_DIR = os.environ.get('DL_STATE_DIR') or os.path.join(HERE, 'state')
BROKER_STATE = os.path.join(STATE_DIR, 'broker_state.json')
BROKER_CFG = os.environ.get('DL_BROKER_CFG') or os.path.join(HERE, 'broker.json')
ALERT_JSONL = CFG.path('paths.alerts_file', 'worker_alerts')
DEFAULT_OWNER = CFG.get('identity.owner', 'agent')

DEFAULT_CFG = CFG.get_map('domain.broker_default_cfg', {
    'v': 1,
    'cap': {},
    'driver_owners': {},
    'external_script_owners': {},
    'contention_alert_after': 2,
})

_CHANNEL_PORTS = tuple(int(c) for c in CFG.get_list('endpoints.channels')
                       if str(c).strip().isdigit())



def now8(fmt='%Y-%m-%d %H:%M:%S'):
    return time.strftime(fmt, time.localtime(time.time() + 8 * 3600))



def load_cfg(path=None):
    # 内置默认**打底**，配置层给了什么再覆盖什么。
    # 不能直接把配置层的返回值当底：map 型配置项如果在 site.json 里写成空 map
    # （`--export-example` 产出的模板正是这么写的），子键会一个都不剩，
    # 于是 'driver_owners' 这类必填子键在这里直接 KeyError。
    # 结论：缺哪个子键就在这儿补齐，别指望配置层凑齐。
    cfg = {'v': 1, 'cap': {}, 'driver_owners': {}, 'external_script_owners': {},
           'contention_alert_after': 2}
    cfg.update(DEFAULT_CFG or {})
    cfg['cap'] = dict(cfg.get('cap') or {})
    cfg['driver_owners'] = dict(cfg.get('driver_owners') or {})
    cfg['external_script_owners'] = dict(cfg.get('external_script_owners') or {})
    try:
        with open(path or BROKER_CFG, encoding='utf-8') as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in ('cap', 'driver_owners', 'external_script_owners') and isinstance(v, dict):
                    cfg[k].update({str(kk): vv for kk, vv in v.items()})
                elif k in DEFAULT_CFG:
                    cfg[k] = v
    except Exception:
        pass
    cfg['cap'] = {int(k): int(v) for k, v in cfg['cap'].items()}
    return cfg


def cap_of(cfg, port):
    return int(cfg.get('cap', {}).get(int(port), 1))



def proc_argv(pid):
    try:
        with open(f'/proc/{int(pid)}/cmdline', 'rb') as f:
            return [a.decode('utf-8', 'replace') for a in f.read().split(b'\x00') if a]
    except Exception:
        return []


def proc_ppid(pid):
    try:
        with open(f'/proc/{int(pid)}/stat', 'rb') as f:
            data = f.read().decode('utf-8', 'replace')
        return int(data[data.rindex(')') + 1:].split()[1])
    except Exception:
        return 0


def ancestry(pid, max_depth=8):
    chain, seen = [], set()
    cur = int(pid)
    for _ in range(max_depth):
        if not cur or cur in seen or cur == 1:
            break
        seen.add(cur)
        chain.append(cur)
        cur = proc_ppid(cur)
    return chain


def cmdline_str(pid):
    argv = proc_argv(pid)
    if not argv:
        return ''
    return ' '.join(argv)[:160]



def _ss_clients(port):
    try:
        out = subprocess.run(['ss', '-tnp'], capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return None
    pats = (f':{int(port)}',)
    pids = []
    for line in out.splitlines():
        tok = line.split()
        if len(tok) < 5:
            continue
        peer = tok[4]
        if not any(peer.endswith(p) for p in pats):
            continue
        for m in re.findall(r'pid=(\d+)', line):
            p = int(m)
            if p not in pids:
                pids.append(p)
    return pids



def driver_snapshots():
    out = {}
    try:
        names = sorted(n for n in os.listdir(STATE_DIR)
                       if n.startswith('driver_') and n.endswith('.json'))
    except OSError:
        return out
    for n in names:
        try:
            with open(os.path.join(STATE_DIR, n), encoding='utf-8') as f:
                snap = json.load(f)
        except Exception:
            continue
        pid = snap.get('pid')
        if pid and L.pid_alive(pid):
            out[int(pid)] = {'name': snap.get('name') or n[7:-5], 'snap': snap}
    return out


def owner_of(pid, cfg, drivers):
    chain = ancestry(pid)
    for p in chain:
        if p in drivers:
            name = drivers[p]['name']
            return cfg['driver_owners'].get(name, name), 'driver', {'driver': name}
    for p in chain:
        argv = proc_argv(p)
        if not argv:
            continue
        for script, owner in (cfg.get('external_script_owners') or {}).items():
            if any(a.endswith(script) for a in argv):
                extra = {'upstream_pid': p, 'upstream': ' '.join(argv)[:120]}
                tag = _arg_value(argv, '--tag')
                if tag:
                    extra['tag'] = tag
                st = _arg_value(argv, '--state')
                if st:
                    extra['state'] = st
                return owner, 'external', extra
    return 'external', 'unknown', {}


def _arg_value(argv, flag):
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + '='):
            return a.split('=', 1)[1]
    return None


def _norm_owner(name, cfg):
    if not name:
        return name
    for script, owner in (cfg.get('external_script_owners') or {}).items():
        if name == script or name == os.path.splitext(script)[0]:
            return owner
    return (cfg.get('driver_owners') or {}).get(name, name)


def describe_client(pid, cfg, drivers):
    argv = proc_argv(pid)
    owner, kind, extra = owner_of(pid, cfg, drivers)
    script = next((os.path.basename(a) for a in argv if a.endswith('.py')), '')
    url = next((a for a in argv if a.startswith('http')), '')
    outfile = _arg_value(argv, '-o') or _arg_value(argv, '--output')
    worker_arg = _arg_value(argv, '--worker')
    return {'pid': pid, 'owner': owner, 'kind': kind,
            'script': script, 'url': url[:160], 'out': outfile,
            'worker_arg': worker_arg, **extra}



def observe(cfg=None, with_traffic=True, ports=None):
    cfg = cfg or load_cfg()
    try:
        import worker_pool as WP
    except Exception:
        WP = None
    drivers = driver_snapshots()

    if ports is None:
        ports = set()
        if WP is not None:
            try:
                ports |= set(int(p) for p in (WP.load_registry().get('workers') or {}))
            except Exception:
                pass
        ports |= set(_CHANNEL_PORTS)
        try:
            for fn in os.listdir(L.LEASE_DIR):
                m = re.match(r'^(\d+)\.', fn)
                if m:
                    ports.add(int(m.group(1)))
        except OSError:
            pass
    ports = sorted(int(p) for p in ports)

    reg = {}
    if WP is not None:
        try:
            reg = (WP.load_registry().get('workers') or {})
        except Exception:
            reg = {}

    traffic = {}
    if with_traffic and WP is not None:
        try:
            traffic = WP.traffic_batch(ports) or {}
        except Exception:
            traffic = {}

    result = {'v': 1, 'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
              'ts_utc8': now8(), 'pid': os.getpid(), 'ports': {}, 'contention': []}

    for port in ports:
        pids = _ss_clients(port)
        clients = [describe_client(p, cfg, drivers) for p in (pids or [])]
        holding, lease_info = L.holder_of(port)
        advs = L.advisories(port)
        obs = {}
        for c in clients:
            obs[c['owner']] = obs.get(c['owner'], 0) + 1
        mine = set(cfg.get('driver_owners', {}).values())
        mine |= {cfg.get('driver_owners', {}).get(d['name'], d['name'])
                 for d in drivers.values()}
        mine.discard(None)
        if not mine:
            mine = {DEFAULT_OWNER}
        external = sum(n for o, n in obs.items() if o not in mine)
        cap = cap_of(cfg, port)
        my = sum(n for o, n in obs.items() if o in mine)
        rec = {
            'label': (reg.get(port) or {}).get('label', ''),
            'device': (reg.get(port) or {}).get('device', ''),
            'enabled': bool((reg.get(port) or {}).get('enabled', True)),
            'cap': cap,
            'ss_ok': pids is not None,
            'observed': obs,
            'clients': clients,
            'my_clients': my,
            'observed_external': external,
            'my_share': max(0, cap - external),
            'traffic_mbps': (traffic.get(port) or {}).get('traffic_mbps'),
            'traffic': (traffic.get(port) or {}).get('traffic'),
            'lease': {'held': holding,
                      'holder': (lease_info or {}).get('holder'),
                      'heartbeat': (lease_info or {}).get('heartbeat'),
                      'stale': bool(lease_info) and L.heartbeat_stale(lease_info)},
            'advisories': [{'holder': a.get('holder'), 'heartbeat': a.get('heartbeat')}
                           for a in advs],
        }
        if external >= cap and external > 0:
            rec['verdict'] = 'oversubscribed'
        elif external > 0:
            rec['verdict'] = 'shared'
        elif my > cap:
            rec['verdict'] = 'my_oversubscribed'
        elif my > 0:
            rec['verdict'] = 'ok'
        else:
            rec['verdict'] = 'idle'
        declared = {_norm_owner((a.get('holder') or {}).get('owner'), cfg) for a in advs}
        if holding:
            declared.add(_norm_owner(((lease_info or {}).get('holder') or {}).get('owner'), cfg))
        declared.discard(None)
        undeclared = sorted(set(obs) - declared - mine)
        if undeclared:
            rec['undeclared'] = undeclared
            if rec['verdict'] in ('oversubscribed', 'shared'):
                rec['verdict'] = 'undeclared_external'
        result['ports'][str(port)] = rec
        if rec['verdict'] in ('oversubscribed', 'undeclared_external', 'my_oversubscribed'):
            result['contention'].append({
                'port': port, 'cap': cap, 'observed': obs,
                'observed_external': external, 'my_share': rec['my_share'],
                'verdict': rec['verdict'],
                'undeclared': rec.get('undeclared', []),
                'clients': [{'pid': c['pid'], 'owner': c['owner'], 'out': c.get('out')}
                            for c in clients],
            })
    return result



def _write_json_atomic(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.bst-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_alert(rec, path=ALERT_JSONL):
    line = json.dumps(rec, ensure_ascii=False)
    if len(line.encode('utf-8')) > 4000:
        rec = dict(rec, truncated=True, detail='')
        line = json.dumps(rec, ensure_ascii=False)[:3900] + '…'
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
        return True
    except OSError:
        return False


def _contention_streak_path():
    return os.path.join(STATE_DIR, 'broker_contention.json')


def alert_contention(state, cfg):
    need = int(cfg.get('contention_alert_after', 2))
    cur = {}
    for c in state.get('contention', []):
        cur[str(c['port'])] = c.get('observed', {})

    prev = {}
    try:
        with open(_contention_streak_path(), encoding='utf-8') as f:
            prev = json.load(f) or {}
    except Exception:
        prev = {}
    streak = prev.get('streak') or {}
    active = prev.get('active') or {}

    fired = []
    new_active = dict(active)
    for port, obs in cur.items():
        streak[port] = int(streak.get(port, 0)) + 1
        if streak[port] >= need and port not in active:
            rec = {'ts_utc8': now8(), 'event': 'CONTENTION', 'port': int(port),
                   'observed': obs, 'detail': prev.get('detail', {}).get(port, '')}
            if record_alert(rec):
                fired.append(rec)
                new_active[port] = obs
    for port in list(streak):
        if port not in cur:
            streak[port] = 0
            if port in new_active:
                rec = {'ts_utc8': now8(), 'event': 'CONTENTION_RESOLVED', 'port': int(port),
                       'detail': '该口已无外部占用'}
                if record_alert(rec):
                    fired.append(rec)
                new_active.pop(port, None)
    detail = {str(c['port']): '；'.join(
        f"{o}×{n}" for o, n in (c.get('observed') or {}).items()) for c in state.get('contention', [])}
    try:
        _write_json_atomic(_contention_streak_path(),
                           {'streak': streak, 'active': new_active, 'detail': detail,
                            'updated': now8()})
    except Exception:
        pass
    return fired



def print_state(state, verbose=False):
    print(f"broker 观测（{state.get('ts_utc8')} UTC+8）")
    for port, r in sorted(state['ports'].items(), key=lambda kv: int(kv[0])):
        v = r['verdict']
        mark = {'ok': '✓', 'idle': '·', 'shared': '◐', 'oversubscribed': '⚠',
                'undeclared_external': '⚠', 'my_oversubscribed': '⚠'}.get(v, '?')
        head = (f"  {mark} {port} {r.get('label') or '':<10} cap={r['cap']} "
                f"观测={r['observed'] or '{}'} 外部={r['observed_external']} 我可分={r['my_share']}")
        if r.get('traffic_mbps') is not None:
            head += f" {r['traffic_mbps']}MB/s"
        head += f"  [{v}]"
        print(head)
        if r['lease']['held']:
            h = r['lease'].get('holder') or {}
            print(f"      租约: 被持有 by {h.get('owner')}/{h.get('name')} pid={h.get('pid')}"
                  + ("（心跳旧）" if r['lease'].get('stale') else ""))
        for a in r.get('advisories') or []:
            h = a.get('holder') or {}
            print(f"      声明: {h.get('owner')}/{h.get('name')} pid={h.get('pid')}")
        if verbose or v.startswith(('oversub', 'undeclared', 'my_')):
            for c in r['clients']:
                print(f"      · pid={c['pid']} owner={c['owner']} ({c['kind']}) "
                      f"{os.path.basename(c.get('out') or '') or c.get('url', '')[:60]}"
                      + (f" ← {c.get('upstream_pid')}" if c.get('upstream_pid') else ''))
    if state.get('contention'):
        print(f"  ⚠ 争用 {len(state['contention'])} 处: "
              + '、'.join(f"{c['port']}(外部{c['observed_external']}/cap{c['cap']})"
                          for c in state['contention']))
    else:
        print("  · 无争用")


def why(port, cfg=None):
    cfg = cfg or load_cfg()
    drivers = driver_snapshots()
    print(f"=== 口 {port} 的证据链（{now8()} UTC+8）===")
    holding, info = L.holder_of(port)
    print(f"租约: {'被持有' if holding else '空闲'}"
          + (f" → {json.dumps(info, ensure_ascii=False)}" if info else ""))
    for a in L.advisories(port):
        print(f"声明: {json.dumps(a, ensure_ascii=False)}")
    pids = _ss_clients(port)
    if pids is None:
        print("ss -tnp 取不到结果（无法观测）")
        return 1
    if not pids:
        print("ss 观测: 没有任何本地客户端连着这个口")
        return 0
    print(f"ss 观测: {len(pids)} 个本地客户端")
    for p in pids:
        d = describe_client(p, cfg, drivers)
        print(f"\n  pid={p}  owner={d['owner']} ({d['kind']})")
        print(f"    脚本: {d.get('script')}   worker 参数: {d.get('worker_arg')}")
        print(f"    输出: {d.get('out')}")
        print(f"    URL : {(d.get('url') or '')[:110]}")
        if d.get('upstream_pid'):
            print(f"    祖先进程 pid={d['upstream_pid']}: {d.get('upstream')}")
        print("    祖先链: " + ' → '.join(f"{x}({os.path.basename((proc_argv(x) or ['?'])[0])})"
                                        for x in ancestry(p)[:6]))
    return 0



def main(argv=None):
    ap = argparse.ArgumentParser(prog='dl_broker_observe.py', description='broker 观测半身（只读）')
    ap.add_argument('--dry-run', action='store_true', help='只打印，不写任何文件/告警')
    ap.add_argument('--json', action='store_true', help='打印 JSON')
    ap.add_argument('--verbose', action='store_true', help='每个口都列客户端明细')
    ap.add_argument('--no-traffic', action='store_true', help='跳过 2s 流量采样')
    ap.add_argument('--why', type=int, metavar='PORT', help='给一个口的完整证据链')
    a = ap.parse_args(argv)

    mp, fs = L.assert_local()
    cfg = load_cfg()
    if a.why is not None:
        return why(a.why, cfg)

    state = observe(cfg, with_traffic=not a.no_traffic)
    state['lease_dir'] = L.LEASE_DIR
    state['lease_fs'] = fs
    if a.json:
        print(json.dumps(state, ensure_ascii=False, indent=1))
    else:
        print_state(state, verbose=a.verbose)

    if a.dry_run:
        print("  （--dry-run：未写 broker_state.json，未发告警）")
        return 0
    _write_json_atomic(BROKER_STATE, state)
    fired = alert_contention(state, cfg)
    if fired:
        print(f"  → 告警流水 +{len(fired)} 条: "
              + '、'.join(f"{f['event']} {f['port']}" for f in fired))
    print(f"  → {BROKER_STATE}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
