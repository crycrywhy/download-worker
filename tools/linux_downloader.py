import argparse
import atexit
import hashlib
import http.client
import json
import os
import queue
import re
import sys
import threading
import time
from urllib.parse import quote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import stream_verify as sv
except Exception:
    sv = None

import dl_config as CFG
DEFAULT_WORKER = CFG.get('endpoints.default_worker', '')

MIB = 1024 * 1024
ZERO_RUN_LIMIT = 64 * 1024
FLUSH_CHUNK = 8 * MIB
PUNCH_MIN = 32 * MIB
STALL_SECS = 600.0
HARD_STALL_SECS = 3600.0

def _infer_client():
    try:
        with open("/proc/%d/cmdline" % os.getppid(), "rb") as f:
            argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\x00") if a]
        for a in argv:
            b = os.path.basename(a)
            if b.endswith(".py"):
                return b[:-3]
    except Exception:
        pass
    return ""

_CLIENT = os.environ.get("DL_CLIENT", "") or _infer_client()
_TASK = ""


def _advisory_declare(worker_url, url, out):
    try:
        import dl_lease as L
        port = int(urlparse(worker_url).port or 0)
        if not port:
            return None
        ok = L.declare(port,
                       {"pid": os.getpid(), "ppid": os.getppid(),
                        "owner": os.environ.get("DL_OWNER") or _CLIENT or "unknown",
                        "client": _CLIENT, "host": os.uname()[1]},
                       task={"output": out, "url": str(url)[:300]})
        if not ok:
            return None
        atexit.register(lambda: L.drop_declare(port, os.getpid()))
        return port
    except Exception:
        return None


def now():
    return time.strftime("%H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def parse_size(s):
    m = re.fullmatch(r"(\d+)([KkMmGg]?)[Bb]?", s.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"非法大小: {s!r}(示例: 256M / 1G / 512K)")
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2).lower()]
    return int(m.group(1)) * mult


class RangeIgnored(Exception):
    pass


class StreamFatal(Exception):
    pass


def _proxy_for(scheme, host):
    envnames = ("https_proxy", "HTTPS_PROXY") if scheme == "https" else ("http_proxy", "HTTP_PROXY")
    proxy = next((os.environ[n] for n in envnames if os.environ.get(n)), None)
    if not proxy:
        return None
    np = (os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or "")
    for ent in (e.strip().lstrip(".").lower() for e in np.split(",")):
        if ent and (host == ent or host.endswith("." + ent)):
            return None
    return proxy


class Source:

    def __init__(self, worker=None, direct=False, timeout=300):
        self.worker = worker.rstrip("/") if worker else None
        self.direct = direct
        self.timeout = timeout

    def _open(self, url, range_header=None):
        if self.direct:
            u = urlparse(url)
            path = u.path + (f"?{u.query}" if u.query else "")
            proxy = _proxy_for(u.scheme, u.hostname)
            if proxy:
                pu = urlparse(proxy if "://" in proxy else "http://" + proxy)
                if pu.scheme == "https":
                    raise RuntimeError(f"暂不支持 https:// 形式的代理: {proxy!r}（请给 http:// 代理口）")
                conn = http.client.HTTPConnection(pu.hostname, pu.port or 80, timeout=self.timeout)
                if u.scheme == "https":
                    conn = http.client.HTTPSConnection(pu.hostname, pu.port or 80, timeout=self.timeout)
                    conn.set_tunnel(u.hostname, u.port or 443)
                else:
                    path = url
            else:
                conn = (http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=self.timeout)
                        if u.scheme == "https"
                        else http.client.HTTPConnection(u.hostname, u.port or 80, timeout=self.timeout))
        else:
            w = urlparse(self.worker)
            conn = (http.client.HTTPSConnection(w.hostname, w.port or 443, timeout=self.timeout)
                    if w.scheme == "https"
                    else http.client.HTTPConnection(w.hostname, w.port or 80, timeout=self.timeout))
            path = "/stream?url=" + quote(url, safe="")
        headers = {"Range": range_header} if range_header else {}
        if _CLIENT:
            headers["X-Client"] = _CLIENT
        if _TASK:
            headers["X-Task"] = _TASK
        conn.request("GET", path, headers=headers)
        return conn, conn.getresponse()

    def probe(self, url):
        conn, r = self._open(url, "bytes=0-0")
        try:
            if r.status == 206:
                cr = r.headers.get("Content-Range", "")
                m = re.match(r"bytes 0-0/(\d+)", cr)
                r.read()
                return (int(m.group(1)) if m else None), True
            if r.status == 200:
                cl = r.headers.get("Content-Length")
                return (int(cl) if cl else None), False
            raise RuntimeError(f"探测返回 {r.status}: {r.read(300)[:200]!r}")
        finally:
            conn.close()

    def fetch_range(self, url, off, size):
        rng = f"bytes={off}-{off + size - 1}"
        conn, r = self._open(url, rng)
        if r.status == 200:
            conn.close()
            raise RangeIgnored(f"Range({rng}) 返回 200 —— 上游/Worker 忽略 Range, 按失败处理")
        if r.status != 206:
            body = r.read(300)
            conn.close()
            raise RuntimeError(f"Range({rng}) 返回 {r.status}(期望 206): {body[:200]!r}")
        cr = r.headers.get("Content-Range", "")
        m = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", cr)
        if not m or int(m.group(1)) != off or int(m.group(2)) != off + size - 1:
            conn.close()
            raise RuntimeError(f"Content-Range 不符: {cr!r}(期望 bytes {off}-{off + size - 1}/...)")
        return conn, r

    def fetch_full(self, url):
        conn, r = self._open(url)
        if r.status != 200:
            body = r.read(300)
            conn.close()
            raise RuntimeError(f"GET 返回 {r.status}(期望 200): {body[:200]!r}")
        return conn, r


def save_sidecar(path, meta):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f)
    os.replace(tmp, path)


class StreamSink:

    def __init__(self, final, window, total, chunk_size, n_chunks, meta, sidecar, lock,
                 window_max, sync_bytes, sync_secs, punch_mode="auto", use_checker=True,
                 resume_from=0, expect_trailer=None, expect_md5=None):
        self.final = final
        self.partial = final + ".partial"
        self.window_path = window
        self.total = total
        self.chunk_size = chunk_size
        self.n_chunks = n_chunks
        self.meta = meta
        self.sidecar = sidecar
        self.lock = lock
        self.window_max = window_max
        self.sync_bytes = sync_bytes
        self.sync_secs = sync_secs
        self.punch_mode = punch_mode
        self.expect_md5 = expect_md5
        self.expect_trailer = (final.endswith(".bin") if expect_trailer is None
                                else expect_trailer)
        self.base = int(resume_from)
        self.written = {}
        self._lo = min(self.base // chunk_size, n_chunks)
        self.cov_hi = self.base
        self.flush_hi = self.base
        self.sync_hi = self.base
        self.punch_hi = self.base
        self.recv_total = self.base
        self.seq_mode = False
        self.seq_written = self.base
        self.stop = False
        self.error = None
        self.verify_error = None
        self.throttled = 0
        self.last_sync = time.time()
        self.cond = threading.Condition()
        exp_gzip = None if self.base == 0 else meta.get("gzip")
        self.verifier = sv.GzipStreamVerifier(base_offset=self.base, expect_gzip=exp_gzip) if sv else None
        self.zero = sv.ZeroRunScanner(base_offset=self.base) if sv else None
        self.md5 = hashlib.md5()
        self.checker = sv.StreamCheck() if (sv and use_checker) else None
        if self.base > 0 and self.checker:
            self.checker.proc.kill()
            self.checker = None
        if punch_mode == "none":
            self.window_max = float("inf")
            log("[stream] ⚠️ --punch none：不打洞、不背压，SSD 窗口会一直涨到文件下完（仅调试用）")
        os.makedirs(os.path.dirname(self.window_path) or ".", exist_ok=True)
        self.wfd = os.open(self.window_path, os.O_RDWR | os.O_CREAT, 0o644)
        os.ftruncate(self.wfd, 0)
        self.nfd = os.open(self.partial, os.O_RDWR | os.O_CREAT, 0o644)
        os.ftruncate(self.nfd, self.base)
        self.done = False
        self.thread = threading.Thread(target=self._flush_loop, daemon=True, name="stream-flush")
        self.thread.start()
        self.syncer = threading.Thread(target=self._sync_loop, daemon=True, name="stream-sync")
        self.syncer.start()
        if sv:
            log(f"[stream] 窗口 {self.window_path}（上限 {human(window_max)}）→ {self.partial}"
                + (f"，续传起点 {human(self.base)}" if self.base else ""))

    def note_block(self, ci, end_off, delta):
        with self.cond:
            new = end_off - ci * self.chunk_size
            old = self.written.get(ci, 0)
            if new > old:
                self.written[ci] = new
                self.recv_total += (new - old)
            if self.seq_mode:
                self.seq_written = end_off
                self.cov_hi = end_off
            else:
                lo = self.lowest_open(advance=True)
                self.cov_hi = self.total if lo is None else max(
                    self.base, lo * self.chunk_size + self.written.get(lo, 0))
            self.cond.notify_all()

    def lowest_open(self, advance=False):
        if advance:
            while (self._lo < self.n_chunks
                   and self.written.get(self._lo, 0) >= self._chunk_len(self._lo)):
                self._lo += 1
        return self._lo if self._lo < self.n_chunks else None

    def _chunk_len(self, ci):
        return min(self.chunk_size, self.total - ci * self.chunk_size)

    def note_seq(self, end_off, delta):
        with self.cond:
            self.seq_mode = True
            prev = self.seq_written
            if end_off > prev:
                self.recv_total += end_off - prev
            self.seq_written = max(prev, end_off)
            self.cov_hi = max(self.cov_hi, end_off)
            self.cond.notify_all()

    def throttle(self):
        with self.cond:
            over = (self.recv_total - self.punch_hi) > self.window_max
        if over and not self.stop and self.error is None:
            try:
                self._punch(force=True)
            except StreamFatal as e:
                with self.cond:
                    self.error = str(e)
                    self.stop = True
                    self.cond.notify_all()
        with self.cond:
            t0 = time.time()
            punch_seen = self.punch_hi
            last_try = time.time()
            while not self.stop and self.error is None \
                    and (self.recv_total - self.punch_hi) > self.window_max:
                self.throttled += 1
                if self.throttled == 1 or self.throttled % 20 == 0:
                    log(f"[stream] NFS 背压：窗口在途 "
                        f"{human(self.recv_total - self.punch_hi)} > {human(self.window_max)}"
                        f"（已等待 {self.throttled} 次；NFS 写慢于下载，SSD 先兜着）")
                if self.punch_hi > punch_seen:
                    punch_seen = self.punch_hi
                    t0 = time.time()
                elif time.time() - last_try >= 10.0:
                    last_try = time.time()
                    try:
                        self._punch(force=True)
                    except StreamFatal as e:
                        with self.cond:
                            self.error = str(e)
                            self.stop = True
                            self.cond.notify_all()
                else:
                    gate = self.verifier.verified_hi if self.verifier else self.sync_hi
                    gap = self.recv_total - self.punch_hi
                    if gate - self.punch_hi >= PUNCH_MIN and time.time() - t0 > STALL_SECS:
                        raise StreamFatal(
                            f"背压自锁：窗口在途 {human(gap)} > {human(self.window_max)}，"
                            f"且 {STALL_SECS:.0f}s 内打洞零推进"
                            f"（可打区间还有 {human(gate - self.punch_hi)}）")
                    if time.time() - t0 > HARD_STALL_SECS:
                        raise StreamFatal(
                            f"背压停滞：窗口在途 {human(gap)} > {human(self.window_max)}，"
                            f"且 {HARD_STALL_SECS / 3600:.0f} 小时无任何推进"
                            f"（NFS 侧 flush 停在 {human(self.flush_hi)}）")
                self.cond.wait(1.0)
            if self.error:
                raise StreamFatal(self.error)

    def _punch(self, force=False):
        with self.cond:
            gate = self.verifier.verified_hi if self.verifier else self.sync_hi
            if not force and gate - self.punch_hi < PUNCH_MIN:
                return
        if gate - self.punch_hi < 4096:
            return
        try:
            n = sv.punch_hole(self.window_path, self.punch_hi, gate - self.punch_hi,
                              self.punch_mode)
        except Exception as e:
            raise StreamFatal(f"打洞失败（{self.punch_mode}）: {e!r}")
        if n:
            with self.cond:
                self.punch_hi += n
                self.cond.notify_all()

    def _sync_once(self, force=False):
        with self.cond:
            hi = self.flush_hi
            if not force and hi - self.sync_hi < self.sync_bytes \
                    and time.time() - self.last_sync < self.sync_secs:
                return False
        os.fsync(self.nfd)
        with self.cond:
            self.sync_hi = hi
            self.last_sync = time.time()
            self.meta["durable_bytes"] = hi
            if self.verifier:
                self.meta["resume_at"] = min(hi, self.verifier.resume_hi)
                self.meta["gzip"] = self.verifier.is_gzip
            else:
                self.meta["resume_at"] = hi
        with self.lock:
            if not self.done:
                save_sidecar(self.sidecar, self.meta)
        return True

    def _sync_loop(self):
        while True:
            with self.cond:
                if self.stop or self.done:
                    return
            try:
                self._sync_once()
            except Exception as e:
                self.error = f"fsync 失败: {e!r}"
                with self.cond:
                    self.stop = True
                    self.cond.notify_all()
                return
            time.sleep(2.0)

    def _flush_loop(self):
        try:
            while True:
                with self.cond:
                    while self.cov_hi <= self.flush_hi and not self.stop:
                        self.cond.wait(0.5)
                    if self.cov_hi <= self.flush_hi and self.stop:
                        return
                    lo, hi = self.flush_hi, self.cov_hi
                self._flush_range(lo, hi)
        except StreamFatal as e:
            self.error = str(e)
            with self.cond:
                self.stop = True
                self.cond.notify_all()
        except Exception as e:
            self.error = f"flush 失败: {e!r}"
            with self.cond:
                self.stop = True
                self.cond.notify_all()

    def _flush_range(self, lo, hi):
        off = lo
        while off < hi:
            n = min(FLUSH_CHUNK, hi - off)
            b = os.pread(self.wfd, n, off)
            if len(b) != n:
                raise RuntimeError(f"窗口短读 {len(b)} != {n} @{off}")
            os.pwrite(self.nfd, b, off)
            if self.verifier:
                self.verifier.feed(b)
                self.zero.feed(b)
                self.md5.update(b)
                if self.verifier.error:
                    self.verify_error = self.verifier.error
                    raise StreamFatal(f"流式校验失败: {self.verifier.error}")
            if self.checker:
                self.checker.feed(b)
            off += n
            with self.cond:
                self.flush_hi = off
                self.cond.notify_all()
            self._punch()

    def finish(self):
        with self.cond:
            self.stop = True
            self.cond.notify_all()
        self.thread.join(timeout=3600)
        if self.thread.is_alive():
            raise RuntimeError("flush 线程未能在 1 h 内排空")
        if self.verify_error:
            return [self.verify_error]
        if self.error:
            raise RuntimeError(self.error)
        os.fsync(self.nfd)
        with self.cond:
            self.sync_hi = self.flush_hi = self.cov_hi
            self.meta["durable_bytes"] = self.total
            with self.lock:
                save_sidecar(self.sidecar, self.meta)
        errs = []
        if self.verifier and self.base >= self.total and self.total > 0:
            tail = self._read_tail(4096)
            if self.expect_trailer and sv.BGZF_EOF_SIG not in tail:
                errs.append("尾部缺 gzip container EOF 块（上轮校验通过、但尾部签名不符）")
            else:
                log(f"[stream] 续传点在文件末尾（{human(self.total)}）：本轮无新字节，"
                    f"完整性继承上一轮的 member 校验；已补尾部签名检查")
        elif self.verifier:
            e = self.verifier.finish(expect_trailer=self.expect_trailer)
            if e:
                errs.append(f"gzip/gzip container: {e}")
            holes = self.zero.finish()
            if holes:
                errs.append(f"卡洞: {len(holes)} 处 >=64KiB 零串，首处偏移 {holes[0]}")
            if self.verifier.fed < self.total:
                errs.append(f"覆盖不足: 只验了 {self.verifier.fed} / {self.total}")
        if self.checker:
            e = self.checker.finish()
            if e:
                errs.append(f"stream_checker: {e}")
        if self.expect_md5 and self.base == 0:
            got = self.md5.hexdigest()
            if got != self.expect_md5.lower():
                errs.append(f"md5: {got} != 官方 {self.expect_md5}")
            else:
                log(f"[stream] md5 一致（增量，未回读）")
        elif self.expect_md5:
            log(f"[stream] 续传（起点 {human(self.base)}）：增量 md5 不含前缀，跳过比对；"
                f"外部校验链会复核最终文件")
        if errs:
            return errs
        if not os.path.exists(self.partial):
            raise RuntimeError(f"落位失败：{self.partial} 不见了")
        os.replace(self.partial, self.final)
        with self.lock:
            self.done = True
            if os.path.exists(self.sidecar):
                os.unlink(self.sidecar)
        self._cleanup_window()
        return []

    def _read_tail(self, n):
        try:
            sz = os.fstat(self.nfd).st_size
            return os.pread(self.nfd, min(n, sz), max(0, sz - n))
        except OSError:
            return b""

    def _cleanup_window(self):
        for fd in (self.wfd, self.nfd):
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(self.window_path):
            try:
                os.unlink(self.window_path)
            except OSError:
                pass
        try:
            os.rmdir(os.path.dirname(self.window_path))
        except OSError:
            pass

    def abort(self, join_timeout=5.0):
        with self.cond:
            self.stop = True
            self.cond.notify_all()
        with self.lock:
            self.done = True
        try:
            self.thread.join(timeout=join_timeout)
        except Exception:
            pass
        try:
            self.syncer.join(timeout=join_timeout)
        except Exception:
            pass
        try:
            if self.checker:
                self.checker.proc.kill()
        except Exception:
            pass
        self._cleanup_window()

    def quarantine(self, reason):
        with self.lock:
            self.done = True
        self._cleanup_window()
        tag = f"{self.partial}.corrupt-{time.strftime('%Y%m%d%H%M%S')}"
        try:
            if os.path.exists(self.partial):
                os.replace(self.partial, tag)
        except OSError:
            tag = self.partial
        for f in (self.sidecar,):
            if os.path.exists(f):
                try:
                    os.unlink(f)
                except OSError:
                    pass
        return f"{tag}（{reason}）"


def zero_scan(path):
    holes = []
    run = 0
    run_start = 0
    pos = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(MIB)
            if not b:
                break
            for i, byte in enumerate(b):
                if byte == 0:
                    if run == 0:
                        run_start = pos + i
                    run += 1
                else:
                    if run >= ZERO_RUN_LIMIT:
                        holes.append(run_start)
                    run = 0
            pos += len(b)
    if run >= ZERO_RUN_LIMIT:
        holes.append(run_start)
    return holes


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(MIB)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def stream_to_fd(r, fd, off, expected, label, sink=None, ci=None):
    got = 0
    while True:
        if sink is not None and sink.error:
            raise StreamFatal(sink.error)
        b = r.read(MIB)
        if not b:
            break
        os.pwrite(fd, b, off + got)
        got += len(b)
        if sink is not None:
            sink.note_block(ci, off + got, len(b))
    if got != expected:
        raise RuntimeError(f"{label} 实收 {got} != 期望 {expected}")


def single_stream_to_fd(src, url, fd, sink=None, skip=0):
    conn, r = src.fetch_full(url)
    got = 0
    dropped = 0
    try:
        while True:
            b = r.read(MIB)
            if not b:
                break
            if dropped < skip:
                cut = min(skip - dropped, len(b))
                dropped += cut
                b = b[cut:]
                if not b:
                    continue
            os.pwrite(fd, b, skip + got)
            got += len(b)
            if sink is not None:
                sink.note_seq(skip + got, len(b))
    finally:
        conn.close()
    return got


def run_parallel_chunks(src, url, fd, n_chunks, meta, sidecar, args, sink=None, lock=None):
    total = meta["size"]

    def _rng(ci):
        start = ci * args.chunk_size
        if sink is not None:
            start = max(start, sink.base)
        return start, min((ci + 1) * args.chunk_size, total)

    pending = [ci for ci in range(n_chunks)
               if ci not in meta["completed_chunks"] and _rng(ci)[1] > _rng(ci)[0]]
    if not pending:
        return "ok"
    q = queue.Queue()
    for ci in pending:
        q.put(ci)
    abort = threading.Event()
    range_ignored = threading.Event()
    if lock is None:
        lock = threading.Lock()
    fatal = []
    fatal_reason = []

    def worker():
        while not abort.is_set():
            if sink is not None:
                try:
                    sink.throttle()
                except StreamFatal as e:
                    log(f"流式链路致命错误（尚未领块），不再重试: {e}")
                    fatal_reason.append(str(e))
                    abort.set()
                    return
            try:
                ci = q.get_nowait()
            except queue.Empty:
                return
            off, end = _rng(ci)
            size = end - off
            log(f"chunk{ci} 开始 {human(size)} @{human(off)}")
            ok = False
            for attempt in range(1, args.max_retries + 1):
                if abort.is_set():
                    return
                try:
                    t0 = time.time()
                    conn, r = src.fetch_range(url, off, size)
                    try:
                        stream_to_fd(r, fd, off, size, f"chunk{ci}", sink=sink, ci=ci)
                    finally:
                        conn.close()
                    dt = time.time() - t0
                    with lock:
                        meta["completed_chunks"].append(ci)
                        meta["completed_chunks"].sort()
                        save_sidecar(sidecar, meta)
                        done_n = len(meta["completed_chunks"])
                    log(f"chunk{ci} 完成 {human(size)} / {dt:.1f}s = {size / dt / MIB:.2f} MiB/s "
                        f"(总进度 {done_n}/{n_chunks})")
                    ok = True
                    break
                except RangeIgnored as e:
                    log(f"chunk{ci} {e} —— 取消并发, 回退单流")
                    range_ignored.set()
                    abort.set()
                    return
                except StreamFatal as e:
                    log(f"chunk{ci} 流式链路致命错误，不再重试: {e}")
                    fatal_reason.append(str(e))
                    abort.set()
                    return
                except Exception as e:
                    log(f"chunk{ci} 失败 (attempt {attempt}/{args.max_retries}): {e!r}")
                    if attempt < args.max_retries and abort.wait(2 ** attempt):
                        return
            if not ok:
                log(f"chunk{ci} 重试 {args.max_retries} 次仍失败, 中止整个下载")
                fatal.append(ci)
                abort.set()
                return

    n_workers = min(args.connections, len(pending))
    log(f"并发下载: {n_workers} 连接, 待下 {len(pending)} 块")
    threads = [threading.Thread(target=worker, daemon=True, name=f"dl-{i}") for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if range_ignored.is_set():
        return "fallback"
    if fatal_reason:
        raise StreamFatal(fatal_reason[0])
    if fatal:
        raise SystemExit(f"ERROR: chunk{fatal} 重试 {args.max_retries} 次仍失败, 中止。"
                         f"已完成块已记入 sidecar, 重跑同命令即续传。")
    return "ok"


TINY_DIRECT_MAX = int(os.environ.get("DL_TINY_DIRECT_MAX_MB", "16") or 16) * MIB


def _probe_with_fallback(src, args, url):
    try:
        return src.probe(url)
    except Exception as exc:
        if args.direct or not src.worker:
            raise
        werr = exc
        log(f"⚠ worker 探测失败（{type(werr).__name__}: {werr}）→ 直连重探一次（bug #54 兜底）")
    dsrc = Source(worker=None, direct=True, timeout=src.timeout)
    try:
        total, range_ok = dsrc.probe(url)
    except Exception as e2:
        raise SystemExit(f"ERROR: 探测失败（worker 与直连都不行）worker={werr!r} direct={e2!r}")
    if total is not None and total > TINY_DIRECT_MAX:
        raise SystemExit(
            "ERROR: worker 探测失败（%r）；直连探得 %d 字节 > 上限 %d MB，不自动改走直连。"
            "要下请显式加 --direct，或先修 worker（bugs.md #54）"
            % (werr, total, TINY_DIRECT_MAX // MIB))
    log(f"[兜底] 直连探测成功（total={human(total) if total else '未知'} ≤ {TINY_DIRECT_MAX // MIB} MB）"
        f" → **本次改为直连下载**（worker 侧响应异常，见 bugs.md #54）")
    src.direct, src.worker = True, None
    return total, range_ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("-o", "--output", required=True, help="最终输出路径(下载中即落在此路径, 配合 sidecar 断点)")
    ap.add_argument("--worker", default=DEFAULT_WORKER,
                    help="流式 Worker base URL；或 'auto'=从 worker 池(worker_pool.py/workers.json)随机挑一个存活口"
                         "(默认 %(default)s 固定端口; 勿直连 Windows 侧地址，只走本机隧道口)")
    ap.add_argument("--chunk-size", type=parse_size, default=256 * MIB, help="块大小(默认 256M)")
    ap.add_argument("--md5", help="官方整文件 md5(给定则终验比对)")
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--connections", type=int, default=2,
                    help="concurrent range connections per transfer (default 2; 1 = one block at a time)")
    ap.add_argument("--max-bytes", type=parse_size, help="只下前 N 字节(测试用; 需 Range 支持, 单流回退模式下报错)")
    ap.add_argument("--direct", action="store_true", help="绕过 Worker 直连官方源(对比/兜底)")
    g = ap.add_argument_group("流式中转（SSD 只当有界窗口，边下边传到 NFS）")
    g.add_argument("--stream", action="store_true",
                   help="开启：下载数据同时流到 <输出>.partial，flush 过的区间在 SSD 上打洞释放")
    g.add_argument("--window-dir", default=None, help="SSD 窗口根目录（默认取环境变量 STAGE_DIR）")
    g.add_argument("--window-max", type=parse_size, default=1024 * MIB,
                   help="窗口在途上限(默认 1G)；下限 (connections+2)*chunk-size，低于它会误触背压")
    g.add_argument("--sync-bytes", type=parse_size, default=512 * MIB, help="每传多少字节 fsync 一次(默认 512M)")
    g.add_argument("--sync-secs", type=float, default=60.0, help="最长多少秒 fsync 一次(默认 60)")
    g.add_argument("--punch", default="auto", choices=["auto", "fallocate", "ctypes", "none"],
                   help="打洞方式（none=只用窗口不释放，调试用）")
    g.add_argument("--final-check", default="auto", choices=["auto", "always", "never"],
                   help="是否用常驻 stream_checker 管道顺路做全量 gzip container 校验（.bin 且无续传时默认开）")
    args = ap.parse_args()

    os.umask(0o027)

    if args.stream and args.connections < 1:
        raise SystemExit("ERROR: --stream 需要 --connections >= 1")
    if args.stream:
        floor = args.sync_bytes + (args.connections + 2) * args.chunk_size
        if args.window_max < floor:
            args.window_max = floor
            log(f"[stream] window-max 低于下限(sync_bytes+在途块)，抬到 {human(args.window_max)}")

    worker = args.worker
    if args.direct:
        # --direct 走直连，口根本用不上 ⇒ 必须**先**判 direct。
        # 反过来的话 `--worker auto --direct` 会在探活那步就退出（池里没口时），
        # 于是一个明说"绕过 worker"的调用，反倒因为找不到 worker 而失败。
        worker = None
    elif not worker:
        raise SystemExit("ERROR: 未指定 --worker（endpoints.default_worker 未配置）\n"
                         "       请显式 --worker <URL>、--worker auto（自动挑存活口），"
                         "或 --direct 直连官方源。")
    if worker == 'auto':
        try:
            import worker_pool
            picked = worker_pool.pick_alive()
        except Exception as e:
            raise SystemExit(f"ERROR: --worker auto 探活失败: {e!r}")
        if not picked:
            raise SystemExit("ERROR: --worker auto 未发现任何存活 worker"
                             f"(检查隧道/worker；查看: python3 {os.path.join(HERE, 'worker_pool.py')})")
        worker = picked
        log(f"[auto] 从 worker 池选中 {worker}")

    src = Source(worker=worker, direct=args.direct)
    out = os.path.abspath(args.output)
    global _TASK
    _TASK = os.path.basename(out)
    sidecar = out + ".download.json"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    mode = "direct 直连" if args.direct else f"worker {src.worker}"
    log(f"目标 {args.url} → {out} ({mode})")

    if not args.direct:
        _adv_port = _advisory_declare(src.worker, args.url, out)
        if _adv_port:
            log(f"[lease] 已声明口 {_adv_port}（advisory：拿不到锁也照常下载）")

    try:
        total, range_ok = _probe_with_fallback(src, args, args.url)
    except SystemExit:
        raise
    except Exception as e:
        raise SystemExit(f"ERROR: 探测失败: {e!r}")
    log(f"探测: total={human(total) if total else '未知'}, Range={'支持' if range_ok else '不支持'}")

    sink = None
    resume_from = 0
    chunked = range_ok and total is not None
    if args.stream:
        if sv is None:
            raise SystemExit("ERROR: --stream 需要同目录的 stream_verify.py（导入失败）")
        wdir = args.window_dir or os.environ.get("STAGE_DIR") or ""
        if not wdir:
            raise SystemExit("ERROR: --stream 需要 --window-dir 或环境变量 STAGE_DIR")
        wname = os.path.basename(out)
        window = os.path.join(wdir, wname, wname)
        if os.path.exists(out) and not os.path.exists(sidecar):
            _v, _detail = "unknown", "未做磁盘可信度判定"
            try:
                import disk_trust as _dt
                _v, _detail, _loc, _rem = _dt.judge_existing(out, args.url)
                _dt.append_ledger(_v, out, _detail, url=args.url, who="linux_downloader",
                                  local=_loc, remote=_rem)
            except Exception as _e:
                _v, _detail = "unknown", f"可信度判定失败（{type(_e).__name__}）"
            if _v == "bad":
                _bad = out + time.strftime(".badsize-%Y%m%d%H%M%S", time.localtime(time.time() + 8 * 3600))
                try:
                    os.replace(out, _bad)
                    log(f"目标已存在但与远端不符（{_detail}）→ 挪开留证 {_bad}，重新下载")
                except OSError as _e:
                    log(f"警告: 与远端不符但挪不开（{_e}）→ 继续覆盖下载：{out}")
            else:
                log(f"目标已存在且无 sidecar（{_detail}）→ 跳过：{out}")
                return
        if args.max_bytes and chunked and args.max_bytes < total:
            log(f"--max-bytes: 只下前 {human(args.max_bytes)}(测试截断, 非完整文件)")
            total = args.max_bytes
        n_chunks = ((total + args.chunk_size - 1) // args.chunk_size) if chunked else 0
        meta = {"url": args.url, "size": total, "chunk_size": args.chunk_size,
                "completed_chunks": [], "mode": "stream", "final": out,
                "window": window, "durable_bytes": 0}
        if os.path.exists(sidecar):
            with open(sidecar) as f:
                old = json.load(f)
            if (old.get("mode") == "stream" and old.get("url") == args.url
                    and old.get("size") == total and old.get("chunk_size") == args.chunk_size):
                durable = int(old.get("durable_bytes") or 0)
                ra = old.get("resume_at")
                ra = durable if ra is None else int(ra)
                resume_from = max(0, min(ra, total))
                meta["gzip"] = old.get("gzip")
                log(f"流式续传: NFS 侧 durable {human(durable)}、member 边界续传点 "
                    f"{human(resume_from)}，从该处继续"
                    + (f"（回退 {human(durable - resume_from)} 到上一个 member 边界，为了能续验）"
                       if durable > resume_from else ""))
            else:
                log("警告: sidecar 与本次参数不符或非流式，从零开始（残留 .partial 会被截断重下）")
        if chunked:
            meta["completed_chunks"] = list(range(min(resume_from // args.chunk_size, n_chunks)))
        meta["durable_bytes"] = resume_from
        save_sidecar(sidecar, meta)
        want_checker = args.final_check == "always" or (
            args.final_check == "auto" and out.endswith(".bin"))
        sink = StreamSink(final=out, window=window, total=total or 0,
                          chunk_size=args.chunk_size, n_chunks=n_chunks, meta=meta,
                          sidecar=sidecar, lock=threading.Lock(),
                          window_max=args.window_max, sync_bytes=args.sync_bytes,
                          sync_secs=args.sync_secs, punch_mode=args.punch,
                          use_checker=want_checker, resume_from=resume_from,
                          expect_md5=args.md5)
        if not chunked:
            sink.seq_mode = True
        fd = sink.wfd
    else:
        fd = os.open(out, os.O_RDWR | os.O_CREAT, 0o644)
    t_start = time.time()
    try:
        if sink is not None:
            if not chunked:
                log("Server does not support HTTP Range; 流式单流模式（含 skip 续传，但无法并发）")
                got = single_stream_to_fd(src, args.url, fd, sink=sink, skip=resume_from)
                log(f"单流下载完成 {human(got)} / {time.time() - t_start:.1f}s")
                if total is not None and resume_from + got != total:
                    raise SystemExit(f"ERROR: 单流收到 {resume_from + got} != 探测值 {total}")
            elif args.connections > 1:
                outcome = run_parallel_chunks(src, args.url, fd, n_chunks, meta, sidecar,
                                              args, sink=sink, lock=sink.lock)
                if outcome == "fallback":
                    raise SystemExit("ERROR: 下载中途 Range 被忽略；--stream 下请重跑同命令"
                                     "（已传部分在 NFS 上有效，会从断点继续）")
            else:
                for ci in range(n_chunks):
                    if ci in meta["completed_chunks"]:
                        continue
                    off = ci * args.chunk_size
                    if off < sink.base:
                        off = sink.base
                    size = min(args.chunk_size, total - off)
                    if size <= 0:
                        continue
                    sink.throttle()
                    ok = False
                    for attempt in range(1, args.max_retries + 1):
                        try:
                            t0 = time.time()
                            conn, r = src.fetch_range(args.url, off, size)
                            try:
                                stream_to_fd(r, fd, off, size, f"chunk{ci}", sink=sink, ci=ci)
                            finally:
                                conn.close()
                            dt = time.time() - t0
                            with sink.lock:
                                meta["completed_chunks"].append(ci)
                                meta["completed_chunks"].sort()
                                save_sidecar(sidecar, meta)
                            log(f"chunk{ci} 完成 {human(size)} / {dt:.1f}s = {size / dt / MIB:.2f} MiB/s "
                                f"(总进度 {len(meta['completed_chunks'])}/{n_chunks})")
                            ok = True
                            break
                        except Exception as e:
                            log(f"chunk{ci} 失败 (attempt {attempt}/{args.max_retries}): {e!r}")
                            if attempt < args.max_retries:
                                time.sleep(2 ** attempt)
                    if not ok:
                        raise SystemExit(f"ERROR: chunk{ci} 重试 {args.max_retries} 次仍失败, 中止。"
                                         f"NFS 已传部分与 sidecar 保留, 重跑同命令即续传。")
            errs = sink.finish()
            if errs:
                where = sink.quarantine("；".join(errs))
                raise SystemExit("ERROR: 流式终验未通过 → 已隔离 " + where)
            dt = time.time() - t_start
            sz = os.path.getsize(out)
            log(f"==== 下载完成(流式): {out} ({human(sz)}) 总耗时 {dt:.1f}s"
                + (f", 平均 {sz / dt / MIB:.2f} MiB/s" if dt > 0 else "")
                + f", 窗口已清 (打洞释放累计 {human(sink.punch_hi)})")
            return

        if not range_ok or total is None:
            if args.max_bytes:
                raise SystemExit("ERROR: --max-bytes 需要 Range 支持, 该源已回退单流模式")
            log("Server does not support HTTP Range; falling back to single-stream download. "
                "(该模式无断点续传, 中断需重下)")
            got = single_stream_to_fd(src, args.url, fd)
            os.ftruncate(fd, got)
            log(f"单流下载完成 {human(got)} / {time.time() - t_start:.1f}s")
        else:
            if args.max_bytes and args.max_bytes < total:
                log(f"--max-bytes: 只下前 {human(args.max_bytes)}(测试截断, 非完整文件)")
                total = args.max_bytes
            n_chunks = (total + args.chunk_size - 1) // args.chunk_size
            meta = {"url": args.url, "size": total, "chunk_size": args.chunk_size,
                    "completed_chunks": []}
            if os.path.exists(sidecar):
                with open(sidecar) as f:
                    old = json.load(f)
                if old.get("url") == args.url and old.get("size") == total \
                        and old.get("chunk_size") == args.chunk_size:
                    meta["completed_chunks"] = sorted(set(old.get("completed_chunks", [])))
                    log(f"断点恢复: {len(meta['completed_chunks'])}/{n_chunks} 块已完成, 跳过")
                else:
                    log("警告: sidecar 与本次参数不符, 从零开始(已下载数据保留但会被逐块覆写)")
            os.ftruncate(fd, total)
            save_sidecar(sidecar, meta)

            if args.connections > 1:
                outcome = run_parallel_chunks(src, args.url, fd, n_chunks, meta, sidecar, args)
                if outcome == "fallback":
                    if args.max_bytes:
                        raise SystemExit("ERROR: --max-bytes 需要 Range 支持, 运行中回退单流无法截断")
                    log("下载中途 Range 被忽略; falling back to single-stream download. (无断点, 从头重下)")
                    got = single_stream_to_fd(src, args.url, fd)
                    os.ftruncate(fd, got)
                    log(f"单流下载完成 {human(got)} / {time.time() - t_start:.1f}s")
            else:
                for ci in range(n_chunks):
                    if ci in meta["completed_chunks"]:
                        continue
                    off = ci * args.chunk_size
                    size = min(args.chunk_size, total - off)
                    ok = False
                    for attempt in range(1, args.max_retries + 1):
                        try:
                            t0 = time.time()
                            conn, r = src.fetch_range(args.url, off, size)
                            try:
                                stream_to_fd(r, fd, off, size, f"chunk{ci}")
                            finally:
                                conn.close()
                            dt = time.time() - t0
                            meta["completed_chunks"].append(ci)
                            meta["completed_chunks"].sort()
                            save_sidecar(sidecar, meta)
                            log(f"chunk{ci} 完成 {human(size)} / {dt:.1f}s = {size / dt / MIB:.2f} MiB/s "
                                f"(总进度 {len(meta['completed_chunks'])}/{n_chunks})")
                            ok = True
                            break
                        except Exception as e:
                            log(f"chunk{ci} 失败 (attempt {attempt}/{args.max_retries}): {e!r}")
                            if attempt < args.max_retries:
                                time.sleep(2 ** attempt)
                    if not ok:
                        raise SystemExit(f"ERROR: chunk{ci} 重试 {args.max_retries} 次仍失败, 中止。"
                                         f"已下载数据与 sidecar 保留, 重跑同命令即续传。")

        log("终验: 大小/ md5 / zero_scan ...")
        actual = os.path.getsize(out)
        if total is not None and actual != total:
            raise SystemExit(f"ERROR: 终验大小 {actual} != 探测值 {total}; sidecar 保留")
        if args.md5:
            t0 = time.time()
            m = md5_file(out)
            if m != args.md5.lower():
                raise SystemExit(f"ERROR: 终验 md5 {m} != 官方 {args.md5}; sidecar 保留")
            log(f"终验 md5 一致({time.time() - t0:.1f}s)")
        holes = zero_scan(out)
        if holes:
            raise SystemExit(f"ERROR: zero_scan 发现 {len(holes)} 处 >=64KB 零串(疑似卡洞), "
                             f"首处偏移 {holes[0]}; sidecar 保留")
        if os.path.exists(sidecar):
            os.unlink(sidecar)
        dt = time.time() - t_start
        size_done = os.path.getsize(out)
        log(f"==== 下载完成: {out} ({human(size_done)}) 总耗时 {dt:.1f}s"
            + (f", 平均 {size_done / dt / MIB:.2f} MiB/s" if dt > 0 else "") + ", sidecar 已清理")
    except StreamFatal as e:
        if sink is not None and sink.verify_error:
            where = sink.quarantine(str(e))
            raise SystemExit(f"ERROR: {e} → 已隔离 {where}")
        raise SystemExit(f"ERROR: {e}（NFS .partial 与 sidecar 保留，重跑同命令即续传）")
    except KeyboardInterrupt:
        raise SystemExit("\n用户中断: 已下载数据与 sidecar 保留, 重跑同命令即续传。")
    finally:
        if sink is None:
            os.close(fd)
        else:
            sink.abort()


if __name__ == "__main__":
    main()
