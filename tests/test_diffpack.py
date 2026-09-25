"""diffpack 验收测试：只读 samples/，输出写临时目录。"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
PAIRS = os.path.join(SAMPLES, "pairs")
EXPECTED = os.path.join(SAMPLES, "expected")

sys.path.insert(0, ROOT)

from diffpack import engine  # noqa: E402


def read_expected(case):
    values = {}
    with open(os.path.join(EXPECTED, case + ".txt"), encoding="utf-8") as f:
        for line in f:
            key, _, val = line.strip().partition(" ")
            values[key] = val
    return values


def case_names():
    return sorted(
        name for name in os.listdir(PAIRS)
        if os.path.isdir(os.path.join(PAIRS, name))
    )


def sha256_path(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RoundTripTest(unittest.TestCase):
    """每对样例：apply(旧, diff(旧, 新)) 与 新 逐字节相同，补丁不超上界。"""

    def test_pairs(self):
        for case in case_names():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                old = os.path.join(PAIRS, case, "old")
                new = os.path.join(PAIRS, case, "new")
                patch = os.path.join(td, "case.patch")
                out = os.path.join(td, "out")
                exp = read_expected(case)
                stats = engine.generate(old, new, patch)
                self.assertLessEqual(stats["patch_bytes"], int(exp["patch-max"]),
                                     "补丁超过 patch-max")
                engine.apply_patch(old, patch, out)
                with open(out, "rb") as f:
                    applied = f.read()
                self.assertEqual(len(applied), int(exp["result-size"]))
                self.assertEqual(hashlib.sha256(applied).hexdigest(),
                                 exp["result-sha256"])
                with open(new, "rb") as f:
                    self.assertEqual(applied, f.read(), "应用结果与新文件不一致")


class DeterminismTest(unittest.TestCase):
    """同一对文件跑两遍 diff，补丁逐字节一样。"""

    def test_diff_is_deterministic(self):
        for case in case_names():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                old = os.path.join(PAIRS, case, "old")
                new = os.path.join(PAIRS, case, "new")
                digests = []
                for i in range(2):
                    patch = os.path.join(td, "p%d.patch" % i)
                    engine.generate(old, new, patch)
                    digests.append(sha256_path(patch))
                self.assertEqual(digests[0], digests[1])


class RejectionTest(unittest.TestCase):
    """三个坏补丁按 expected/rejections.txt 给出类别，且不写输出文件。"""

    def test_bad_patches_via_cli(self):
        with open(os.path.join(EXPECTED, "rejections.txt"),
                  encoding="utf-8") as f:
            rows = [line.split() for line in f if line.strip()]
        self.assertEqual(len(rows), 3)
        for patch_name, old_rel, category in rows:
            with self.subTest(patch=patch_name), \
                    tempfile.TemporaryDirectory() as td:
                out = os.path.join(td, "out")
                proc = subprocess.run(
                    [sys.executable, "-m", "diffpack", "apply",
                     os.path.join(SAMPLES, old_rel),
                     os.path.join(SAMPLES, "bad-patches", patch_name),
                     out],
                    cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 3, proc.stderr)
                self.assertTrue(proc.stderr.startswith(category + " "),
                                proc.stderr)
                self.assertIn(patch_name, proc.stderr)
                self.assertFalse(os.path.exists(out), "拒绝时不许写输出文件")

    def test_bad_patches_via_library(self):
        with open(os.path.join(EXPECTED, "rejections.txt"),
                  encoding="utf-8") as f:
            rows = [line.split() for line in f if line.strip()]
        for patch_name, old_rel, category in rows:
            with self.subTest(patch=patch_name), \
                    tempfile.TemporaryDirectory() as td:
                out = os.path.join(td, "out")
                with self.assertRaises(engine.Reject) as ctx:
                    engine.apply_patch(
                        os.path.join(SAMPLES, old_rel),
                        os.path.join(SAMPLES, "bad-patches", patch_name),
                        out)
                self.assertEqual(ctx.exception.category, category)
                self.assertFalse(os.path.exists(out))


class CliTest(unittest.TestCase):
    """命令行：diff 的 JSON 输出、apply 往返、退出码。"""

    def test_diff_json_and_apply(self):
        with tempfile.TemporaryDirectory() as td:
            old = os.path.join(PAIRS, "inplace", "old")
            new = os.path.join(PAIRS, "inplace", "new")
            patch = os.path.join(td, "inplace.patch")
            out = os.path.join(td, "out")
            proc = subprocess.run(
                [sys.executable, "-m", "diffpack", "diff", old, new, patch],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            stats = json.loads(proc.stdout)
            self.assertEqual(list(stats), ["patch_bytes", "blocks",
                                           "copy_bytes", "insert_bytes",
                                           "gen_sec"])
            exp = read_expected("inplace")
            self.assertLessEqual(stats["patch_bytes"], int(exp["patch-max"]))
            self.assertEqual(stats["patch_bytes"], os.path.getsize(patch))
            proc = subprocess.run(
                [sys.executable, "-m", "diffpack", "apply", old, patch, out],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(sha256_path(out), exp["result-sha256"])

    def test_argument_errors(self):
        for argv in ([], ["diff"], ["apply", "a"], ["report", "a", "b", "c"],
                     ["report", "a", "b", "c", "--patch-max", "x"],
                     ["nope", "a", "b", "c"]):
            with self.subTest(argv=argv):
                proc = subprocess.run(
                    [sys.executable, "-m", "diffpack"] + argv,
                    cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 2, proc.stderr)


class ReportTest(unittest.TestCase):
    """报告：五项齐全、补丁口径与 diff 一致、两遍除耗时外逐字节相同。"""

    def test_report(self):
        with tempfile.TemporaryDirectory() as td:
            old = os.path.join(PAIRS, "inplace", "old")
            new = os.path.join(PAIRS, "inplace", "new")
            patch = os.path.join(td, "inplace.patch")
            stats = engine.generate(old, new, patch)
            pages = []
            for name in ("r1.html", "r2.html"):
                proc = subprocess.run(
                    [sys.executable, "-m", "diffpack", "report", old, new,
                     os.path.join(td, name), "--patch-max", "2048"],
                    cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                with open(os.path.join(td, name), encoding="utf-8") as f:
                    pages.append(f.read())
            page = pages[0]
            self.assertIn("<!DOCTYPE html>", page)
            self.assertNotIn("<script", page)
            self.assertIn("old", page)  # 文件名（不含绝对路径）
            self.assertNotIn(PAIRS, page)
            self.assertIn(stats["old_sha256"], page)
            self.assertIn(stats["new_sha256"], page)
            self.assertIn("%d 字节" % stats["patch_bytes"], page)
            self.assertIn("2048 字节", page)
            self.assertIn("达标", page)
            self.assertIn("%d</td>" % stats["blocks"], page)
            self.assertEqual(page.count("通过"), 3)
            self.assertIn("原样复制", page)
            self.assertIn("被删掉", page)
            self.assertIn("新增", page)
            strip_time = lambda s: re.sub(r"\d+\.\d{3} 秒", "T 秒", s)
            self.assertEqual(strip_time(pages[0]), strip_time(pages[1]))


class BulkBudgetTest(unittest.TestCase):
    """bulk 样例：diff <= 60 秒、apply <= 30 秒、tracemalloc 峰值 <= 48 MiB。"""

    def test_budget(self):
        old = os.path.join(PAIRS, "bulk", "old")
        new = os.path.join(PAIRS, "bulk", "new")
        with tempfile.TemporaryDirectory() as td:
            patch = os.path.join(td, "bulk.patch")
            out = os.path.join(td, "out")
            tracemalloc.start()
            t0 = time.perf_counter()
            engine.generate(old, new, patch)
            gen_sec = time.perf_counter() - t0
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertLessEqual(peak, 48 * 1024 * 1024,
                                 "生成端峰值内存 %.1f MiB" % (peak / 2**20))
            self.assertLessEqual(gen_sec, 60)
            t0 = time.perf_counter()
            engine.apply_patch(old, patch, out)
            self.assertLessEqual(time.perf_counter() - t0, 30)
            self.assertEqual(sha256_path(out),
                             read_expected("bulk")["result-sha256"])


if __name__ == "__main__":
    unittest.main()
