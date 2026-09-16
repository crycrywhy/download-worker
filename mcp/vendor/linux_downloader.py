#!/usr/bin/env python3
"""linux_downloader.py — Linux 主导的大文件流式下载器(经 Windows 流式 Worker 出口)。

架构(升级指南2 §24): Linux = Download Manager, Windows = Network Egress / Streaming Proxy。
Worker 契约: GET {worker}/stream?url=<urlencoded> , Range 头透传, 流式回传(见指南2 §18)。

用法:
  python3 linux_downloader.py "https://example.com/large.sra" -o /data/large.sra
      [--worker http://127.0.0.1:8766] [--chunk-size 256M] [--md5 <官方md5>]
      [--max-retries 3] [--direct]

行为(对应指南2条目):
  探测(§6): GET Range bytes=0-0 —— 206 → 得总大小+Range 可用; 200 → Range 不可用,
            回退单次流式下载并明确报告(§16)。(不用 HEAD: Worker 契约只保证 GET /stream。)
  分块(§7): 默认 256 MiB 可配; 串行下载(§11), 1 MiB 流式读, os.pwrite 随机写(§8/§10)。
  校验(§12): 每块验 status==206 + Content-Range 区间逐字节吻合 + 实收字节数==期望。
  重试(§13): 每块最多 --max-retries 次, 指数退避 2/4/8s, 不无限重试。
  断点(§14): sidecar <输出>.download.json 记 completed_chunks; 重跑同命令自动跳过已完成块。
  安全(§15): 失败/Ctrl+C 不删已下载数据; 全部完成+终验通过才删 sidecar。
  终验(§23): 大小 == 探测值; 给 --md5 则整文件比对; zero_scan(>=64KB 连续零=HOLE, 卡洞教训)。
  编码(§17): 目标 URL 经 quote(safe="") 编码后才拼进 /stream?url=。
  --direct: 绕过 Worker 直连官方源(对比/兜底用; 裸 http.client 不走 env 代理, 09-10 事故教训)。

仅依赖 stdlib。Linux 专用(os.pwrite)。
"""
import argparse
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

MIB = 1024 * 1024
ZERO_RUN_LIMIT = 64 * 1024


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
    """客户端发了 Range 但上游/Worker 回 200(忽略 Range)。并发模式据此中止并回退单流。"""


class Source:
    """统一的数据源: worker 模式经 /stream 转发, direct 模式直连官方源。"""

    def __init__(self, worker=None, direct=False, timeout=300):
        self.worker = worker.rstrip("/") if worker else None
        self.direct = direct
        self.timeout = timeout

    def _open(self, url, range_header=None):
        """返回 (conn, response)。调用方负责读 body 并 conn.close()。"""
        if self.direct:
            u = urlparse(url)
            conn = (http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=self.timeout)
                    if u.scheme == "https"
                    else http.client.HTTPConnection(u.hostname, u.port or 80, timeout=self.timeout))
            path = u.path + (f"?{u.query}" if u.query else "")
        else:
            w = urlparse(self.worker)
            conn = (http.client.HTTPSConnection(w.hostname, w.port or 443, timeout=self.timeout)
                    if w.scheme == "https"
                    else http.client.HTTPConnection(w.hostname, w.port or 80, timeout=self.timeout))
            path = "/stream?url=" + quote(url, safe="")
        headers = {"Range": range_header} if range_header else {}
        conn.request("GET", path, headers=headers)
        return conn, conn.getresponse()

    def probe(self, url):
        """GET bytes=0-0 探测。返回 (total_size|None, range_ok|False)。"""
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
        """流式取 [off, off+size)。校验 206 + Content-Range, 返回 (conn, response) 由调用方读。"""
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
        """单次流式(无 Range 回退路径)。返回 (conn, response)。"""
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


def zero_scan(path):
    """扫描 >=64KB 连续零串(卡洞)。返回 HOLE 起始偏移列表。"""
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


def stream_to_fd(r, fd, off, expected, label):
    """从 response 流式读 expected 字节, pwrite 到 fd@off 起。返回实收字节数。"""
    got = 0
    while True:
        b = r.read(MIB)
        if not b:
            break
        os.pwrite(fd, b, off + got)
        got += len(b)
    if got != expected:
        raise RuntimeError(f"{label} 实收 {got} != 期望 {expected}")


def single_stream_to_fd(src, url, fd):
    """§16 单流回退: 无 Range, 从头流式写到尾。返回总字节数。"""
    conn, r = src.fetch_full(url)
    got = 0
    try:
        while True:
            b = r.read(MIB)
            if not b:
                break
            os.pwrite(fd, b, got)
            got += len(b)
    finally:
        conn.close()
    return got


def run_parallel_chunks(src, url, fd, n_chunks, meta, sidecar, args):
    """--connections>1 的并发块调度(指南4): queue 供块 + N 个 worker 线程, 有界并发。

    复用串行路径的全部机制: fetch_range(206/Content-Range/字节数校验) + 1MiB 流式
    pwrite + 2/4/8s 退避重试 + sidecar 断点。任一块重试耗尽 → 中止整个下载(sidecar 保留);
    某块遇 RangeIgnored(200) → 取消并发, 返回 "fallback" 由调用方走单流回退。
    已启动的请求允许完成并记入 sidecar, 不静默丢弃。
    """
    total = meta["size"]
    pending = [ci for ci in range(n_chunks) if ci not in meta["completed_chunks"]]
    if not pending:
        return "ok"
    q = queue.Queue()
    for ci in pending:
        q.put(ci)
    abort = threading.Event()          # 致命失败: 不再领新块
    range_ignored = threading.Event()  # 块级 200: 回退单流
    lock = threading.Lock()            # 保护 meta/sidecar
    fatal = []

    def worker():
        while not abort.is_set():
            try:
                ci = q.get_nowait()
            except queue.Empty:
                return
            off = ci * args.chunk_size
            size = min(args.chunk_size, total - off)
            log(f"chunk{ci} 开始 {human(size)} @{human(off)}")
            ok = False
            for attempt in range(1, args.max_retries + 1):
                if abort.is_set():
                    return
                try:
                    t0 = time.time()
                    conn, r = src.fetch_range(url, off, size)
                    try:
                        stream_to_fd(r, fd, off, size, f"chunk{ci}")
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
                except Exception as e:
                    log(f"chunk{ci} 失败 (attempt {attempt}/{args.max_retries}): {e!r}")
                    if attempt < args.max_retries and abort.wait(2 ** attempt):
                        return  # 退避期间被中止
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
    if fatal:
        raise SystemExit(f"ERROR: chunk{fatal} 重试 {args.max_retries} 次仍失败, 中止。"
                         f"已完成块已记入 sidecar, 重跑同命令即续传。")
    return "ok"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("-o", "--output", required=True, help="最终输出路径(下载中即落在此路径, 配合 sidecar 断点)")
    ap.add_argument("--worker", default="http://127.0.0.1:8766",
                    help="流式 Worker base URL；或 'auto'=从 worker 池(worker_pool.py/workers.json)随机挑一个存活口"
                         "(默认 %(default)s 固定端口; 请勿直连出口机器的其它地址)")
    ap.add_argument("--chunk-size", type=parse_size, default=256 * MIB, help="块大小(默认 256M)")
    ap.add_argument("--md5", help="官方整文件 md5(给定则终验比对)")
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--connections", type=int, default=2,
                    help="并发 Range 下载连接数(2026-09-10 用户定默认 2; 1=串行旧行为; >1 时最多 N 个块同时下载)")
    ap.add_argument("--max-bytes", type=parse_size, help="只下前 N 字节(测试用; 需 Range 支持, 单流回退模式下报错)")
    ap.add_argument("--direct", action="store_true", help="绕过 Worker 直连官方源(对比/兜底)")
    args = ap.parse_args()

    worker = args.worker
    if worker == 'auto':   # 2026-09-14: 多 PC 池动态选择（agent/手工用；driver 走显式 URL 分线）
        try:
            import worker_pool
            picked = worker_pool.pick_alive()
        except Exception as e:
            raise SystemExit(f"ERROR: --worker auto 探活失败: {e!r}")
        if not picked:
            raise SystemExit("ERROR: --worker auto 未发现任何存活 worker"
                             "(检查隧道/worker；查看: python3 worker_pool.py)")
        worker = picked
        log(f"[auto] 从 worker 池选中 {worker}")

    src = Source(worker=worker, direct=args.direct)
    out = os.path.abspath(args.output)
    sidecar = out + ".download.json"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    mode = "direct 直连" if args.direct else f"worker {src.worker}"
    log(f"目标 {args.url} → {out} ({mode})")

    # ---- 探测(§6) ----
    try:
        total, range_ok = src.probe(args.url)
    except Exception as e:
        raise SystemExit(f"ERROR: 探测失败: {e!r}")
    log(f"探测: total={human(total) if total else '未知'}, Range={'支持' if range_ok else '不支持'}")

    fd = os.open(out, os.O_RDWR | os.O_CREAT, 0o644)
    t_start = time.time()
    try:
        if not range_ok or total is None:
            # ---- §16 回退: 单次流式, 无断点 ----
            if args.max_bytes:
                raise SystemExit("ERROR: --max-bytes 需要 Range 支持, 该源已回退单流模式")
            log("Server does not support HTTP Range; falling back to single-stream download. "
                "(该模式无断点续传, 中断需重下)")
            got = single_stream_to_fd(src, args.url, fd)
            os.ftruncate(fd, got)
            log(f"单流下载完成 {human(got)} / {time.time() - t_start:.1f}s")
        else:
            # ---- 分块模式 ----
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
            os.ftruncate(fd, total)  # sparse 预分配
            save_sidecar(sidecar, meta)

            if args.connections > 1:
                # ---- 并发模式(指南4): 有界并发调度, 复用同一套校验/重试/断点 ----
                outcome = run_parallel_chunks(src, args.url, fd, n_chunks, meta, sidecar, args)
                if outcome == "fallback":
                    if args.max_bytes:
                        raise SystemExit("ERROR: --max-bytes 需要 Range 支持, 运行中回退单流无法截断")
                    log("下载中途 Range 被忽略; falling back to single-stream download. (无断点, 从头重下)")
                    got = single_stream_to_fd(src, args.url, fd)
                    os.ftruncate(fd, got)
                    log(f"单流下载完成 {human(got)} / {time.time() - t_start:.1f}s")
            else:
                # ---- 串行模式(默认, 行为与旧版一致) ----
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
                                time.sleep(2 ** attempt)  # 2/4/8s 指数退避
                    if not ok:
                        raise SystemExit(f"ERROR: chunk{ci} 重试 {args.max_retries} 次仍失败, 中止。"
                                         f"已下载数据与 sidecar 保留, 重跑同命令即续传。")

        # ---- 终验(§23) ----
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
    except KeyboardInterrupt:
        raise SystemExit("\n用户中断: 已下载数据与 sidecar 保留, 重跑同命令即续传。")
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
