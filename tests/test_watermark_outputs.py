import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] /
                       "skills" / "shopee-image-batch" / "scripts"))
from shopee_tools import OUTPUTS, audit, inventory, sha256


class WatermarkOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def image(self, path, size=(1000, 1000), color="white"):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, color).save(path)
        return path

    def outputs(self, main_count=2, full=True):
        for index in range(1, main_count + 1):
            self.image(self.root / "1_主圖" / f"主圖_{index:02d}.jpg")
        if full:
            for name, (prefix, _, _) in OUTPUTS.items():
                if name != "1_主圖":
                    self.image(self.root / name / f"{prefix}_01.jpg")
            self.image(self.root / "預覽總覽.jpg")
            for name in ("處理報告.html", "圖片來源對應.csv", "文字轉換對照.csv"):
                (self.root / name).write_text("Synthetic audit fixture", encoding="utf-8")

    def watermark_set(self, name, count=2):
        for index in range(1, count + 1):
            self.image(self.root / "7_浮水印主圖" / name / f"主圖_{index:02d}.jpg",
                       color="gray")

    def test_inventory_excludes_watermark_assets_and_deliveries(self):
        original = self.image(self.root / "商品.jpg")
        for folder in ("浮水印", "浮水印素材", "7_浮水印主圖"):
            self.image(self.root / folder / "店家A" / "watermark.png")
        result = inventory(self.root)
        self.assertEqual([row["relative_path"] for row in result["images"]],
                         [original.name])

    def test_inventory_can_exclude_explicit_loose_watermark(self):
        original = self.image(self.root / "商品.jpg")
        watermark = self.image(self.root / "店家A.png")
        result = inventory(self.root, excludes=[watermark])
        self.assertEqual([row["relative_path"] for row in result["images"]],
                         [original.name])

    def test_two_sets_each_allow_nine_and_audit_preserves_originals(self):
        self.outputs(main_count=9)
        self.watermark_set("店家A", count=9)
        self.watermark_set("店家B", count=9)
        originals = {path: sha256(path) for path in (self.root / "1_主圖").iterdir()}
        result = audit(self.root, sku_count=1, watermark_sets=["店家A", "店家B"])
        self.assertEqual(result["issues"], [])
        self.assertTrue(all(row["status"] == "FILE_OK" for row in result["files"]))
        for name in ("店家A", "店家B"):
            self.assertEqual(sum(name in Path(row["path"]).parts
                                 for row in result["files"]), 9)
        self.assertEqual({path: sha256(path) for path in originals}, originals)
        self.assertEqual(result["visual_status"], "NOT_VERIFIED")

    def test_existing_sets_are_discovered_without_flags(self):
        self.outputs()
        self.watermark_set("店家A")
        result = audit(self.root, sku_count=1)
        self.assertEqual(result["issues"], [])
        self.assertEqual(sum("店家A" in Path(row["path"]).parts
                             for row in result["files"]), 2)

    def test_declared_missing_set_fails(self):
        self.outputs()
        self.watermark_set("店家A")
        result = audit(self.root, sku_count=1, watermark_sets=["店家A", "店家B"])
        self.assertEqual(result["status"], "FAIL")

    def test_incomplete_set_fails(self):
        self.outputs()
        self.watermark_set("店家A", count=1)
        result = audit(self.root, sku_count=1)
        self.assertEqual(result["status"], "FAIL")

    def test_cover_only_scope_keeps_one_image_per_set(self):
        self.outputs()
        self.watermark_set("店家A", count=1)
        result = audit(self.root, sku_count=1, watermark_cover_only=True)
        self.assertEqual(result["issues"], [])

    def test_watermark_geometry_and_size_are_checked(self):
        self.outputs()
        self.watermark_set("店家A")
        bad_size = self.root / "7_浮水印主圖" / "店家A" / "主圖_01.jpg"
        self.image(bad_size, size=(1000, 900))
        oversized = bad_size.with_name("主圖_02.jpg")
        with oversized.open("ab") as handle:
            handle.write(b"\0" * 2000000)
        result = audit(self.root, sku_count=1)
        rows = [row for row in result["files"] if "店家A" in Path(row["path"]).parts]
        self.assertEqual([row["status"] for row in rows], ["REVIEW", "REVIEW"])

    def test_main_mode_supports_standalone_cover(self):
        self.outputs(main_count=1, full=False)
        self.watermark_set("店家A", count=1)
        result = audit(self.root, mode="main", watermark_sets=["店家A"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(len(result["files"]), 2)

    def test_no_watermark_remains_optional(self):
        self.outputs()
        result = audit(self.root, sku_count=1)
        self.assertEqual(result["issues"], [])

    def test_cli_checks_declared_sets_and_cover_only_scope(self):
        self.outputs(full=False)
        self.watermark_set("店家A", count=1)
        script = Path(__file__).resolve().parents[1] / "skills" / "shopee-image-batch" / "scripts" / "shopee_tools.py"
        command = [sys.executable, "-X", "utf8", "-B", str(script), "audit", str(self.root),
                   "--mode", "main", "--watermark-cover-only",
                   "--watermark-set", "店家A", "--watermark-set", "店家B"]
        missing = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(json.loads(missing.stdout)["status"], "FAIL")
        self.watermark_set("店家B", count=1)
        complete = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(complete.returncode, 0, complete.stdout)
        self.assertEqual(json.loads(complete.stdout)["issues"], [])

    def test_set_name_cannot_escape_watermark_directory(self):
        self.outputs()
        with self.assertRaises(ValueError):
            audit(self.root, watermark_sets=["../other"])


if __name__ == "__main__":
    unittest.main()
