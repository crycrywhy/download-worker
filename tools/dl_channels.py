import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dl_config as CFG
STATE_DIR = os.environ.get('DL_STATE_DIR', os.path.join(HERE, 'state'))
CACHE = os.path.join(STATE_DIR, 'dl_channels_cache.json')
CHANNELS = tuple(CFG.get_list('endpoints.channels'))
DEFAULT_OWNER = CFG.get('identity.owner', 'agent')
SPEED_MIN_SPAN = 20.0
SPEED_KEEP = 8
SPEED_MAX_AGE = 900.0


def _argv(pid):
    try:
        with open('/proc/%s/cmdline' % pid, 'rb') as f:
            return f.read().decode('utf-8', 'replace').split('\0')
    except Exception:
        return []


def _ppid(pid):
    try:
        with open('/proc/%s/stat' % pid, encoding='utf-8') as f:
            return int(f.read().rsplit(')', 1)[1].split()[1])
    except Exception:
        return 0


def _ancestry(pid, limit=8):
    chain, cur = [], int(pid)
    while cur > 1 and len(chain) < limit:
        chain.append(cur)
        cur = _ppid(cur)
    return chain


def _driver_owner():
    try:
        import glob
        for p in glob.glob(os.path.join(STATE_DIR, 'driver_*.json')):
            with open(p, encoding='utf-8') as f:
                d = json.load(f)
            if d.get('owner'):
                return d['owner']
    except Exception:
        pass
    return DEFAULT_OWNER


def _owner_of(pid):
    for p in _ancestry(pid):
        argv = _argv(p)
        if not argv:
            continue
        joined = ' '.join(argv)
        if any(a.endswith('dl_all_priority.py') for a in argv):
            return _driver_owner()
        if any(a.endswith('dl_genomes.py') for a in argv):
            return 'K3'
    return 'external'


def _arg_value(argv, flag):
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + '='):
            return a.split('=', 1)[1]
    return None


def _channel_of(argv):
    w = _arg_value(argv, '--worker') or ''
    for port in CHANNELS[1:]:
        if w.endswith(':' + port):
            return port
    return 'linux'


def _progress(out):
    side = out + '.download.json'
    try:
        with open(side, encoding='utf-8') as f:
            d = json.load(f)
        total = int(d.get('size') or 0)
        if d.get('durable_bytes') is not None:
            return int(d['durable_bytes']), total, 'sidecar/durable'
        cc, cs = d.get('completed_chunks'), int(d.get('chunk_size') or 0)
        if isinstance(cc, list) and cs:
            done = min(len(cc) * cs, total) if total else len(cc) * cs
            return done, total, 'sidecar/chunks'
        return 0, total, 'sidecar/empty'
    except Exception:
        pass
    try:
        return os.path.getsize(out), 0, 'file'
    except Exception:
        return 0, 0, 'none'


def _load_cache():
    try:
        with open(CACHE, encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d.get('samples'), dict):
            return d['samples']
    except Exception:
        pass
    return {}


def _save_cache(samples):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = CACHE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'v': 1, 'saved': time.time(), 'samples': samples}, f)
        os.replace(tmp, CACHE)
    except Exception:
        pass


def _speed_mib(key, done, samples, now):
    hist = [s for s in (samples.get(key) or [])
            if isinstance(s, list) and len(s) == 2 and now - s[0] <= SPEED_MAX_AGE]
    hist.append([now, done])
    hist = hist[-SPEED_KEEP:]
    samples[key] = hist
    t0, b0 = hist[0]
    span = now - t0
    if span < SPEED_MIN_SPAN:
        return None
    if done < b0:
        samples[key] = [[now, done]]
        return None
    return (done - b0) / span / 1048576.0


def scan(now=None):
    now = now or time.time()
    samples = _load_cache()
    out = {c: [] for c in CHANNELS}
    for pid in sorted(os.listdir('/proc')):
        if not pid.isdigit():
            continue
        argv = _argv(pid)
        if not any(a.endswith('linux_downloader.py') for a in argv):
            continue
        out_path = _arg_value(argv, '-o') or ''
        if not out_path:
            continue
        ch = _channel_of(argv)
        done, total, src = _progress(out_path)
        spd = _speed_mib(out_path, done, samples, now)
        out.setdefault(ch, []).append({
            'pid': int(pid),
            'owner': _owner_of(pid),
            'task': os.path.basename(out_path),
            'out': out_path,
            'done_bytes': done,
            'total_bytes': total,
            'pct': round(done * 100.0 / total, 1) if total else None,
            'speed_mib': round(spd, 2) if spd is not None else None,
            'progress_src': src,
        })
    for ch in out:
        out[ch].sort(key=lambda e: e['pid'])
    live = {e['out'] for lst in out.values() for e in lst}
    for k in list(samples):
        if k not in live:
            samples.pop(k, None)
    _save_cache(samples)
    return out


def _gib(n):
    return '%.2f GiB' % (n / 1073741824.0) if n else '?'


def render(chans, when=None):
    when = when or time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + 8 * 3600))
    out = ['通道实况（%s UTC+8）' % when]
    for ch in CHANNELS:
        lines = chans.get(ch) or []
        tag = 'linux 直连' if ch == 'linux' else ('PC %s' % ch)
        if not lines:
            out.append('  %-6s %-10s 空闲（无人使用）' % (ch, tag))
            continue
        for e in lines:
            done_s = _gib(e['done_bytes']) if e['done_bytes'] else '0 B'
            prog = ('%s / %s  %s%%' % (done_s, _gib(e['total_bytes']), e['pct'])
                    if e['total_bytes'] else done_s)
            spd = ('%.2f MiB/s' % e['speed_mib']) if e['speed_mib'] is not None else '速率待测'
            out.append('  %-6s %-10s %-8s %-42s %-26s %s'
                       % (ch, tag, e['owner'], e['task'][:42], prog, spd))
    return '\n'.join(out)


def main():
    as_json = '--json' in sys.argv[1:]
    chans = scan()
    text = render(chans)
    if as_json:
        print(json.dumps({'channels': chans, 'text': text,
                          'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                          'ts_utc8': time.strftime('%Y-%m-%d %H:%M:%S',
                                                   time.localtime(time.time() + 8 * 3600))},
                         ensure_ascii=False, indent=1))
        return 0
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
