"""静态 HTML 报告：两条块级条带 + 补丁大小 + 生成耗时 + 校验结果。

块级划分与引擎实际生成的段表完全一致（同一次 generate 的产物）。
单文件、无外部资源、无脚本，双击可开；不含时刻、随机数、绝对路径，
同一对文件跑两遍，除生成耗时数字外逐字节相同。
"""
from __future__ import annotations

import hashlib
import html
import os
import time

from . import engine, patchfile

MAX_BLOCKS = 512


def _band_flags(size, block_len, intervals):
    """每个块是否与任一区间有交。intervals 为 [(start, end), ...]。"""
    if size <= 0:
        return []
    count = -(-size // block_len)
    flags = []
    for j in range(count):
        b0 = j * block_len
        b1 = min((j + 1) * block_len, size)
        flags.append(any(s < b1 and b0 < e for s, e in intervals))
    return flags


def _band_html(flags, cls_hit, cls_miss):
    cells = "".join(
        '<i class="%s"></i>' % (cls_hit if f else cls_miss) for f in flags
    )
    return '<div class="band">%s</div>' % cells


def _pct(part, whole):
    return "%.1f" % (100.0 * part / whole) if whole else "0.0"


def build_report(old_path, new_path, patch_max):
    """生成报告内容（HTML 字符串）。"""
    t0 = time.perf_counter()
    segments, old_size, old_sha, new_size, new_sha = engine.generate(old_path, new_path)
    psize = patchfile.patch_size(segments, old_size, old_sha, new_size, new_sha)
    gen_sec = time.perf_counter() - t0

    # 校验结果：基线 sha256、逐段 crc32、应用结果与 new-sha256
    base_ok = engine.sha256_file(old_path) == old_sha
    of, old = engine.open_ro(old_path)
    nf, new = engine.open_ro(new_path)
    try:
        crc_ok = True
        h = hashlib.sha256()
        for s in segments:
            if s.kind == "C":
                if engine.crc32_region(old, s.old_start, s.length) != s.crc32:
                    crc_ok = False
                end = s.old_start + s.length
                pos = s.old_start
                while pos < end:
                    n = min(engine.CHUNK, end - pos)
                    h.update(old[pos:pos + n])
                    pos += n
            else:
                if engine.crc32_region(new, s.new_start, s.length) != s.crc32:
                    crc_ok = False
                end = s.new_start + s.length
                pos = s.new_start
                while pos < end:
                    n = min(engine.CHUNK, end - pos)
                    h.update(new[pos:pos + n])
                    pos += n
        apply_ok = h.hexdigest() == new_sha
    finally:
        engine.close_ro(of, old)
        engine.close_ro(nf, new)

    # 条带：块长与块数按 §2.4 口径
    block_len = max(1, -(-max(old_size, new_size) // MAX_BLOCKS))
    old_intervals = [(s.old_start, s.old_start + s.length) for s in segments if s.kind == "C"]
    new_intervals = [(s.new_start, s.new_start + s.length) for s in segments if s.kind == "I"]
    old_flags = _band_flags(old_size, block_len, old_intervals)
    new_flags = _band_flags(new_size, block_len, new_intervals)
    old_copied = sum(old_flags)
    new_added = sum(new_flags)

    old_name = html.escape(os.path.basename(old_path))
    new_name = html.escape(os.path.basename(new_path))
    size_ok = psize <= patch_max
    verdict = "达标" if size_ok else "超标"
    pass_word = {True: "通过", False: "不通过"}

    css = (
        "body{font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;"
        "margin:2em auto;max-width:960px;color:#222}"
        "h1{font-size:1.4em}h2{font-size:1.1em;margin-top:1.6em}"
        "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 10px;"
        "text-align:left;font-size:.9em}"
        "td.hash{font-family:ui-monospace,monospace;font-size:.78em;word-break:break-all}"
        ".band{display:flex;height:30px;border:1px solid #999;margin:6px 0}"
        ".band i{flex:1 1 auto;margin:0;padding:0}"
        ".cp{background:#3d9d55}.dl{background:#c9c9c9}.ad{background:#e8833a}"
        ".legend span{display:inline-block;width:12px;height:12px;margin:0 4px 0 14px;"
        "vertical-align:-1px}"
        ".ok{color:#2c7a3f;font-weight:600}.bad{color:#b3261e;font-weight:600}"
        ".meta{color:#555;font-size:.85em}"
    )

    parts = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="zh-CN"><head><meta charset="utf-8">')
    parts.append("<title>补丁报告 %s → %s</title>" % (old_name, new_name))
    parts.append("<style>%s</style></head><body>" % css)
    parts.append("<h1>补丁报告</h1>")

    parts.append("<h2>文件</h2><table>")
    parts.append("<tr><th></th><th>文件名</th><th>字节数</th><th>sha256</th></tr>")
    parts.append("<tr><td>旧文件</td><td>%s</td><td>%d</td><td class='hash'>%s</td></tr>"
                 % (old_name, old_size, old_sha))
    parts.append("<tr><td>新文件</td><td>%s</td><td>%d</td><td class='hash'>%s</td></tr>"
                 % (new_name, new_size, new_sha))
    parts.append("</table>")

    parts.append("<h2>块级条带</h2>")
    parts.append('<p class="meta">块长 %d 字节；老带 %d 块，新带 %d 块。'
                 "块级划分与本次生成的补丁段表一致。</p>"
                 % (block_len, len(old_flags), len(new_flags)))
    parts.append("<p>老带（旧文件）：原样复制 %s%% / 被删掉 %s%%"
                 % (_pct(old_copied, len(old_flags)),
                    _pct(len(old_flags) - old_copied, len(old_flags))))
    parts.append('<span class="legend"><span class="cp"></span>原样复制'
                 '<span class="dl"></span>被删掉</span></p>')
    parts.append(_band_html(old_flags, "cp", "dl"))
    parts.append("<p>新带（新文件）：原样复制 %s%% / 新增 %s%%"
                 % (_pct(len(new_flags) - new_added, len(new_flags)),
                    _pct(new_added, len(new_flags))))
    parts.append('<span class="legend"><span class="cp"></span>原样复制'
                 '<span class="ad"></span>新增</span></p>')
    parts.append(_band_html(new_flags, "ad", "cp"))

    parts.append("<h2>补丁</h2><table>")
    parts.append("<tr><th>补丁大小</th><th>上界 (--patch-max)</th><th>结论</th>"
                 "<th>段数</th><th>生成耗时</th></tr>")
    parts.append("<tr><td>%d 字节</td><td>%d 字节</td>"
                 '<td class="%s">%s</td><td>%d</td><td>%.3f 秒</td></tr>'
                 % (psize, patch_max, "ok" if size_ok else "bad", verdict,
                    len(segments), gen_sec))
    parts.append("</table>")

    parts.append("<h2>校验结果</h2><table>")
    for label, ok in (
        ("基线 sha256", base_ok),
        ("逐段 crc32", crc_ok),
        ("应用结果与 new-sha256", apply_ok),
    ):
        parts.append('<tr><td>%s</td><td class="%s">%s</td></tr>'
                     % (label, "ok" if ok else "bad", pass_word[ok]))
    parts.append("</table>")

    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def write_report(old_path, new_path, html_path, patch_max):
    content = build_report(old_path, new_path, patch_max)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(content)
    return content
