"""diffpack 验收测试。只读 samples/，临时产物写系统临时目录。"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tracemalloc
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
PAIRS = os.path.join(SAMPLES, "pairs")
EXPECTED = os.path.join(SAMPLES, "expected")

sys.path.insert(0, ROOT)

from diffpack import engine, patchfile  # noqa: E402


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_expected(case):
    out = {}
    with open(os.path.join(EXPECTED, case + ".txt"), encoding="utf-8") as f:
        for line in f:
            key, value = line.split(None, 1)
            out[key] = value.strip()
    return out


def run_cli(*argv):
    return subprocess.run(
        [sys.executable, "-m", "diffpack", *argv],
        cwd=ROOT, capture_output=True, text=True)


def case_names():
    return sorted(os.listdir(PAIRS))


class RoundTripTest(unittest.TestCase):
    """每对样例：apply(旧, diff(旧, 新)) 与新文件逐字节一致，补丁不超 patch-max。"""

    def test_roundtrip_all_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            for case in case_names():
                with self.subTest(case=case):
                    exp = read_expected(case)
                    old = os.path.join(PAIRS, case, "old")
                    new = os.path.join(PAIRS, case, "new")
                    patch = os.path.join(tmp, case + ".patch")
                    out = os.path.join(tmp, case + ".out")
                    segments, osz, osha, nsz, nsha = engine.generate(old, new)
                    nf, nbuf = engine.open_ro(new)
                    try:
                        patchfile.write_patch(patch, segments, nbuf, osz, osha, nsz, nsha)
                    finally:
                        engine.close_ro(nf, nbuf)
                    self.assertLessEqual(os.path.getsize(patch), int(exp["patch-max"]))
                    patchfile.apply_patch(old, patch, out)
                    self.assertEqual(os.path.getsize(out), int(exp["result-size"]))
                    self.assertEqual(sha256_of(out), exp["result-sha256"])
                    with open(new, "rb") as f1, open(out, "rb") as f2:
                        self.assertEqual(f1.read(), f2.read())


class CliTest(unittest.TestCase):
    def test_diff_json_line(self):
        exp = read_expected("inplace")
        with tempfile.TemporaryDirectory() as tmp:
            patch = os.path.join(tmp, "p.patch")
            r = run_cli("diff", os.path.join(PAIRS, "inplace", "old"),
                        os.path.join(PAIRS, "inplace", "new"), patch)
            self.assertEqual(r.returncode, 0, r.stderr)
            line = r.stdout.strip()
            self.assertTrue(line.startswith('{"patch_bytes": '))
            stats = json.loads(line)
            self.assertEqual(list(stats), ["patch_bytes", "blocks", "copy_bytes",
                                           "insert_bytes", "gen_sec"])
            self.assertEqual(stats["patch_bytes"], os.path.getsize(patch))
            self.assertLessEqual(stats["patch_bytes"], int(exp["patch-max"]))
            self.assertRegex(line, r'"gen_sec": \d+\.\d{3}\}$')

    def test_apply_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch = os.path.join(tmp, "p.patch")
            out = os.path.join(tmp, "o.out")
            r = run_cli("diff", os.path.join(PAIRS, "tiny", "old"),
                        os.path.join(PAIRS, "tiny", "new"), patch)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = run_cli("apply", os.path.join(PAIRS, "tiny", "old"), patch, out)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(sha256_of(out), read_expected("tiny")["result-sha256"])

    def test_arg_error_exit_2(self):
        r = run_cli("diff", "only-one-arg")
        self.assertEqual(r.returncode, 2)


class RejectTest(unittest.TestCase):
    """三个坏补丁按 expected/rejections.txt 拒绝，且不写输出文件。"""

    def test_bad_patches(self):
        with open(os.path.join(EXPECTED, "rejections.txt"), encoding="utf-8") as f:
            rows = [line.split() for line in f if line.strip()]
        self.assertEqual(len(rows), 3)
        with tempfile.TemporaryDirectory() as tmp:
            for patch_name, old_rel, category in sorted(rows):
                with self.subTest(patch=patch_name):
                    out = os.path.join(tmp, patch_name + ".out")
                    r = run_cli("apply", os.path.join(SAMPLES, old_rel),
                                os.path.join(SAMPLES, "bad-patches", patch_name), out)
                    self.assertEqual(r.returncode, 3, r.stderr)
                    self.assertTrue(r.stderr.startswith(category + " "), r.stderr)
                    self.assertIn(patch_name, r.stderr)
                    self.assertFalse(os.path.exists(out))

    def test_trailing_garbage_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch = os.path.join(tmp, "p.patch")
            out = os.path.join(tmp, "o.out")
            r = run_cli("diff", os.path.join(PAIRS, "tiny", "old"),
                        os.path.join(PAIRS, "tiny", "new"), patch)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(patch, "ab") as f:
                f.write(b"x")
            r = run_cli("apply", os.path.join(PAIRS, "tiny", "old"), patch, out)
            self.assertEqual(r.returncode, 3)
            self.assertFalse(os.path.exists(out))


class DeterminismTest(unittest.TestCase):
    """同一对文件跑两遍 diff，补丁逐字节一致。"""

    def test_diff_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            for case in ("shift", "bulk"):
                with self.subTest(case=case):
                    old = os.path.join(PAIRS, case, "old")
                    new = os.path.join(PAIRS, case, "new")
                    p1 = os.path.join(tmp, case + ".1.patch")
                    p2 = os.path.join(tmp, case + ".2.patch")
                    self.assertEqual(run_cli("diff", old, new, p1).returncode, 0)
                    self.assertEqual(run_cli("diff", old, new, p2).returncode, 0)
                    self.assertEqual(sha256_of(p1), sha256_of(p2))

    def test_report_deterministic_except_gen_sec(self):
        with tempfile.TemporaryDirectory() as tmp:
            h1 = os.path.join(tmp, "r1.html")
            h2 = os.path.join(tmp, "r2.html")
            old = os.path.join(PAIRS, "inplace", "old")
            new = os.path.join(PAIRS, "inplace", "new")
            self.assertEqual(run_cli("report", old, new, h1,
                                     "--patch-max", "2048").returncode, 0)
            self.assertEqual(run_cli("report", old, new, h2,
                                     "--patch-max", "2048").returncode, 0)
            mask = re.compile(rb"\d+\.\d{3} \xe7\xa7\x92")  # "x.xxx 秒"
            with open(h1, "rb") as f:
                c1 = mask.sub(b"SEC", f.read())
            with open(h2, "rb") as f:
                c2 = mask.sub(b"SEC", f.read())
            self.assertEqual(c1, c2)


class ReportTest(unittest.TestCase):
    def test_report_contents(self):
        exp = read_expected("inplace")
        with tempfile.TemporaryDirectory() as tmp:
            html_path = os.path.join(tmp, "patch-map.html")
            old = os.path.join(PAIRS, "inplace", "old")
            new = os.path.join(PAIRS, "inplace", "new")
            r = run_cli("report", old, new, html_path, "--patch-max", "2048")
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(html_path, encoding="utf-8") as f:
                page = f.read()
            # 1. 文件名（不含绝对路径）、字节数、两个 sha256
            self.assertIn("<td>old</td>", page)
            self.assertIn("<td>new</td>", page)
            self.assertNotIn(ROOT, page)
            self.assertIn("65536", page)
            self.assertIn("07e9fdb95d3c3f539b996929a0736287c8751e1de3ecb56e194a52542c4d8c5a", page)
            self.assertIn(exp["result-sha256"], page)
            # 2. 两条横带与图例
            self.assertEqual(page.count('class="band"'), 2)
            self.assertIn("原样复制", page)
            self.assertIn("被删掉", page)
            self.assertIn("新增", page)
            self.assertIn("块长 128 字节", page)
            # 3. 补丁大小与上界、达标结论（与单独 diff 一致）
            self.assertIn("413 字节", page)
            self.assertIn("2048 字节", page)
            self.assertIn("达标", page)
            # 4. 生成耗时
            self.assertRegex(page, r"\d+\.\d{3} 秒")
            # 5. 校验结果三行
            self.assertIn("基线 sha256", page)
            self.assertIn("逐段 crc32", page)
            self.assertIn("应用结果与 new-sha256", page)
            self.assertEqual(page.count("通过"), 3)
            # 无脚本、无外部资源
            self.assertNotIn("<script", page)
            self.assertNotIn("http://", page)
            self.assertNotIn("https://", page)


class BudgetTest(unittest.TestCase):
    """bulk 样例的内存与时间预算。"""

    def test_memory_peak(self):
        old = os.path.join(PAIRS, "bulk", "old")
        new = os.path.join(PAIRS, "bulk", "new")
        with tempfile.TemporaryDirectory() as tmp:
            tracemalloc.start()
            try:
                segments, osz, osha, nsz, nsha = engine.generate(old, new)
                nf, nbuf = engine.open_ro(new)
                try:
                    patchfile.write_patch(os.path.join(tmp, "b.patch"), segments,
                                          nbuf, osz, osha, nsz, nsha)
                finally:
                    engine.close_ro(nf, nbuf)
                _cur, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertLessEqual(peak, 48 * 1024 * 1024)

    def test_time_budget(self):
        import time
        old = os.path.join(PAIRS, "bulk", "old")
        new = os.path.join(PAIRS, "bulk", "new")
        with tempfile.TemporaryDirectory() as tmp:
            patch = os.path.join(tmp, "b.patch")
            out = os.path.join(tmp, "b.out")
            t0 = time.perf_counter()
            self.assertEqual(run_cli("diff", old, new, patch).returncode, 0)
            self.assertLessEqual(time.perf_counter() - t0, 60)
            t0 = time.perf_counter()
            self.assertEqual(run_cli("apply", old, patch, out).returncode, 0)
            self.assertLessEqual(time.perf_counter() - t0, 30)


if __name__ == "__main__":
    unittest.main()
