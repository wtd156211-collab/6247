"""补丁文件格式：序列化、解析、校验、应用。

布局（ASCII 头部 6 行 + 段表 K 行 + 字面量区原始字节）：

    DIFFPACK 1
    old-size <十进制>
    old-sha256 <64 位小写十六进制>
    new-size <十进制>
    new-sha256 <64 位小写十六进制>
    blocks <K>
    C <new-start> <old-start> <len> <crc32>
    I <new-start> <len> <crc32>
    <字面量字节，按 I 段先后首尾相接>

应用端三道闸门（顺序固定）：① 结构完整（缺字节 -> TRUNCATED）
② 基线匹配（old-size / old-sha256 -> BASELINE-MISMATCH）
③ 校验（逐段 crc32 + new-sha256 -> CHECKSUM-MISMATCH）。
三道闸门都过才写输出。
"""
from __future__ import annotations

import hashlib
import os

from .engine import CHUNK, Segment, close_ro, crc32_region, open_ro, sha256_file

MAGIC = "DIFFPACK 1"


class Reject(Exception):
    """补丁被拒绝。category 为 stderr 行首类别。"""

    def __init__(self, category, message):
        super().__init__(message)
        self.category = category


def header_bytes(old_size, old_sha, new_size, new_sha, segments):
    lines = [
        MAGIC,
        "old-size %d" % old_size,
        "old-sha256 %s" % old_sha,
        "new-size %d" % new_size,
        "new-sha256 %s" % new_sha,
        "blocks %d" % len(segments),
    ]
    for s in segments:
        if s.kind == "C":
            lines.append("C %d %d %d %08x" % (s.new_start, s.old_start, s.length, s.crc32))
        else:
            lines.append("I %d %d %08x" % (s.new_start, s.length, s.crc32))
    return ("\n".join(lines) + "\n").encode("ascii")


def patch_size(segments, old_size, old_sha, new_size, new_sha):
    head = header_bytes(old_size, old_sha, new_size, new_sha, segments)
    return len(head) + sum(s.length for s in segments if s.kind == "I")


def write_patch(path, segments, new, old_size, old_sha, new_size, new_sha):
    """把段表写成补丁文件。new 为新文件的可切片缓冲。返回补丁字节数。"""
    head = header_bytes(old_size, old_sha, new_size, new_sha, segments)
    total = len(head)
    with open(path, "wb") as f:
        f.write(head)
        for s in segments:
            if s.kind != "I":
                continue
            pos = s.new_start
            end = pos + s.length
            while pos < end:
                n = min(CHUNK, end - pos)
                f.write(new[pos:pos + n])
                pos += n
            total += s.length
    return total


def _parse_int(text, what):
    try:
        value = int(text)
    except ValueError:
        raise Reject("INVALID-PATCH", "%s 不是合法十进制数" % what)
    if value < 0:
        raise Reject("INVALID-PATCH", "%s 不能为负" % what)
    return value


def _parse_crc(text):
    if len(text) != 8:
        raise Reject("INVALID-PATCH", "crc32 字段应为 8 位十六进制")
    try:
        return int(text, 16)
    except ValueError:
        raise Reject("INVALID-PATCH", "crc32 字段不是十六进制")


def parse_patch(path):
    """闸门①：读满头部与段表，核对字面量区长度。

    返回 (old_size, old_sha, new_size, new_sha, segments, literal_offset)。
    segments 为 Segment 列表（I 段 old_start 字段借作字面量区内的相对偏移）。
    """
    f = open(path, "rb")
    with f:
        def line():
            raw = f.readline()
            if not raw.endswith(b"\n"):
                raise Reject("TRUNCATED", "头部或段表读到一半没有字节了")
            try:
                return raw[:-1].decode("ascii")
            except UnicodeDecodeError:
                raise Reject("INVALID-PATCH", "头部或段表含非 ASCII 字节")

        if line() != MAGIC:
            raise Reject("INVALID-PATCH", "缺少 DIFFPACK 1 魔数")

        def field(prefix):
            text = line()
            if not text.startswith(prefix):
                raise Reject("INVALID-PATCH", "期望 %s 行" % prefix.strip())
            return text[len(prefix):]

        old_size = _parse_int(field("old-size "), "old-size")
        old_sha = field("old-sha256 ")
        if len(old_sha) != 64:
            raise Reject("INVALID-PATCH", "old-sha256 长度不对")
        new_size = _parse_int(field("new-size "), "new-size")
        new_sha = field("new-sha256 ")
        if len(new_sha) != 64:
            raise Reject("INVALID-PATCH", "new-sha256 长度不对")
        blocks = _parse_int(field("blocks "), "blocks")

        segments = []
        pos = 0
        lit_rel = 0
        for _ in range(blocks):
            parts = line().split(" ")
            if parts[0] == "C" and len(parts) == 5:
                ns = _parse_int(parts[1], "new-start")
                os_ = _parse_int(parts[2], "old-start")
                ln = _parse_int(parts[3], "len")
                crc = _parse_crc(parts[4])
                if ln < 1:
                    raise Reject("INVALID-PATCH", "C 段 len < 1")
                if os_ + ln > old_size:
                    raise Reject("INVALID-PATCH", "C 段来源区间越出旧文件")
                segments.append(Segment("C", ns, os_, ln, crc))
            elif parts[0] == "I" and len(parts) == 4:
                ns = _parse_int(parts[1], "new-start")
                ln = _parse_int(parts[2], "len")
                crc = _parse_crc(parts[3])
                if ln < 1:
                    raise Reject("INVALID-PATCH", "I 段 len < 1")
                segments.append(Segment("I", ns, lit_rel, ln, crc))
                lit_rel += ln
            else:
                raise Reject("INVALID-PATCH", "段表行无法识别")
            if segments[-1].new_start != pos:
                raise Reject("INVALID-PATCH", "段表 new-start 不连续")
            pos += segments[-1].length
        if pos != new_size:
            raise Reject("INVALID-PATCH", "段表总长与 new-size 不符")

        lit_off = f.tell()
        file_size = os.fstat(f.fileno()).st_size
        if file_size < lit_off + lit_rel:
            raise Reject("TRUNCATED", "字面量区不足 %d 字节" % lit_rel)
        if file_size > lit_off + lit_rel:
            raise Reject("INVALID-PATCH", "补丁尾部多出字节")
    return old_size, old_sha, new_size, new_sha, segments, lit_off


def _hash_region(h, buf, start, length):
    end = start + length
    while start < end:
        n = min(CHUNK, end - start)
        h.update(buf[start:start + n])
        start += n


def _write_region(f, buf, start, length):
    end = start + length
    while start < end:
        n = min(CHUNK, end - start)
        f.write(buf[start:start + n])
        start += n


def apply_patch(old_path, patch_path, out_path):
    """应用补丁。三道闸门任一不过则抛 Reject，且不写输出文件。"""
    old_size, old_sha, new_size, new_sha, segments, lit_off = parse_patch(patch_path)

    # 闸门②：基线匹配
    if os.path.getsize(old_path) != old_size:
        raise Reject("BASELINE-MISMATCH", "旧文件大小与补丁声明的 old-size 不符")
    if sha256_file(old_path) != old_sha:
        raise Reject("BASELINE-MISMATCH", "旧文件 sha256 与补丁声明的基线版本不符")

    # 闸门③：逐段 crc32 + 整体 new-sha256，全部通过才写输出
    of, old = open_ro(old_path)
    pf, pbuf = open_ro(patch_path)
    try:
        h = hashlib.sha256()
        for s in segments:
            if s.kind == "C":
                if crc32_region(old, s.old_start, s.length) != s.crc32:
                    raise Reject("CHECKSUM-MISMATCH", "C 段 crc32 不符 (new-start %d)" % s.new_start)
                _hash_region(h, old, s.old_start, s.length)
            else:
                base = lit_off + s.old_start  # old_start 字段借作字面量区相对偏移
                if crc32_region(pbuf, base, s.length) != s.crc32:
                    raise Reject("CHECKSUM-MISMATCH", "I 段 crc32 不符 (new-start %d)" % s.new_start)
                _hash_region(h, pbuf, base, s.length)
        if h.hexdigest() != new_sha:
            raise Reject("CHECKSUM-MISMATCH", "应用结果 sha256 与 new-sha256 不符")

        with open(out_path, "wb") as out:
            for s in segments:
                if s.kind == "C":
                    _write_region(out, old, s.old_start, s.length)
                else:
                    _write_region(out, pbuf, lit_off + s.old_start, s.length)
    finally:
        close_ro(of, old)
        close_ro(pf, pbuf)
    return new_size
