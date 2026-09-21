import argparse
import fcntl
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.environ.get('DL_TRUST_LEDGER', os.path.join(HERE, 'state', 'disk_trust_log.tsv'))

VERDICTS = ('trusted', 'bad', 'unknown', 'absent')
EXIT = {'trusted': 0, 'bad': 1, 'unknown': 2, 'absent': 3}


def utc8(ts=None):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime((ts or time.time()) + 8 * 3600))


def human(n):
    try:
        n = float(n)
    except Exception:
        return str(n)
    for u, s in (('TiB', 2 ** 40), ('GiB', 2 ** 30), ('MiB', 2 ** 20), ('KiB', 2 ** 10)):
        if n >= s:
            return f'{n / s:.2f} {u}'
    return f'{int(n)} B'


def remote_size(url, timeout=30):
    if not url:
        return None
    try:
        r = subprocess.run(['curl', '-sIL', '--max-time', str(timeout), url],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout:
            cl = None
            for line in r.stdout.splitlines():
                if line.lower().startswith('content-length:'):
                    try:
                        cl = int(line.split(':', 1)[1].strip())
                    except ValueError:
                        pass
            if cl is not None:
                return cl
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, method='HEAD',
                                     headers={'User-Agent': 'download-worker/disk_trust'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            cl = resp.headers.get('Content-Length')
            return int(cl) if cl is not None else None
    except Exception:
        return None


def judge_existing(path, url=None, timeout=30, size_fn=None):
    try:
        local = os.path.getsize(path)
    except OSError as e:
        return ('absent', f'本地取不到（{type(e).__name__}）', None, None)
    fn = size_fn or remote_size
    remote = None
    if url:
        try:
            remote = fn(url, timeout=timeout)
        except Exception:
            remote = None
    if remote is None:
        return ('unknown', f'远端大小取不到（本地 {human(local)}）', local, None)
    if int(remote) == int(local):
        return ('trusted', f'大小与远端一致（{int(local)} 字节）', local, int(remote))
    return ('bad', f'本地 {human(local)} ≠ 远端 {human(remote)}', local, int(remote))


def append_ledger(verdict, path, detail='', url='', who='', local=None, remote=None):
    row = [utc8(), verdict, who, path, '' if local is None else str(local),
           '' if remote is None else str(remote), detail, url]
    line = '\t'.join(str(x).replace('\t', ' ').replace('\n', ' ') for x in row)
    if len(line.encode('utf-8', 'replace')) > 3800:
        line = line[:1200] + '…(截断)'
    try:
        d = os.path.dirname(LEDGER)
        if d:
            os.makedirs(d, exist_ok=True)
        fd = os.open(LEDGER, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, (line + '\n').encode('utf-8', 'replace'))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    except Exception:
        pass


def check(path, url=None, who='', timeout=30, size_fn=None, quiet=False, log=True):
    v, detail, local, remote = judge_existing(path, url, timeout=timeout, size_fn=size_fn)
    if log and v in ('trusted', 'bad', 'unknown'):
        append_ledger(v, path, detail, url=url or '', who=who, local=local, remote=remote)
    if not quiet:
        print(f'{v}\t{detail}\t{path}', flush=True)
    return v, detail, local, remote


def main():
    ap = argparse.ArgumentParser(description='盘上文件可信度判据（大小 vs 远端）')
    ap.add_argument('path')
    ap.add_argument('url', nargs='?', default=None)
    ap.add_argument('--who', default='cli')
    ap.add_argument('--timeout', type=int, default=30)
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--ledger', help='覆盖台账路径（默认 %s）' % LEDGER)
    a = ap.parse_args()
    if a.ledger:
        globals()['LEDGER'] = a.ledger
    v, detail, _l, _r = check(a.path, a.url, who=a.who, timeout=a.timeout, quiet=a.quiet)
    return EXIT.get(v, 2)


if __name__ == '__main__':
    sys.exit(main())
