import argparse
import ctypes
import hashlib
import os
import re
import stat
import struct
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dl_config as CFG

MIB = 1024 * 1024
ZERO_RUN_LIMIT = 64 * 1024
GZIP_MAGIC = b"\x1f\x8b"
BGZF_EOF_SIG = bytes.fromhex("1f8b08040000000000ff060042430200")
TAIL_KEEP = 4096
VERIFY_LAG = 16 * MIB
ZLIB_WINDOW = 1024 * 1024
STREAM_CHECKER = CFG.get('tools.stream_checker', '')

FTEXT, FHCRC, FEXTRA, FNAME, FCOMMENT = 1, 2, 4, 8, 16

FALLOC_FL_KEEP_SIZE = 0x01
FALLOC_FL_PUNCH_HOLE = 0x02


def punch_hole(path, off, length, mode="auto"):
    start = (int(off) + 4095) & ~4095
    end = (int(off) + int(length)) & ~4095
    if end <= start:
        return 0
    n = end - start
    if mode == "none":
        return 0
    if mode in ("auto", "fallocate"):
        try:
            r = subprocess.run(["fallocate", "-p", "-o", str(start), "-l", str(n), path],
                               capture_output=True, text=True, timeout=300)
            if r.returncode == 0:
                return n
            err = f"fallocate -p rc={r.returncode}: {(r.stderr or '').strip()[:200]}"
            if mode == "fallocate":
                raise RuntimeError(err)
        except FileNotFoundError:
            err = "未找到 fallocate 命令"
            if mode == "fallocate":
                raise RuntimeError(err)
    if mode in ("auto", "ctypes"):
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.fallocate.restype = ctypes.c_int
            libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int,
                                       ctypes.c_longlong, ctypes.c_longlong]
            fd = os.open(path, os.O_RDWR)
            try:
                rc = libc.fallocate(fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE,
                                    ctypes.c_longlong(start), ctypes.c_longlong(n))
            finally:
                os.close(fd)
            if rc != 0:
                en = ctypes.get_errno()
                raise OSError(en, os.strerror(en))
            return n
        except Exception as e:
            if mode == "ctypes":
                raise RuntimeError(f"fallocate(2) ioctl 失败: {e!r}")
    raise RuntimeError(f"打洞失败（{err if 'err' in dir() else mode}）")


def blocks_used(path):
    st = os.stat(path)
    if stat.S_ISDIR(st.st_mode):
        tot = 0
        for r, _d, fs in os.walk(path):
            for f in fs:
                try:
                    tot += os.stat(os.path.join(r, f)).st_blocks * 512
                except OSError:
                    pass
        return tot
    return st.st_blocks * 512


class GzipStreamVerifier:

    def __init__(self, base_offset=0, expect_gzip=None):
        self.buf = bytearray()
        self.pos = 0
        self.fed = base_offset
        self.member_start = base_offset
        self.verified_hi = base_offset
        self.resume_hi = base_offset
        self.n_members = 0
        self.member_bytes = 0
        self.is_gzip = expect_gzip
        self.is_container = False
        self.error = None
        self.tail = bytearray()
        self._dec = None
        self._crc = 0
        self._usize = 0
        self._state = "magic" if expect_gzip is not False else "passthrough"

    def _fail(self, msg):
        if not self.error:
            self.error = f"{msg}（偏移 {self.member_start}，第 {self.n_members + 1} 个 member）"

    def _parse_header(self):
        b, p = self.buf, self.pos
        if len(b) - p < 10:
            return False
        if b[p:p + 2] != GZIP_MAGIC:
            self._fail(f"member 魔术字节不符: {bytes(b[p:p + 2])!r}")
            return False
        if b[p + 2] != 8:
            self._fail(f"压缩方法非 deflate: CM={b[p + 2]}")
            return False
        flg = b[p + 3]
        q = p + 10
        bsize = None
        if flg & FEXTRA:
            if len(b) - q < 2:
                return False
            xlen = b[q] | (b[q + 1] << 8)
            if len(b) - q - 2 < xlen:
                return False
            extra = bytes(b[q + 2:q + 2 + xlen])
            i = 0
            while i + 4 <= len(extra):
                si1, si2, slen = extra[i], extra[i + 1], extra[i + 2] | (extra[i + 3] << 8)
                if si1 == ord("B") and si2 == ord("C") and slen == 2 and i + 6 <= len(extra):
                    bsize = extra[i + 4] | (extra[i + 5] << 8)
                    self.is_container = True
                i += 4 + slen
            q += 2 + xlen
        for flag in (FNAME, FCOMMENT):
            if flg & flag:
                z = b.find(0, q)
                if z < 0:
                    return False
                q = z + 1
        if flg & FHCRC:
            if len(b) - q < 2:
                return False
            q += 2
        self.member_bytes = q - p
        self._bsize = bsize
        self.pos = q
        self._dec = zlib.decompressobj(-15)
        self._crc = 0
        self._usize = 0
        self._state = "deflate"
        return True

    def _pump(self):
        while not self.error:
            if self._state == "magic":
                if len(self.buf) - self.pos < 2:
                    return
                if bytes(self.buf[self.pos:self.pos + 2]) != GZIP_MAGIC:
                    self.is_gzip = False
                    self.verified_hi = max(self.verified_hi, self.fed - VERIFY_LAG)
                    self.resume_hi = max(self.resume_hi, self.fed - VERIFY_LAG)
                    self._state = "passthrough"
                    return
                self.is_gzip = True
                self.member_start = self.fed - (len(self.buf) - self.pos)
                self.member_bytes = 0
                self._state = "header"
            elif self._state == "passthrough":
                self.verified_hi = max(self.verified_hi, self.fed - VERIFY_LAG)
                self.resume_hi = max(self.resume_hi, self.fed - VERIFY_LAG)
                return
            elif self._state == "header":
                if not self._parse_header():
                    return
            elif self._state == "deflate":
                remain = len(self.buf) - self.pos
                if remain <= 0:
                    return
                if self._bsize is not None:
                    take = min(remain, (self._bsize + 1) - self.member_bytes - 8)
                    if take <= 0:
                        self._fail(f"BSIZE 与已消费字节不符（块内剩余 {take}）")
                        return
                else:
                    take = min(remain, ZLIB_WINDOW)
                chunk = bytes(self.buf[self.pos:self.pos + take])
                try:
                    out = self._dec.decompress(chunk)
                except zlib.error as e:
                    self._fail(f"deflate 解压失败: {e}")
                    return
                self._crc = zlib.crc32(out, self._crc)
                self._usize += len(out)
                if not self._dec.eof:
                    self.member_bytes += len(chunk)
                    self.pos += len(chunk)
                    continue
                unused = self._dec.unused_data
                used = len(chunk) - len(unused)
                self.member_bytes += used
                self.pos += used
                self._state = "trailer"
            elif self._state == "trailer":
                if len(self.buf) - self.pos < 8:
                    return
                crc, isize = struct.unpack("<II", bytes(self.buf[self.pos:self.pos + 8]))
                if crc != (self._crc & 0xFFFFFFFF):
                    self._fail(f"CRC32 不符: 实际 {self._crc & 0xFFFFFFFF:08x} != 记录 {crc:08x}")
                    return
                if isize != (self._usize & 0xFFFFFFFF):
                    self._fail(f"ISIZE 不符: 实际 {self._usize} != 记录 {isize}")
                    return
                self.member_bytes += 8
                self.pos += 8
                bs = getattr(self, "_bsize", None)
                if bs is not None and self.member_bytes != bs + 1:
                    self._fail(f"gzip container BSIZE 不符: 实际块长 {self.member_bytes} != 记录 {bs + 1}")
                    return
                self.n_members += 1
                mstart = self.member_start
                self.member_start = self.fed - (len(self.buf) - self.pos)
                self.verified_hi = max(mstart + self.member_bytes, self.verified_hi)
                self.resume_hi = self.member_start
                self._state = "magic"

    def feed(self, data):
        if self.error:
            self.fed += len(data)
            return
        self.buf += data
        self.fed += len(data)
        self.tail += data
        if len(self.tail) > TAIL_KEEP:
            del self.tail[:-TAIL_KEEP]
        self._pump()
        if self._state == "passthrough":
            self.verified_hi = max(self.verified_hi, self.fed - VERIFY_LAG)
        if self.pos > 4 * MIB:
            del self.buf[:self.pos]
            self.pos = 0

    def finish(self, expect_trailer=True):
        if self.error:
            return self.error
        if self.is_gzip and self._state not in ("magic", "passthrough"):
            return f"文件在 member 中途结束（截断），偏移 {self.member_start}"
        if self.is_gzip and self.is_container and expect_trailer:
            if BGZF_EOF_SIG not in bytes(self.tail):
                return "尾部缺 gzip container EOF 块（疑截断/未刷盘）"
        self.verified_hi = self.fed
        return None


class ZeroRunScanner:

    def __init__(self, limit=ZERO_RUN_LIMIT, base_offset=0):
        self.limit = limit
        self.pat = re.compile(b"\x00{%d,}" % limit)
        self.carry = b""
        self.carry_start = None
        self.pos = base_offset
        self.holes = []

    def feed(self, data):
        ncarry = len(self.carry)
        buf = self.carry + data if ncarry else data
        base = self.pos - ncarry
        for m in self.pat.finditer(buf):
            start = self.carry_start if (m.start() == 0 and ncarry) else base + m.start()
            if not self.holes or start != self.holes[-1]:
                self.holes.append(start)
        k = len(buf) - len(buf.rstrip(b"\x00"))
        if k == 0:
            self.carry, self.carry_start = b"", None
        else:
            idx = len(buf) - k
            if not (idx == 0 and ncarry):
                self.carry_start = base + idx
            self.carry = buf[idx:][:self.limit]
        self.pos += len(data)

    def finish(self):
        return self.holes


class StreamCheck:

    def __init__(self, stream_checker=STREAM_CHECKER):
        self.stream_checker = stream_checker
        self.proc = subprocess.Popen([stream_checker, "view", "-c", "-"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.fed = 0
        self.error = None

    def feed(self, data):
        if self.error:
            return
        try:
            self.proc.stdin.write(data)
            self.fed += len(data)
        except (BrokenPipeError, ValueError, OSError) as e:
            self.error = f"stream_checker 管道提前关闭（疑数据损坏）: {e!r}"

    def finish(self, timeout=1800):
        try:
            _out, err = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.communicate()
            return "stream_checker 收尾超时"
        if self.error:
            return self.error
        rc = self.proc.returncode
        errs = (err or b"").decode("utf-8", "replace")
        if rc != 0:
            return f"stream_checker view -c 失败 rc={rc}: {errs.strip()[:200]}"
        if re.search(r"CRC32|mismatch|inflate|truncated|EOF marker", errs, re.I):
            return f"stream_checker 报完整性错误: {errs.strip()[:200]}"
        return None


def verify_file(path, expect_md5=None, use_checker=True, quiet=False, bufsize=8 * MIB,
                expect_trailer=True):
    v = GzipStreamVerifier()
    z = ZeroRunScanner()
    md5 = hashlib.md5()
    sam = StreamCheck() if (use_checker and path.endswith(".bin")) else None
    t_feed = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            md5.update(b)
            z.feed(b)
            v.feed(b)
            if sam:
                sam.feed(b)
                t_feed += len(b)
    errs = []
    e = v.finish(expect_trailer=expect_trailer)
    if e:
        errs.append(f"[gzip] {e}")
    holes = z.finish()
    if holes:
        errs.append(f"[zero] {len(holes)} 处 >=64KiB 零串，首处偏移 {holes[0]}")
    if sam:
        e = sam.finish()
        if e:
            errs.append(f"[stream_checker] {e}")
    got = md5.hexdigest()
    if expect_md5 and got != expect_md5.lower():
        errs.append(f"[md5] {got} != 官方 {expect_md5}")
    if not quiet:
        print(f"文件 {path}")
        print(f"  大小 {os.path.getsize(path):,} B | 实占 {blocks_used(path):,} B")
        print(f"  gzip={'是' if v.is_gzip else '否'}"
              f"{'（gzip container）' if v.is_container else ''} | member {v.n_members} 个 "
              f"| 已验前缀 {v.verified_hi:,} B / 喂入 {v.fed:,} B")
        print(f"  md5 {got}")
        print("  结果 " + ("✅ 全绿" if not errs else "❌ " + "；".join(errs)))
    return errs, got


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--expect-md5")
    ap.add_argument("--no-check", action="store_true", help="跳过 stream_checker 管道检查")
    ap.add_argument("--no-trailer", action="store_true", help="不要求尾部 gzip container EOF 块")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    errs, _ = verify_file(args.file, args.expect_md5, not args.no_check,
                          args.quiet, expect_trailer=not args.no_trailer)
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
