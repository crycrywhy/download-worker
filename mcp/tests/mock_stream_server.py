#!/usr/bin/env python3
"""mock_stream_server.py — 流式 Worker 契约的本地 mock, 供 relay_client --via-worker 脱离 Windows 自测。

行为(单文件 payload, --payload 指定):
  GET /stream?url=<u>  Worker 契约模式:
      <u> 含 "missing"      → 404 (模拟上游错误暴露)
      <u> 含 "ignorerange"  → 200 全量 (模拟上游/Worker 忽略 Range, 客户端必须响亮失败)
      <u> 含 "chunkfail"    → 探测正常, 其余 Range 一律 500 (测重试耗尽)
      <u> 含 "badcr"        → 206 但 Content-Range start 故意 +1 (测 Content-Range 校验)
      <u> 含 "flaky"        → 首个非探测 Range 请求 500 一次, 之后正常 (测单块独立重试)
      <u> 含 "midrun200"    → 探测(bytes=0-0)正常回 206, 数据 Range 回 200 全量
                              (测并发中途 Range 被忽略 → 取消并发 → 回退单流)
      带 Range 头            → 206 + Content-Range + 对应切片
      不带 Range             → 200 全量流式
  --delay-s S: 每个 /stream 与 direct 数据请求先睡 S 秒(模拟传输耗时, 供并发重叠计时取证)。
  GET 其它路径(如 /origin.bin)  直连官方源模式: 同样支持 Range→206, 用于回归 direct 模式。

用法: python3 mock_stream_server.py --payload /path/origin.bin --port 18766 [--delay-s 1]
"""
import argparse
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

MB = 1024 * 1024
PAYLOAD = None   # str, 由 main 注入
DELAY_S = 0.0    # float, 由 main 注入
FLAKY_LOCK = threading.Lock()
FLAKY_SEEN = False  # flaky 模式: 首个数据 Range 是否已 500 过


def send_slice(h, start, end):
    """end 含端点。写 206 + Content-Range + 流式 body。"""
    import os
    total = os.path.getsize(PAYLOAD)
    end = min(end, total - 1)
    length = end - start + 1
    h.send_response(206)
    h.send_header("Content-Range", f"bytes {start}-{end}/{total}")
    h.send_header("Content-Length", str(length))
    h.end_headers()
    with open(PAYLOAD, "rb") as f:
        f.seek(start)
        left = length
        while left > 0:
            b = f.read(min(MB, left))
            if not b:
                break
            h.wfile.write(b)
            left -= len(b)


def send_full(h, status=200):
    import os
    total = os.path.getsize(PAYLOAD)
    h.send_response(status)
    h.send_header("Content-Length", str(total))
    h.end_headers()
    with open(PAYLOAD, "rb") as f:
        while True:
            b = f.read(MB)
            if not b:
                break
            h.wfile.write(b)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/stream":
            target = parse_qs(u.query).get("url", [""])[0]
            if "missing" in target:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if "ignorerange" in target:
                send_full(self, 200)  # 无视 Range, 模拟违约上游
                return
            if "chunkfail" in target:
                # 探测(bytes=0-0)正常, 其余 Range 一律 500, 测客户端重试耗尽
                if self.headers.get("Range", "").strip() != "bytes=0-0":
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            if "badcr" in target:
                # 206 但 Content-Range start 故意 +1, 测客户端 Content-Range 严格校验
                rng0 = self.headers.get("Range", "")
                m0 = re.match(r"bytes=(\d+)-(\d+)", rng0)
                if m0 and rng0.strip() != "bytes=0-0":
                    start, end = int(m0.group(1)), int(m0.group(2))
                    total = os.path.getsize(PAYLOAD)
                    end = min(end, total - 1)
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start + 1}-{end}/{total}")  # 故意错
                    self.send_header("Content-Length", str(end - start + 1))
                    self.end_headers()
                    with open(PAYLOAD, "rb") as f:
                        f.seek(start)
                        self.wfile.write(f.read(end - start + 1))
                    return
            if "flaky" in target:
                # 首个非探测 Range 500 一次(线程安全), 之后正常——测单块独立重试
                global FLAKY_SEEN
                if self.headers.get("Range", "").strip() != "bytes=0-0":
                    with FLAKY_LOCK:
                        if not FLAKY_SEEN:
                            FLAKY_SEEN = True
                            self.send_response(500)
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
            if "midrun200" in target:
                # 探测正常 206; 数据 Range 回 200 全量(忽略 Range), 测中途回退单流
                if self.headers.get("Range", "").strip() not in ("", "bytes=0-0"):
                    send_full(self, 200)
                    return
        elif u.path == "/health":
            body = b'{"status":"ok","worker":"mock-stream"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if DELAY_S:
            time.sleep(DELAY_S)
        # 契约正常路径与 direct 模式共用: 有 Range→206, 无→200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d+)-(\d+)", rng)
            if not m:
                self.send_response(416)
                self.end_headers()
                return
            send_slice(self, int(m.group(1)), int(m.group(2)))
        else:
            send_full(self, 200)


def main():
    global PAYLOAD, DELAY_S
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", required=True)
    ap.add_argument("--port", type=int, default=18766)
    ap.add_argument("--delay-s", type=float, default=0.0)
    args = ap.parse_args()
    PAYLOAD = args.payload
    DELAY_S = args.delay_s
    print(f"mock_stream_server on 127.0.0.1:{args.port}, payload={PAYLOAD}, delay={DELAY_S}s", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()


if __name__ == "__main__":
    main()
