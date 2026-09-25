"""静态 HTML 补丁报告：两条块级条带 + 补丁大小 + 耗时 + 校验结果。

单文件、无外部资源、无脚本；不写时刻、随机数、绝对路径，
同一对文件跑两遍，除生成耗时数字外逐字节相同。
"""

from __future__ import annotations

import html
import os
import shutil
import tempfile

from . import engine

_STRIP_BLOCKS = 512  # 条带目标块数（块长由此推出，见 README 2.4）


def _strip_flags(size, intervals, block_len):
    """每个块一个布尔：是否与任一给定区间有交。"""
    count = -(-size // block_len) if size else 0
    flags = [False] * count
    for start, length in intervals:
        first = start // block_len
        last = (start + length - 1) // block_len
        for j in range(first, min(last, count - 1) + 1):
            flags[j] = True
    return flags


def _strip_html(flags, on_class):
    cells = "".join(
        '<div class="%s"></div>' % (on_class if flag else "off")
        for flag in flags
    )
    if not cells:
        cells = '<div class="off"></div>'
    return '<div class="strip">%s</div>' % cells


def _pct(part, whole):
    return "%.1f" % (100.0 * part / whole) if whole else "0.0"


def generate_report(old_path, new_path, html_path, patch_max):
    """内部先跑一遍生成，把补丁画成静态 HTML 写进 html_path。"""
    tmpdir = tempfile.mkdtemp(prefix="diffpack-report-")
    try:
        patch_path = os.path.join(tmpdir, "patch")
        out_path = os.path.join(tmpdir, "out")
        stats = engine.generate(old_path, new_path, patch_path)
        parsed = engine.parse_patch(patch_path)
        baseline_ok = engine.check_baseline(old_path, parsed)
        crc_ok = engine.check_segment_crcs(old_path, patch_path, parsed)
        try:
            engine.apply_patch(old_path, patch_path, out_path)
            apply_ok = engine._sha256_path(out_path) == parsed.new_sha
        except engine.Reject:
            apply_ok = False

        old_name = html.escape(os.path.basename(old_path))
        new_name = html.escape(os.path.basename(new_path))
        old_size = stats["old_size"]
        new_size = stats["new_size"]
        block_len = max(1, -(-max(old_size, new_size) // _STRIP_BLOCKS))

        copy_intervals = [(s[2], s[3]) for s in stats["segments"] if s[0] == "C"]
        insert_intervals = [(s[1], s[3]) for s in stats["segments"] if s[0] == "I"]
        old_flags = _strip_flags(old_size, copy_intervals, block_len)
        new_flags = _strip_flags(new_size, insert_intervals, block_len)
        old_copied = sum(old_flags)
        new_inserted = sum(new_flags)

        within = stats["patch_bytes"] <= patch_max
        size_verdict = "达标" if within else "超标"

        def ok_line(flag):
            return "通过" if flag else "不通过"

        page = _TEMPLATE.format(
            old_name=old_name,
            new_name=new_name,
            old_size=old_size,
            new_size=new_size,
            old_sha=stats["old_sha256"],
            new_sha=stats["new_sha256"],
            block_len=block_len,
            old_blocks=len(old_flags),
            new_blocks=len(new_flags),
            old_strip=_strip_html(old_flags, "copy"),
            new_strip=_strip_html(new_flags, "ins"),
            old_copy_pct=_pct(old_copied, len(old_flags)),
            old_del_pct=_pct(len(old_flags) - old_copied, len(old_flags)),
            new_copy_pct=_pct(len(new_flags) - new_inserted, len(new_flags)),
            new_ins_pct=_pct(new_inserted, len(new_flags)),
            patch_bytes=stats["patch_bytes"],
            patch_max=patch_max,
            size_verdict=size_verdict,
            blocks=stats["blocks"],
            gen_sec="%.3f" % stats["gen_sec"],
            baseline_line=ok_line(baseline_ok),
            crc_line=ok_line(crc_ok),
            apply_line=ok_line(apply_ok),
        )
        with open(html_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(page)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>补丁报告 {old_name} → {new_name}</title>
<style>
body {{ font-family: "PingFang SC", "Microsoft YaHei", sans-serif; margin: 2em; color: #222; }}
h1 {{ font-size: 1.4em; }}
h2 {{ font-size: 1.1em; margin-top: 1.6em; }}
table {{ border-collapse: collapse; }}
td, th {{ border: 1px solid #ccc; padding: 4px 10px; text-align: left; }}
.strip {{ display: flex; height: 28px; border: 1px solid #999; margin: 6px 0 2px 0; }}
.strip div {{ flex: 1 1 auto; }}
.copy {{ background: #4d9e57; }}
.ins {{ background: #f0a030; }}
.off {{ background: #d8d8d8; }}
.legend span {{ display: inline-block; width: 12px; height: 12px; margin: 0 4px 0 14px; vertical-align: -1px; }}
.mono {{ font-family: ui-monospace, Consolas, monospace; font-size: 0.92em; word-break: break-all; }}
.verdict-ok {{ color: #2c7a34; font-weight: bold; }}
</style>
</head>
<body>
<h1>补丁报告</h1>
<h2>文件</h2>
<table>
<tr><th></th><th>文件名</th><th>字节数</th><th>sha256</th></tr>
<tr><td>旧文件</td><td>{old_name}</td><td>{old_size}</td><td class="mono">{old_sha}</td></tr>
<tr><td>新文件</td><td>{new_name}</td><td>{new_size}</td><td class="mono">{new_sha}</td></tr>
</table>
<h2>块级条带</h2>
<p>块长 {block_len} 字节；老带 {old_blocks} 块，新带 {new_blocks} 块。块级划分与本次生成的补丁段表一致。</p>
<p>老带（原样复制 {old_copy_pct}% / 被删掉 {old_del_pct}%）</p>
{old_strip}
<p class="legend"><span class="copy"></span>原样复制<span class="off"></span>被删掉</p>
<p>新带（原样复制 {new_copy_pct}% / 新增 {new_ins_pct}%）</p>
{new_strip}
<p class="legend"><span class="copy"></span>原样复制<span class="ins"></span>新增</p>
<h2>补丁</h2>
<table>
<tr><th>补丁大小</th><td>{patch_bytes} 字节</td></tr>
<tr><th>大小上界</th><td>{patch_max} 字节</td></tr>
<tr><th>结论</th><td class="verdict-ok">{size_verdict}</td></tr>
<tr><th>段数</th><td>{blocks}</td></tr>
<tr><th>生成耗时</th><td>{gen_sec} 秒</td></tr>
</table>
<h2>校验结果</h2>
<table>
<tr><td>基线 sha256</td><td>{baseline_line}</td></tr>
<tr><td>逐段 crc32</td><td>{crc_line}</td></tr>
<tr><td>应用结果与 new-sha256</td><td>{apply_line}</td></tr>
</table>
</body>
</html>
"""
