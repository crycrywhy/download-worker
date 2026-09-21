import errno
import fcntl
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
LEASE_DIR = os.environ.get('DL_LEASE_DIR') or os.path.join(HERE, 'lease')

import dl_config as CFG

DEFAULT_TTL_S = 120
_LOCK_SUFFIX = '.lock'
_JSON_SUFFIX = '.json'
_ADV_SUFFIX = '.advisory'



def lock_path(port):
    return os.path.join(LEASE_DIR, f'{int(port)}{_LOCK_SUFFIX}')


def json_path(port):
    return os.path.join(LEASE_DIR, f'{int(port)}{_JSON_SUFFIX}')


def advisory_path(port, pid=None):
    return os.path.join(LEASE_DIR, f'{int(port)}{_ADV_SUFFIX}.{pid or os.getpid()}.json')



_NET_FS = ('nfs', 'nfs4', 'cifs', 'smb', 'smbfs', 'fuse.sshfs', 'fuseblk.sshfs')


def mount_of(path):
    real = os.path.realpath(path)
    best = ('/', 'unknown')
    try:
        with open('/proc/mounts') as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp, fs = parts[1].replace('\\040', ' '), parts[2]
                if (real == mp or real.startswith(mp.rstrip('/') + '/')) and len(mp) >= len(best[0]):
                    best = (mp, fs)
    except OSError:
        pass
    return best


def assert_local(path=None):
    p = path if path is not None else LEASE_DIR
    mp, fs = mount_of(p)
    if fs.lower().startswith(_NET_FS):
        raise RuntimeError(
            f"[dl_lease] 拒绝使用网络文件系统存放租约：{p} 在 {mp}（fs={fs}）。"
            f"flock 在网络盘上不可靠，租约会失效。请把 DL_LEASE_DIR 指到本地盘。")
    return mp, fs



def _read_json(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.lease-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
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


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise


def _open_lock(port):
    os.makedirs(LEASE_DIR, exist_ok=True)
    return os.open(lock_path(port), os.O_RDWR | os.O_CREAT, 0o664)


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S')



def acquire(port, holder, timeout=0.0, ttl_s=DEFAULT_TTL_S, task=None):
    port = int(port)
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        fd = _open_lock(port)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise
            if time.time() >= deadline:
                return None, _read_json(json_path(port))
            time.sleep(0.2)
            continue
        h = {'fd': fd, 'port': port, 'holder': dict(holder or {}), 'acquired': _now(), 'ttl_s': ttl_s}
        rec = {'v': 1, 'port': port, 'state': 'held',
               'holder': dict(holder or {}),
               'declared_at': h['acquired'], 'heartbeat': h['acquired'],
               'ttl_s': ttl_s, 'task': task or None}
        try:
            _write_json(json_path(port), rec)
        except Exception:
            pass
        return h, None


def renew(handle, task=None):
    if not handle or handle.get('fd') is None:
        return False
    port = handle['port']
    try:
        rec = _read_json(json_path(port)) or {}
        rec.update({'v': 1, 'port': port, 'state': 'held',
                    'holder': handle.get('holder') or {}, 'heartbeat': _now(),
                    'ttl_s': handle.get('ttl_s', DEFAULT_TTL_S)})
        if task is not None:
            rec['task'] = task
        rec.setdefault('declared_at', handle.get('acquired') or _now())
        _write_json(json_path(port), rec)
        return True
    except Exception:
        return False


def release(handle):
    if not handle or handle.get('fd') is None:
        return
    port = handle['port']
    fd = handle['fd']
    try:
        _unlink_quiet(json_path(port))
    except Exception:
        pass
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass
    handle['fd'] = None


def holder_of(port):
    port = int(port)
    fd = _open_lock(port)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise
            return True, _read_json(json_path(port))
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False, _read_json(json_path(port))
    finally:
        os.close(fd)


def gc(ports=None):
    cleaned = []
    if ports is None:
        ports = []
        try:
            for fn in os.listdir(LEASE_DIR):
                if fn.endswith(_JSON_SUFFIX):
                    try:
                        ports.append(int(fn[:-len(_JSON_SUFFIX)]))
                    except ValueError:
                        continue
        except OSError:
            return cleaned
    for port in ports:
        holding, _info = holder_of(port)
        if holding:
            continue
        try:
            _unlink_quiet(json_path(port))
            cleaned.append(int(port))
        except Exception:
            pass
    return cleaned



def declare(port, holder, task=None, ttl_s=DEFAULT_TTL_S):
    try:
        os.makedirs(LEASE_DIR, exist_ok=True)
        _write_json(advisory_path(port, holder.get('pid') if holder else None),
                    {'v': 1, 'port': int(port), 'state': 'advisory',
                     'holder': dict(holder or {}), 'heartbeat': _now(),
                     'ttl_s': ttl_s, 'task': task or None})
        return True
    except Exception:
        return False


def drop_declare(port, pid=None):
    try:
        _unlink_quiet(advisory_path(port, pid))
        return True
    except Exception:
        return False


def advisories(port=None, stale_after=None):
    out = []
    try:
        names = os.listdir(LEASE_DIR)
    except OSError:
        return out
    for fn in names:
        if _ADV_SUFFIX not in fn or not fn.endswith('.json'):
            continue
        try:
            p = int(fn.split(_ADV_SUFFIX, 1)[0])
        except ValueError:
            continue
        if port is not None and p != int(port):
            continue
        rec = _read_json(os.path.join(LEASE_DIR, fn))
        if not rec:
            continue
        h = rec.get('holder') or {}
        pid = h.get('pid')
        if pid and not pid_alive(pid):
            _unlink_quiet(os.path.join(LEASE_DIR, fn))
            continue
        if stale_after is not None and _age_s(rec.get('heartbeat')) > stale_after:
            continue
        rec['_file'] = fn
        out.append(rec)
    return out



def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _age_s(ts):
    try:
        return time.time() - time.mktime(time.strptime(ts, '%Y-%m-%d %H:%M:%S'))
    except Exception:
        return -1


def heartbeat_stale(rec, ttl_s=None):
    ttl = ttl_s if ttl_s is not None else (rec or {}).get('ttl_s') or DEFAULT_TTL_S
    age = _age_s((rec or {}).get('heartbeat'))
    return age < 0 or age > ttl


if __name__ == '__main__':
    mp, fs = assert_local()
    print(f"lease 目录: {LEASE_DIR}（mount={mp} fs={fs}）")
    _ports = [int(c) for c in CFG.get_list('endpoints.channels') if str(c).strip().isdigit()]
    print(f"lock 路径示例: {lock_path(_ports[0])}" if _ports
          else "lock 路径示例: （endpoints.channels 未配置端口）")
    for p in _ports:
        holding, info = holder_of(p)
        if holding:
            h = (info or {}).get('holder') or {}
            print(f"  {p}: 被持有 → {h.get('owner') or '?'}/{h.get('name') or '?'} "
                  f"pid={h.get('pid')} 心跳={((info or {}).get('heartbeat'))} "
                  f"{'（旧）' if heartbeat_stale(info) else ''}")
        else:
            print(f"  {p}: 空闲（无持有者）")
    advs = advisories()
    if advs:
        print("advisory 声明:")
        for a in advs:
            h = a.get('holder') or {}
            print(f"  {a['port']}: {h.get('owner')}/{h.get('name')} pid={h.get('pid')}")
