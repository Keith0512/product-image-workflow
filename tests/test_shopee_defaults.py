"""Synthetic checks for Shopee output limits and opt-in MOMO auditing."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "shopee-image-batch" / "scripts"))
from shopee_tools import audit


class ShopeeDefaultsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for folder, prefix in (("1_主圖", "主圖"), ("2_選項圖", "選項"),
                               ("3_詳情頁", "詳情"), ("4_S詳情頁", "S詳情")):
            directory = self.root / folder
            directory.mkdir()
            Image.new("RGB", (1000, 1000), "white").save(directory / f"{prefix}_01.jpg")
        for name in ("處理報告.html", "圖片來源對應.csv", "文字轉換對照.csv", "預覽總覽.jpg"):
            (self.root / name).touch()

    def test_full_audit_does_not_require_m_or_create_optional_outputs(self):
        checked = audit(self.root, sku_count=1)
        self.assertEqual(checked["issues"], [])
        self.assertEqual(len(checked["files"]), 4)
        self.assertTrue(all(row["status"] == "FILE_OK" for row in checked["files"]))
        self.assertFalse((self.root / "5_M詳情頁").exists())
        self.assertFalse((self.root / "8_MOMO專用").exists())

    def test_explicit_m_audits_still_require_m_output(self):
        for kwargs in ({"include_m": True}, {"mode": "m"}):
            with self.subTest(**kwargs):
                checked = audit(self.root, sku_count=1, **kwargs)
                self.assertEqual(checked["status"], "FAIL")
                self.assertTrue(any("5_M詳情頁必要輸出缺失" in row["reason"]
                                    for row in checked["issues"]))

    def test_s_ratio_uses_width_divided_by_height(self):
        path = self.root / "4_S詳情頁/S詳情_01.jpg"
        for height, accepted in ((31, False), (32, True), (500, True),
                                 (2000, True), (2001, False), (32000, False)):
            with self.subTest(height=height):
                Image.new("RGB", (1000, height), "white").save(path)
                checked = audit(self.root, sku_count=1)
                row = next(row for row in checked["files"] if row["path"] == str(path.relative_to(self.root)))
                self.assertEqual(row["status"], "FILE_OK" if accepted else "REVIEW")
                self.assertEqual(any("S寬高比不符" in issue for issue in row["issues"]), not accepted)

    def test_s_count_still_allows_twelve_and_flags_thirteen(self):
        directory = self.root / "4_S詳情頁"
        image = Image.new("RGB", (1000, 1000), "white")
        for n in range(2, 13):
            image.save(directory / f"S詳情_{n:02d}.jpg")
        self.assertEqual(audit(self.root, sku_count=1)["issues"], [])
        image.save(directory / "S詳情_13.jpg")
        checked = audit(self.root, sku_count=1)
        self.assertTrue(any("4_S詳情頁超限：13>12" in row["reason"] for row in checked["issues"]))
        self.assertEqual(sum(row["path"].startswith("4_S詳情頁/") for row in checked["files"]), 13)

    def test_cli_defaults_to_shopee_and_supports_explicit_m(self):
        script = Path(__file__).resolve().parents[1] / "skills/shopee-image-batch/scripts/shopee_tools.py"
        command = [sys.executable, str(script), "audit", str(self.root), "--sku-count", "1"]
        default = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(default.returncode, 0, default.stderr + default.stdout)
        self.assertEqual(json.loads(default.stdout)["issues"], [])
        with_m = subprocess.run(command + ["--include-m"], capture_output=True, text=True, check=False)
        self.assertEqual(with_m.returncode, 2, with_m.stderr + with_m.stdout)
        self.assertTrue(any("5_M詳情頁必要輸出缺失" in row["reason"]
                            for row in json.loads(with_m.stdout)["issues"]))


if __name__ == "__main__":
    unittest.main()
