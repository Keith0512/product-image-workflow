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
from build_compare_preview import INDEX_ROOT, PREVIEW_NAME, build


def page_data(html):
    payload = re.search(r'<script type="application/json" id="comparison-data">(.*?)</script>',
                        html, re.S).group(1)
    return json.loads(payload)


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
        html = (self.output / result["preview"]).read_text(encoding="utf-8")
        data = page_data(html)
        self.assertEqual(data["title"], self.plan["title"])
        self.assertEqual(data["variants"]["remade"]["label"], "重製版本")
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
        index_file, = (self.output / INDEX_ROOT).glob("*.json")
        self.assertEqual(json.loads(index_file.read_text(encoding="utf-8"))["visual_status"],
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
        result = build(self.write_plan(), self.output)
        asset_dir = Path(result["images"][0]["src"]).parent
        index_file, = (self.output / INDEX_ROOT).glob("*.json")
        for relative in (asset_dir, index_file.relative_to(self.output)):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                target = output / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if relative == asset_dir:
                    target.mkdir()
                else:
                    target.write_text("previous index")
                with self.assertRaises(FileExistsError):
                    build(self.plan_path, output)
                self.assertEqual(list(output.glob("*.html")), [])
                self.assertTrue(target.exists())

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


class ComparisonPreviewHistoryTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.original = self.root / "original.png"
        Image.new("RGB", (4, 3), "white").save(self.original)

    def render(self, title, product_id, version, mode, color, *, status="PASS", category="main",
               source=None, source_version=None):
        if source is None:
            source = self.root / f"{color}.png"
            Image.new("RGB", (4, 3), color).save(source)
        plan = self.root / "preview.json"
        metadata = {"status": status}
        if source_version is not None:
            metadata["version"] = source_version
        plan.write_text(json.dumps({
            "title": title,
            "product_id": product_id,
            "variants": {mode: metadata},
            "groups": [{
                "id": category,
                "label": "主圖" if category == "main" else "詳情頁",
                "original": [self.original.name],
                mode: [str(source)],
            }],
        }, ensure_ascii=False), encoding="utf-8")
        output = self.root / "輸出" / version
        result = build(plan, output)
        self.assertEqual(result["preview"], f"{title}_{PREVIEW_NAME}")
        page = output / result["preview"]
        return page, page_data(page.read_text(encoding="utf-8"))

    def snapshot(self, page, data, mode):
        return page.parent / data["groups"][0][mode][0]["src"]

    def assert_snapshot_color(self, page, data, mode, rgb):
        with Image.open(self.snapshot(page, data, mode)) as image:
            self.assertEqual(image.getpixel((0, 0)), rgb)

    def test_multiple_products_share_a_version_directory(self):
        version = "20261117-v01"
        light, light_data = self.render("露營燈", "light-A", version, "localized", "red")
        chair, chair_data = self.render("折疊椅", "chair-B", version, "localized", "blue")

        self.assertEqual(light.parent, chair.parent)
        self.assertNotEqual(self.snapshot(light, light_data, "localized").parent,
                            self.snapshot(chair, chair_data, "localized").parent)
        self.assert_snapshot_color(light, light_data, "localized", (255, 0, 0))
        self.assert_snapshot_color(chair, chair_data, "localized", (0, 0, 255))
        self.assertEqual(len(list((light.parent / INDEX_ROOT).glob("*.json"))), 2)

    def test_history_selects_latest_exact_product_and_preserves_current_images(self):
        self.render("舊名稱", "light-A", "20261117-v99", "redesigned", "red")
        self.render("舊名稱", "light-A", "20261117-v100", "redesigned", "blue")
        self.render("露營燈", "light-B", "20261117-v101", "redesigned", "yellow")
        page, data = self.render("露營燈", "light-A", "20261117-v102", "localized", "green")

        self.assertEqual(data["variants"]["redesigned"]["version"], "20261117-v100")
        self.assertEqual(data["variants"]["localized"]["version"], "20261117-v102")
        self.assert_snapshot_color(page, data, "redesigned", (0, 0, 255))
        self.assert_snapshot_color(page, data, "localized", (0, 128, 0))
        self.assert_snapshot_color(page, data, "original", (255, 255, 255))

    def test_review_and_bad_hash_fall_back_to_an_earlier_verified_version(self):
        self.render("露營燈", "light-A", "20261117-v01", "redesigned", "red")
        page, data = self.render("露營燈", "light-A", "20261117-v02", "redesigned", "blue")
        Image.new("RGB", (4, 3), "yellow").save(self.snapshot(page, data, "redesigned"))
        self.render("露營燈", "light-A", "20261117-v03", "redesigned", "pink", status="REVIEW")
        page, data = self.render("露營燈", "light-A", "20261117-v04", "localized", "green")

        self.assertEqual(data["variants"]["redesigned"]["version"], "20261117-v01")
        self.assert_snapshot_color(page, data, "redesigned", (255, 0, 0))

    def test_history_needs_a_matching_category(self):
        self.render("露營燈", "light-A", "20261117-v01", "redesigned", "red", category="m-detail")
        _, data = self.render("露營燈", "light-A", "20261117-v02", "localized", "green", category="detail")

        self.assertEqual([group["id"] for group in data["groups"]], ["detail"])
        self.assertNotIn("redesigned", data["variants"])

    def test_history_does_not_treat_an_inherited_copy_as_a_direct_result(self):
        source, _ = self.render("露營燈", "light-A", "20261117-v01", "redesigned", "red")
        _, inherited = self.render("露營燈", "light-A", "20261117-v02", "localized", "green")
        self.assertEqual(inherited["variants"]["redesigned"]["version"], "20261117-v01")
        source.parent.rename(source.parent.with_name("archived-v01"))
        _, data = self.render("露營燈", "light-A", "20261117-v03", "localized", "green")

        self.assertNotIn("redesigned", data["variants"])

    def test_preview_only_can_use_an_explicitly_identified_older_pass(self):
        prior = "20261117-v01"
        page, old_data = self.render("露營燈", "light-A", prior, "localized", "red")
        source = self.snapshot(page, old_data, "localized")
        rebuilt, data = self.render("露營燈", "light-A", "20261117-v02", "localized", "red",
                                    source=source, source_version=prior)
        self.assertEqual(data["variants"]["localized"]["version"], prior)
        self.assert_snapshot_color(rebuilt, data, "localized", (255, 0, 0))


if __name__ == "__main__":
    unittest.main()
