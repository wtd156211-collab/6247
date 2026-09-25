"""命令行入口：python -m diffpack <diff|apply|report> ...

退出码：0 成功、2 参数错、3 拒绝、1 其它失败。
"""

import sys

from . import engine
from .report import generate_report

USAGE = (
    "用法:\n"
    "  python -m diffpack diff   <旧文件> <新文件> <补丁文件>\n"
    "  python -m diffpack apply  <旧文件> <补丁文件> <输出文件>\n"
    "  python -m diffpack report <旧文件> <新文件> <报告文件> --patch-max <字节>\n"
)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stderr.write(USAGE)
        return 2
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "diff" and len(rest) == 3:
            stats = engine.generate(rest[0], rest[1], rest[2])
            sys.stdout.write(
                '{"patch_bytes": %d, "blocks": %d, "copy_bytes": %d, '
                '"insert_bytes": %d, "gen_sec": %.3f}\n'
                % (stats["patch_bytes"], stats["blocks"], stats["copy_bytes"],
                   stats["insert_bytes"], stats["gen_sec"]))
            return 0
        if cmd == "apply" and len(rest) == 3:
            engine.apply_patch(rest[0], rest[1], rest[2])
            return 0
        if cmd == "report" and len(rest) == 5 and rest[3] == "--patch-max":
            try:
                patch_max = int(rest[4])
            except ValueError:
                patch_max = -1
            if patch_max < 0:
                sys.stderr.write(USAGE)
                return 2
            generate_report(rest[0], rest[1], rest[2], patch_max)
            return 0
        sys.stderr.write(USAGE)
        return 2
    except engine.Reject as rej:
        patch_path = rest[1] if cmd == "apply" and len(rest) >= 2 else ""
        sys.stderr.write("%s %s: %s\n" % (rej.category, patch_path, rej.detail))
        return 3
    except OSError as exc:
        sys.stderr.write("ERROR %s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
