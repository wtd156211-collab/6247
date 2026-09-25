"""补丁引擎：生成（diff）与应用（apply）。

格式与口径见 README.md 第 2、3 节；匹配策略见第 8 节。
全程流式读写，两份输入都只做 mmap 只读映射，不整个读进内存。
"""

from __future__ import annotations

import array
import hashlib
import mmap
import os
import time
import zlib

MAGIC = b"DIFFPACK 1"
MIN_BLOCK = 64               # 最小匹配窗口（字节）
MAX_INDEX_BLOCKS = 1 << 20   # 旧文件索引块数上限（内存不随输入变大）
IDX_BITS = 21                # 索引槽低位留给块号的位数（块号 + 1）
CHUNK = 1 << 20              # 流式读写 / 比较的分片大小


class Reject(Exception):
    """补丁被拒绝；category 为拒绝类别。"""

    def __init__(self, category, detail):
        super().__init__(detail)
        self.category = category
        self.detail = detail


def choose_block_size(old_size):
    """匹配窗口 B：>= ceil(old-size / 2^20) 的最小 2 的幂，且至少 64。"""
    need = -(-old_size // MAX_INDEX_BLOCKS)
    block = MIN_BLOCK
    while block < need:
        block <<= 1
    return block


class _Source:
    """只读映射的文件；空文件退化为 b""。"""

    def __init__(self, path):
        self.path = path
        self.size = os.path.getsize(path)
        self._f = open(path, "rb")
        if self.size:
            self.data = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        else:
            self.data = b""

    def close(self):
        if self.size:
            self.data.close()
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _sha256_path(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _crc32_span(data, start, length):
    crc = 0
    end = start + length
    while start < end:
        step = min(CHUNK, end - start)
        crc = zlib.crc32(data[start:start + step], crc)
        start += step
    return crc


def _build_index(data, size, block):
    """旧文件对齐块 crc32 -> 块号 的开地址哈希表；同 crc 只留最早块号。"""
    nblocks = size // block
    slots = 1 << 12
    while slots < nblocks * 2:
        slots <<= 1
    table = array.array("Q", bytes(8 * slots))
    mask = slots - 1
    for i in range(nblocks):
        off = i * block
        crc = zlib.crc32(data[off:off + block])
        slot = crc & mask
        while True:
            entry = table[slot]
            if entry == 0:
                table[slot] = (crc << IDX_BITS) | (i + 1)
                break
            if (entry >> IDX_BITS) == crc:
                break
            slot = (slot + 1) & mask
    return table, mask


def _lookup(table, mask, crc):
    slot = crc & mask
    while True:
        entry = table[slot]
        if entry == 0:
            return None
        if (entry >> IDX_BITS) == crc:
            return (entry & ((1 << IDX_BITS) - 1)) - 1
        slot = (slot + 1) & mask


def _common_prefix(a, a0, b, b0, limit):
    """a[a0:] 与 b[b0:] 的最长公共前缀，不超过 limit；分片比较，C 速度。"""
    n = 0
    while n < limit:
        step = min(CHUNK, limit - n)
        if a[a0 + n:a0 + n + step] != b[b0 + n:b0 + n + step]:
            lo, hi = 0, step
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if a[a0 + n:a0 + n + mid] == b[b0 + n:b0 + n + mid]:
                    lo = mid
                else:
                    hi = mid
            return n + lo
        n += step
    return n


def _common_suffix(a, a_end, b, b_end, limit):
    """a[:a_end] 与 b[:b_end] 的最长公共后缀，不超过 limit。"""
    n = 0
    while n < limit:
        step = min(CHUNK, limit - n)
        if a[a_end - n - step:a_end - n] != b[b_end - n - step:b_end - n]:
            lo, hi = 0, step
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if a[a_end - n - mid:a_end - n] == b[b_end - n - mid:b_end - n]:
                    lo = mid
                else:
                    hi = mid
            return n + lo
        n += step
    return n


def _merge(segs):
    """规范形：相邻 C 段在旧文件里也连续时并成一段。"""
    merged = []
    for kind, ns, os_, ln in segs:
        if (merged and kind == "C" and merged[-1][0] == "C"
                and merged[-1][2] + merged[-1][3] == os_):
            prev = merged[-1]
            merged[-1] = ("C", prev[1], prev[2], prev[3] + ln)
        else:
            merged.append((kind, ns, os_, ln))
    return merged


def compute_segments(old, old_size, new, new_size):
    """扫描新文件，产出段表 [(kind, new-start, old-start|None, len)]。"""
    block = choose_block_size(old_size)
    table, mask = _build_index(old, old_size, block)
    segs = []
    prev = 0
    p = 0
    while p + block <= new_size:
        idx = _lookup(table, mask, zlib.crc32(new[p:p + block]))
        if idx is not None:
            off = idx * block
            if old[off:off + block] == new[p:p + block]:
                back = _common_suffix(old, off, new, p, min(off, p - prev))
                new_start = p - back
                old_start = off - back
                limit = min(old_size - old_start, new_size - new_start)
                length = _common_prefix(old, old_start, new, new_start, limit)
                if new_start > prev:
                    segs.append(("I", prev, None, new_start - prev))
                segs.append(("C", new_start, old_start, length))
                prev = new_start + length
                p = prev
                continue
        p += 1
    if prev < new_size:
        segs.append(("I", prev, None, new_size - prev))
    return _merge(segs)


def _write_patch(f, segs, old, new, old_sha, new_sha):
    f.write(MAGIC + b"\n")
    f.write(b"old-size %d\n" % old.size)
    f.write(b"old-sha256 %s\n" % old_sha.encode("ascii"))
    f.write(b"new-size %d\n" % new.size)
    f.write(b"new-sha256 %s\n" % new_sha.encode("ascii"))
    f.write(b"blocks %d\n" % len(segs))
    for kind, ns, os_, ln in segs:
        if kind == "C":
            crc = _crc32_span(old.data, os_, ln)
            f.write(b"C %d %d %d %08x\n" % (ns, os_, ln, crc))
        else:
            crc = _crc32_span(new.data, ns, ln)
            f.write(b"I %d %d %08x\n" % (ns, ln, crc))
    for kind, ns, os_, ln in segs:
        if kind == "I":
            start = ns
            end = ns + ln
            while start < end:
                step = min(CHUNK, end - start)
                f.write(new.data[start:start + step])
                start += step


def generate(old_path, new_path, patch_path):
    """生成补丁并写出，返回统计与段表。"""
    t0 = time.perf_counter()
    with _Source(old_path) as old, _Source(new_path) as new:
        old_sha = _sha256_path(old_path)
        new_sha = _sha256_path(new_path)
        segs = compute_segments(old.data, old.size, new.data, new.size)
        with open(patch_path, "wb") as f:
            _write_patch(f, segs, old, new, old_sha, new_sha)
    return {
        "patch_bytes": os.path.getsize(patch_path),
        "blocks": len(segs),
        "copy_bytes": sum(s[3] for s in segs if s[0] == "C"),
        "insert_bytes": sum(s[3] for s in segs if s[0] == "I"),
        "gen_sec": time.perf_counter() - t0,
        "segments": segs,
        "old_size": old.size,
        "new_size": new.size,
        "old_sha256": old_sha,
        "new_sha256": new_sha,
    }


class Parsed:
    __slots__ = ("old_size", "old_sha", "new_size", "new_sha",
                 "blocks", "segments", "literal_offset")

    def __init__(self, old_size, old_sha, new_size, new_sha,
                 blocks, segments, literal_offset):
        self.old_size = old_size
        self.old_sha = old_sha
        self.new_size = new_size
        self.new_sha = new_sha
        self.blocks = blocks
        self.segments = segments          # (kind, new-start, old-start|None, len, crc)
        self.literal_offset = literal_offset


def _read_line(f):
    line = f.readline()
    if not line or not line.endswith(b"\n"):
        raise Reject("TRUNCATED", "头部或段表读到一半没有字节了")
    return line[:-1]


def _parse_dec(tok, what):
    if not tok or not tok.isdigit() or (len(tok) > 1 and tok[:1] == b"0"):
        raise Reject("INVALID", "%s 不是合法十进制数" % what)
    return int(tok)


def _parse_hex(tok, length, what):
    if len(tok) != length or any(c not in b"0123456789abcdef" for c in tok):
        raise Reject("INVALID", "%s 不是 %d 位小写十六进制" % (what, length))
    return tok.decode("ascii")


def _kv(line, key):
    parts = line.split(b" ")
    if len(parts) != 2 or parts[0] != key:
        raise Reject("INVALID", "头部行格式不符：期望 %s" % key.decode("ascii"))
    return parts[1]


def parse_patch(path):
    """闸门 1：读满头部与段表，按声明核对字面量区长度与文件尾。"""
    with open(path, "rb") as f:
        if _read_line(f) != MAGIC:
            raise Reject("INVALID", "魔数不符")
        old_size = _parse_dec(_kv(_read_line(f), b"old-size"), "old-size")
        old_sha = _parse_hex(_kv(_read_line(f), b"old-sha256"), 64, "old-sha256")
        new_size = _parse_dec(_kv(_read_line(f), b"new-size"), "new-size")
        new_sha = _parse_hex(_kv(_read_line(f), b"new-sha256"), 64, "new-sha256")
        blocks = _parse_dec(_kv(_read_line(f), b"blocks"), "blocks")
        segments = []
        expect = 0
        literal_bytes = 0
        for _ in range(blocks):
            parts = _read_line(f).split(b" ")
            if parts[0] == b"C" and len(parts) == 5:
                ns = _parse_dec(parts[1], "new-start")
                os_ = _parse_dec(parts[2], "old-start")
                ln = _parse_dec(parts[3], "len")
                crc = int(_parse_hex(parts[4], 8, "crc32"), 16)
                if ln < 1:
                    raise Reject("INVALID", "段长小于 1")
                if os_ + ln > old_size:
                    raise Reject("INVALID", "C 段来源越出旧文件")
                segments.append(("C", ns, os_, ln, crc))
            elif parts[0] == b"I" and len(parts) == 4:
                ns = _parse_dec(parts[1], "new-start")
                ln = _parse_dec(parts[2], "len")
                crc = int(_parse_hex(parts[3], 8, "crc32"), 16)
                if ln < 1:
                    raise Reject("INVALID", "段长小于 1")
                segments.append(("I", ns, None, ln, crc))
                literal_bytes += ln
            else:
                raise Reject("INVALID", "段表行格式不符")
            if ns != expect:
                raise Reject("INVALID", "段起点与前一段之尾不衔接")
            expect += ln
        if expect != new_size:
            raise Reject("INVALID", "段长之和与 new-size 不符")
        literal_offset = f.tell()
        remaining = literal_bytes
        while remaining:
            chunk = f.read(min(CHUNK, remaining))
            if not chunk:
                raise Reject("TRUNCATED", "字面量区不足声明的 %d 字节" % literal_bytes)
            remaining -= len(chunk)
        if f.read(1):
            raise Reject("INVALID", "补丁尾部多出字节")
    return Parsed(old_size, old_sha, new_size, new_sha,
                  blocks, segments, literal_offset)


def check_baseline(old_path, parsed):
    """闸门 2：old-size 与 old-sha256 跟实参旧文件一致。"""
    return (os.path.getsize(old_path) == parsed.old_size
            and _sha256_path(old_path) == parsed.old_sha)


def check_segment_crcs(old_path, patch_path, parsed):
    """逐段重算 crc32，与段表声明比对。"""
    with _Source(old_path) as old, open(patch_path, "rb") as pf:
        pf.seek(parsed.literal_offset)
        for kind, _ns, os_, ln, crc in parsed.segments:
            if kind == "C":
                actual = _crc32_span(old.data, os_, ln)
            else:
                actual = 0
                remaining = ln
                while remaining:
                    chunk = pf.read(min(CHUNK, remaining))
                    actual = zlib.crc32(chunk, actual)
                    remaining -= len(chunk)
            if actual != crc:
                return False
    return True


def apply_patch(old_path, patch_path, out_path):
    """三道闸门全过才把拼回结果落盘；拒绝时不写输出文件。"""
    parsed = parse_patch(patch_path)
    if not check_baseline(old_path, parsed):
        raise Reject("BASELINE-MISMATCH",
                     "旧文件的大小或 sha256 与补丁声明的基线不符")
    tmp_path = "%s.tmp-%d" % (out_path, os.getpid())
    try:
        with _Source(old_path) as old, \
                open(patch_path, "rb") as pf, \
                open(tmp_path, "wb") as out:
            pf.seek(parsed.literal_offset)
            digest = hashlib.sha256()
            for i, (kind, _ns, os_, ln, crc) in enumerate(parsed.segments):
                crc_actual = 0
                pos = os_ if kind == "C" else 0
                remaining = ln
                while remaining:
                    step = min(CHUNK, remaining)
                    if kind == "C":
                        chunk = old.data[pos:pos + step]
                        pos += step
                    else:
                        chunk = pf.read(step)
                    crc_actual = zlib.crc32(chunk, crc_actual)
                    digest.update(chunk)
                    out.write(chunk)
                    remaining -= step
                if crc_actual != crc:
                    raise Reject("CHECKSUM-MISMATCH",
                                 "第 %d 段 crc32 与声明不符" % i)
            if digest.hexdigest() != parsed.new_sha:
                raise Reject("CHECKSUM-MISMATCH",
                             "拼回结果的 sha256 与 new-sha256 不符")
        os.replace(tmp_path, out_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
