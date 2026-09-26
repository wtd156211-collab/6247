"""命令行入口：python -m diffpack <diff|apply|report> ...

退出码：0 成功、2 参数错、3 拒绝、1 其它失败。
"""
from __future__ import annotations

import argparse
import sys
import time

from . import engine, patchfile, report


def _cmd_diff(args):
    t0 = time.perf_counter()
    segments, old_size, old_sha, new_size, new_sha = engine.generate(
        args.old, args.new)
    nf, new = engine.open_ro(args.new)
    try:
        patch_bytes = patchfile.write_patch(
            args.patch, segments, new, old_size, old_sha, new_size, new_sha)
    finally:
        engine.close_ro(nf, new)
    gen_sec = time.perf_counter() - t0
    copy_bytes = sum(s.length for s in segments if s.kind == "C")
    insert_bytes = sum(s.length for s in segments if s.kind == "I")
    print('{"patch_bytes": %d, "blocks": %d, "copy_bytes": %d, '
          '"insert_bytes": %d, "gen_sec": %.3f}'
          % (patch_bytes, len(segments), copy_bytes, insert_bytes, gen_sec))
    return 0


def _cmd_apply(args):
    try:
        patchfile.apply_patch(args.old, args.patch, args.out)
    except patchfile.Reject as r:
        print("%s %s: %s" % (r.category, args.patch, r), file=sys.stderr)
        return 3
    return 0


def _cmd_report(args):
    report.write_report(args.old, args.new, args.html, args.patch_max)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="diffpack")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("diff", help="生成补丁")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("patch")
    p.set_defaults(func=_cmd_diff)

    p = sub.add_parser("apply", help="应用补丁")
    p.add_argument("old")
    p.add_argument("patch")
    p.add_argument("out")
    p.set_defaults(func=_cmd_apply)

    p = sub.add_parser("report", help="生成静态 HTML 报告")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("html")
    p.add_argument("--patch-max", type=int, required=True)
    p.set_defaults(func=_cmd_report)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except patchfile.Reject as r:
        print("%s: %s" % (r.category, r), file=sys.stderr)
        return 3
    except OSError as e:
        print("IO-ERROR: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
