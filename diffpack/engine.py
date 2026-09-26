"""段查找引擎：在旧文件中为新文件找可复制区间，产出规范形段表。

内存口径：两份输入都用 mmap 惰性分页，索引条目数封顶 MAX_INDEX，
因此 tracemalloc 额外峰值不随输入变大。
"""
from __future__ import annotations

import hashlib
import mmap
import os
import zlib
from collections import namedtuple

WINDOW = 64        # 锚点窗口字节数
MAX_INDEX = 131072  # 旧文件索引条目上限（内存上界的来源）
CHUNK = 1 << 18    # 扩展比较 / 校验的分块大小（256 KiB）

Segment = namedtuple("Segment", "kind new_start old_start length crc32")


def open_ro(path):
    """只读打开文件，返回 (文件对象, 可切片缓冲)。空文件退回 b''。"""
    f = open(path, "rb")
    if os.fstat(f.fileno()).st_size == 0:
        return f, b""
    return f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)


def close_ro(f, buf):
    if isinstance(buf, mmap.mmap):
        buf.close()
    f.close()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def crc32_region(buf, start, length):
    crc = 0
    end = start + length
    while start < end:
        n = min(CHUNK, end - start)
        crc = zlib.crc32(buf[start:start + n], crc)
        start += n
    return crc


def _build_index(old, old_size):
    """旧文件按 stride 抽样建索引：窗口前 8 字节 -> 首个旧偏移。"""
    index = {}
    if old_size < WINDOW:
        return index, 0
    stride = 16
    while old_size // stride > MAX_INDEX:
        stride *= 2
    for q in range(0, old_size - WINDOW + 1, stride):
        key = int.from_bytes(old[q:q + 8], "little")
        if key not in index:
            index[key] = q
    return index, stride


def _extend_fwd(old, new, oi, ni, old_end, new_end):
    """从锚点向后扩展，返回匹配区间末端（开区间）。"""
    while oi < old_end and ni < new_end:
        n = min(CHUNK, old_end - oi, new_end - ni)
        oc = old[oi:oi + n]
        nc = new[ni:ni + n]
        if oc == nc:
            oi += n
            ni += n
        else:
            i = 0
            while oc[i] == nc[i]:
                i += 1
            return oi + i, ni + i
    return oi, ni


def _extend_back(old, new, oi, ni, new_lo):
    """从锚点向前扩展，返回匹配区间起点。new_lo 为已覆盖边界。"""
    while ni > new_lo and oi > 0:
        n = min(CHUNK, ni - new_lo, oi)
        oc = old[oi - n:oi]
        nc = new[ni - n:ni]
        if oc == nc:
            oi -= n
            ni -= n
        else:
            i = n - 1
            while oc[i] == nc[i]:
                i -= 1
            return oi - n + (i + 1), ni - n + (i + 1)
    return oi, ni


def _match(old, old_size, new, new_size):
    """扫描新文件找锚点并扩展，产出有序的 C 段列表 [(new_start, old_start, len)]。"""
    index, _ = _build_index(old, old_size)
    raw = []
    covered = 0
    p = 0
    while p + WINDOW <= new_size:
        key = int.from_bytes(new[p:p + 8], "little")
        q = index.get(key)
        if q is not None and old[q:q + WINDOW] == new[p:p + WINDOW]:
            os_, ns = _extend_back(old, new, q, p, covered)
            _oe, ne = _extend_fwd(old, new, q + WINDOW, p + WINDOW, old_size, new_size)
            raw.append((ns, os_, ne - ns))
            covered = ne
            p = ne
        else:
            p += 1
    return raw


def _normalize(raw, new_size):
    """补 I 段填空隙；相邻 C 段在旧文件里连续时合并（规范形）。"""
    segs = []
    pos = 0
    for ns, os_, ln in raw:
        if ns > pos:
            segs.append(["I", pos, 0, ns - pos])
        if segs and segs[-1][0] == "C" and segs[-1][1] + segs[-1][2] == os_:
            segs[-1][2] += ln
        else:
            segs.append(["C", ns, os_, ln])
        pos = ns + ln
    if pos < new_size:
        segs.append(["I", pos, 0, new_size - pos])
    return segs


def _fill_crc(segs, old, new):
    out = []
    for kind, ns, os_, ln in segs:
        if kind == "C":
            crc = crc32_region(old, os_, ln)
            out.append(Segment("C", ns, os_, ln, crc))
        else:
            crc = crc32_region(new, ns, ln)
            out.append(Segment("I", ns, 0, ln, crc))
    return out


def generate(old_path, new_path):
    """生成规范形段表。返回 (segments, old_size, old_sha, new_size, new_sha)。"""
    old_size = os.path.getsize(old_path)
    new_size = os.path.getsize(new_path)
    old_sha = sha256_file(old_path)
    new_sha = sha256_file(new_path)
    of, old = open_ro(old_path)
    nf, new = open_ro(new_path)
    try:
        raw = _match(old, old_size, new, new_size)
        segs = _normalize(raw, new_size)
        segments = _fill_crc(segs, old, new)
    finally:
        close_ro(of, old)
        close_ro(nf, new)
    return segments, old_size, old_sha, new_size, new_sha
