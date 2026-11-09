import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "product-image-workflow" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
from build_compare_preview import ASSET_DIR, INDEX_FILE, PREVIEW_NAME, build


class ComparisonPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = self.root / '原始 & "1".jpg'
        self.second = self.root / "原始2.png"
        self.remade = self.root / "重製.webp"
        Image.new("RGB", (23, 17), "red").save(self.original)
        Image.new("RGB", (16, 31), "green").save(self.second)
        Image.new("RGB", (40, 20), "blue").save(self.remade)
        self.plan = {"title": "帳篷</script><script>錯誤標記</script>", "groups": [
            {"id": "main", "label": "主圖", "original": [self.second.name, self.original.name],
             "remade": [str(self.remade)]},
            {"id": "detail", "label": "詳情頁", "original": [self.original.name],
             "remade": [self.remade.name]},
        ]}
        self.plan_path = self.root / "計畫.json"
        self.output = self.root / "輸出"

    def write_plan(self, plan=None):
        self.plan_path.write_text(json.dumps(self.plan if plan is None else plan, ensure_ascii=False),
                                  encoding="utf-8-sig")
        return self.plan_path

    def test_unequal_counts_keep_order_bytes_and_portable_paths(self):
        result = build(self.write_plan(), self.output)
        html = (self.output / PREVIEW_NAME).read_text(encoding="utf-8")
        payload = re.search(r'<script type="application/json" id="comparison-data">(.*?)</script>',
                            html, re.S).group(1)
        data = json.loads(payload)
        self.assertEqual(data["title"], self.plan["title"])
        self.assertNotIn("</script><script>錯誤標記", html)
        self.assertNotIn(str(self.root), html)
        self.assertEqual([g["id"] for g in data["groups"]], ["main", "detail"])
        self.assertEqual([i["name"] for i in data["groups"][0]["original"]],
                         [self.second.name, self.original.name])
        self.assertEqual(result["groups"][0], {"id": "main", "label": "主圖", "original": 2, "remade": 1})
        self.assertEqual(len(result["images"]), 5)
        for item in result["images"]:
            source = Path(item["source_path"])
            snapshot = self.output / item["src"]
            self.assertFalse(Path(item["src"]).is_absolute())
            self.assertEqual(snapshot.read_bytes(), source.read_bytes())
            self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), item["sha256"])
        self.assertEqual(json.loads((self.output / INDEX_FILE).read_text())["visual_status"],
                         "NOT_VERIFIED_BY_TOOL")

    def test_cover_only_does_not_create_unrequested_detail_group(self):
        self.plan["groups"] = self.plan["groups"][:1]
        result = build(self.write_plan(), self.output)
        self.assertEqual([g["id"] for g in result["groups"]], ["main"])

    def test_exif_orientation_uses_display_dimensions_without_changing_bytes(self):
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (23, 17), "red").save(self.original, exif=exif)
        result = build(self.write_plan(), self.output)
        item = next(i for i in result["images"] if i["name"] == self.original.name)
        self.assertEqual((item["width"], item["height"]), (17, 23))
        self.assertEqual((self.output / item["src"]).read_bytes(), self.original.read_bytes())

    def test_existing_output_is_not_overwritten(self):
        build(self.write_plan(), self.output)
        before = {p: p.read_bytes() for p in self.output.rglob("*") if p.is_file()}
        with self.assertRaises(FileExistsError):
            build(self.plan_path, self.output)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_existing_partial_assets_or_index_are_also_protected(self):
        for relative in (ASSET_DIR, INDEX_FILE):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                target = output / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if relative == ASSET_DIR:
                    target.mkdir()
                else:
                    target.write_text("previous index")
                with self.assertRaises(FileExistsError):
                    build(self.write_plan(), output)
                self.assertFalse((output / PREVIEW_NAME).exists())

    def test_bad_last_image_fails_preflight_without_partial_preview(self):
        for bad in ("不存在.jpg", "損壞.jpg", "不支援.gif"):
            with self.subTest(bad=bad):
                (self.root / "損壞.jpg").write_bytes(b"not an image")
                Image.new("RGB", (8, 8)).save(self.root / "不支援.gif")
                plan = copy.deepcopy(self.plan)
                plan["groups"][-1]["remade"].append(bad)
                with self.assertRaises((OSError, ValueError)):
                    build(self.write_plan(plan), self.output)
                self.assertFalse(self.output.exists())

    def test_invalid_plan_has_no_partial_output(self):
        duplicate = copy.deepcopy(self.plan)
        duplicate["groups"][1]["id"] = "main"
        empty_side = copy.deepcopy(self.plan)
        empty_side["groups"][1]["remade"] = []
        traversal = copy.deepcopy(self.plan)
        traversal["groups"][0]["id"] = "../main"
        for plan in ([], {"groups": []}, duplicate, empty_side, traversal):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                build(self.write_plan(plan), self.output)
            self.assertFalse(self.output.exists())

    def test_cli_reports_failure_and_returns_nonzero(self):
        self.plan["groups"][-1]["remade"] = ["不存在.jpg"]
        completed = subprocess.run([sys.executable, str(SCRIPT_DIR / "build_compare_preview.py"),
                                    "--plan", str(self.write_plan()), "--output", str(self.output)],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("比較頁未完成", completed.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
