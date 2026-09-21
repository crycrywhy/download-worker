#!/usr/bin/env python3
"""验证分块文件的内容完整性（严格：逐 gzip 成员解压 + 手动 CRC32 检查）。
从候选 magic 开始验证成员链，链必须覆盖 >=99% 数据。
OK 退出 0，损坏退出 1"""
import struct, zlib, sys, re

def verify(path):
    with open(path, 'rb') as f:
        data = f.read()
    if len(data) < 18:
        return True
    magics = [m.start() for m in re.finditer(b'\x1f\x8b', data)]
    total_len = len(data)
    for start_magic in magics[:100]:
        pos = start_magic
        chain_ok = True
        while pos + 18 <= total_len:
            if data[pos:pos+2] != b'\x1f\x8b':
                chain_ok = False
                break
            bsize = struct.unpack('<H', data[pos+16:pos+18])[0]
            be = pos + bsize + 1
            if be > total_len:
                break  # 末尾截断允许
            blk = data[pos:be]
            try:
                decompressed = zlib.decompress(blk[18:be-pos-8], -15)
            except Exception:
                chain_ok = False
                break
            # 手动验证 CRC32（块尾 4 字节）
            crc_stored = struct.unpack('<I', blk[-8:-4])[0]
            crc_calc = zlib.crc32(decompressed) & 0xffffffff
            if crc_stored != crc_calc:
                chain_ok = False
                break
            pos = be
        if chain_ok:
            covered = pos - start_magic
            if covered >= total_len * 0.99:
                return True
    return False

if verify(sys.argv[1]):
    sys.exit(0)
else:
    sys.exit(1)
