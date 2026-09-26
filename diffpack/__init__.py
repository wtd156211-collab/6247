"""diffpack：二进制补丁生成与应用（纯标准库）。"""
from .engine import Segment, generate
from .patchfile import Reject, apply_patch, parse_patch, patch_size, write_patch
from .report import build_report, write_report

__all__ = [
    "Segment",
    "generate",
    "Reject",
    "apply_patch",
    "parse_patch",
    "patch_size",
    "write_patch",
    "build_report",
    "write_report",
]

__version__ = "1"
