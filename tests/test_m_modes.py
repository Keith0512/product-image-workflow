"""Synthetic behavior checks; fixture PASS records are not production visual evidence."""
import copy
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "shopee-image-batch" / "scripts"))
from build_m import build, encode
from momo_editorial import CATEGORIES, validate_plan
from shopee_tools import audit, inventory, resolve_m, sha256, validate_visual_reviews


def reviewed(path, **extra):
    digest = sha256(path)
    return {"path": str(path.resolve()), "sha256": digest,
            "visual_review": {"sha256": digest, "status": "PASS", "typography": "PASS",
                              "text_cleanup": "PASS", "background": "PASS", "product_integrity": "PASS",
                              "evidence": "Synthetic test fixture only; no real product was reviewed."}, **extra}


class MModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("MOMO_TEST_TMP"))
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "source"
        self.source = self.root / "3_詳情頁"
        self.source.mkdir(parents=True)
        self.drafts = self.base / "drafts"
        self.drafts.mkdir()
        self.sources, self.pages = [], []
        for n in range(1, 3):
            p = self.source / f"詳情_{n:02d}.png"
            im = Image.new("RGB", (1000, 2000), (n*60, 100, 140))
            ImageDraw.Draw(im).text((30, 30), f"Fixture source {n}", fill="white")
            im.save(p)
            row = reviewed(p)
            if n > 1:
                row["boundary_before"] = {"type": "INDEPENDENT", "evidence": "Independent synthetic cards"}
            self.sources.append(row)
            page = self.drafts / f"page_{n:02d}.png"
            image = Image.new("RGB", (1000, 1500), (240, 240-n*30, 220))
            ImageDraw.Draw(image).text((30, 30), f"Fixture page {n}: 12cm 6PSI 1pcs", fill="black")
            image.save(page)
            self.pages.append(reviewed(page, page=n))
        self.plan = {
            "mode": "momo-editorial", "result_root": str(self.root),
            "source_reviewed": True, "review_evidence": "Synthetic source fixture review",
            "sources": self.sources, "pages": self.pages,
            "coverage_categories": [{"id": x, "applicability": "required", "reason": "Synthetic fixture applies"}
                                    for x in sorted(CATEGORIES)],
            "content_review": {"status": "PASS", "evidence": "Synthetic ledger fixture; not a real factual review"},
            "content_items": [],
        }
        for n, category in enumerate(sorted(CATEGORIES)):
            source = self.sources[n % 2]
            value = {"specifications": "12cm", "safety": "6PSI", "package_contents": "1pcs"}.get(category)
            text = f"Fixture {category}" + (f" {value}" if value else "")
            self.plan["content_items"].append({
                "id": category, "category": category, "required": True, "decision": "keep",
                "reason": "Required synthetic fixture block", "original_text": text, "final_text": text,
                "protected_values": [value] if value else [],
                "source_regions": [{"path": source["path"], "sha256": source["sha256"],
                                    "box": [0, 0, 1000, 2000]}],
                "placements": [{"page": n % 2 + 1, "box": [40, 100+n*150, 960, 200+n*150],
                                "operation": "text_reflow"}],
            })
        self.output = self.base / "output"

    def run_build(self, plan=None, output=None):
        path = self.base / "plan.json"
        path.write_text(json.dumps(plan if plan is not None else self.plan), encoding="utf-8")
        return build(path, output or self.output)

    def reject(self, plan, message):
        with self.assertRaisesRegex(ValueError, message):
            self.run_build(plan)
        self.assertFalse(self.output.exists(), "Invalid plans must fail before creating output")

    def report_stub(self):
        (self.output / "處理報告.html").write_text("<html lang='zh-Hant'>Synthetic fixture</html>", encoding="utf-8")

    def item(self, category):
        return next(x for x in self.plan["content_items"] if x["category"] == category)

    def fixed_plan(self, height):
        for p in self.source.iterdir():
            p.unlink()
        p = self.source / "詳情_01.png"
        image = Image.new("RGB", (1000, height))
        draw = ImageDraw.Draw(image)
        for y in range(0, height, 1500):
            draw.rectangle((0, y, 999, min(height-1, y+1499)), fill=((y//1500*11) % 256, 80, 130))
        image.save(p)
        return {"mode": "fixed-1500", "result_root": str(self.root), "source_reviewed": True,
                "review_evidence": "Synthetic fixed-mode fixture", "sources": [reviewed(p)]}

    def test_default_exports_reviewed_pages_and_preserves_sources(self):
        self.plan.pop("mode")
        before = {p: sha256(p) for p in self.source.iterdir()}
        result = self.run_build()
        self.assertEqual(result["mode"], "momo-editorial")
        self.assertEqual(result["actual_count"], 2)
        self.assertEqual(result["files_status"], "FILE_OK")
        self.assertFalse(result["upload_ready"])
        self.assertFalse((self.output / "6_待人工審核").exists())
        for row in result["pages"]:
            with Image.open(self.output / row["path"]) as image:
                self.assertEqual(image.size, (1000, 1500))
            self.assertLessEqual(row["bytes"], 500000)
        self.assertEqual(before, {p: sha256(p) for p in self.source.iterdir()})
        self.report_stub()
        checked = audit(self.output, mode="m")
        self.assertFalse(any(x["status"] == "FAIL" for x in checked["issues"]))
        self.assertTrue(all(x["status"] == "FILE_OK" for x in checked["files"]))
        self.assertEqual(checked["visual_status"], "NOT_VERIFIED")

    def test_twenty_page_boundary_is_accepted(self):
        for n in range(3, 21):
            path = self.drafts / f"page_{n:02d}.png"
            Image.new("RGB", (1000, 1500), (n, 100, 120)).save(path)
            self.plan["pages"].append(reviewed(path, page=n))
            self.item("identity")["placements"].append({"page": n, "box": [50, 50, 900, 150], "operation": "text_reflow"})
        validate_plan(self.plan)

    def test_twenty_one_pages_are_not_truncated(self):
        self.plan["pages"] = (self.pages * 11)[:21]
        self.reject(self.plan, "1至20")

    def test_required_information_cannot_be_omitted(self):
        row = self.item("safety")
        row.update(decision="omit", placements=[], final_text="")
        self.reject(self.plan, "不得省略必要資訊")

    def test_required_category_must_have_placed_information(self):
        self.plan["content_items"] = [x for x in self.plan["content_items"] if x["category"] != "safety"]
        self.reject(self.plan, "必要資訊類別尚未覆蓋")

    def test_unplaced_kept_information_is_rejected(self):
        self.item("operation")["placements"] = []
        self.reject(self.plan, "未對應任何成品頁")

    def test_changed_protected_pressure_is_rejected(self):
        self.item("safety")["final_text"] = "Fixture safety 8PSI"
        self.reject(self.plan, "受保護")

    def test_numeric_substring_does_not_preserve_a_changed_value(self):
        self.item("safety")["final_text"] = "Fixture safety 16PSI"
        self.reject(self.plan, "受保護")

    def test_unresolved_content_review_does_not_export(self):
        self.plan["content_review"]["status"] = "REVIEW"
        self.reject(self.plan, "實際內容核對")

    def test_nonessential_omission_is_accepted_and_recorded(self):
        item = copy.deepcopy(self.item("key_features"))
        item.update(id="decorative-photo", category="supporting", required=False, decision="omit",
                    reason="Duplicate decorative scene", placements=[], final_text="", protected_values=[])
        self.plan["content_items"].append(item)
        result = self.run_build()
        self.assertEqual(result["files_status"], "FILE_OK")
        text = (self.output / "0_處理紀錄/M內容取捨.csv").read_text(encoding="utf-8-sig")
        self.assertIn("Duplicate decorative scene", text)

    def test_unaccounted_source_cannot_be_silently_dropped(self):
        ref = copy.deepcopy(self.item("identity")["source_regions"])
        for item in self.plan["content_items"]:
            item["source_regions"] = copy.deepcopy(ref)
        self.reject(self.plan, "有來源圖片未記")

    def test_out_of_scope_source_reference_is_rejected(self):
        self.item("identity")["source_regions"][0]["path"] = self.pages[0]["path"]
        self.reject(self.plan, "超出唯一來源")

    def test_core_category_cannot_be_not_applicable(self):
        next(x for x in self.plan["coverage_categories"] if x["id"] == "specifications")["applicability"] = "not_applicable"
        self.reject(self.plan, "主要規格")

    def test_stale_edited_page_review_is_rejected(self):
        Image.new("RGB", (1000, 1500), "blue").save(self.pages[0]["path"])
        self.reject(self.plan, "版本已變更")

    def test_stale_source_review_is_rejected(self):
        self.sources[0]["visual_review"]["sha256"] = "0" * 64
        self.reject(self.plan, "版本已變更")

    def test_duplicate_pages_are_rejected(self):
        self.plan["pages"][1] = {**copy.deepcopy(self.pages[0]), "page": 2}
        self.reject(self.plan, "重複頁")

    def test_wrong_page_height_is_not_stretched(self):
        path = Path(self.pages[0]["path"])
        Image.new("RGB", (1000, 1499), "red").save(path)
        self.plan["pages"][0] = reviewed(path, page=1)
        self.reject(self.plan, "1000×1500")

    def test_lossy_draft_is_not_accepted_as_lossless(self):
        path = Path(self.pages[0]["path"])
        Image.new("RGB", (1000, 1500), "red").save(path, format="JPEG")
        self.plan["pages"][0] = reviewed(path, page=1)
        self.reject(self.plan, "RGB PNG")

    def test_photo_cannot_be_stretched(self):
        self.item("identity")["placements"][0].update(operation="image_crop", source_region=0)
        self.reject(self.plan, "長寬比例")

    def test_original_photo_region_maps_to_the_edited_page(self):
        item = self.item("identity")
        item["placements"] = [{"page": 1, "box": [20, 300, 520, 1300],
                               "operation": "image_crop", "source_region": 0}]
        path = Path(self.pages[0]["path"])
        with Image.open(path) as image, Image.open(self.sources[0]["path"]) as source:
            image.paste(source.resize((500, 1000), Image.Resampling.LANCZOS), (20, 300))
            image.save(path)
        self.plan["pages"][0] = reviewed(path, page=1)
        self.run_build()
        import csv
        with (self.output / "圖片來源對應.csv").open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        row = next(x for x in rows if x["item_id"] == "identity")
        self.assertEqual(row["change_type"], "image_crop")
        self.assertEqual(json.loads(row["source_box"]), [0, 0, 1000, 2000])
        self.assertEqual(json.loads(row["output_box"]), [20, 300, 520, 1300])

    def test_source_order_change_is_rejected(self):
        self.plan["sources"] = list(reversed(self.sources))
        self.reject(self.plan, "排序已變更")

    def test_empty_custom_source_does_not_fall_back(self):
        (self.root / "M版來源").mkdir()
        self.reject(self.plan, "不得回退")

    def test_confirmed_appledouble_is_not_an_image(self):
        (self.source / "._詳情_01.png").write_bytes(b"\x00\x05\x16\x07" + bytes(64))
        self.assertEqual(len(resolve_m(self.root)["images"]), 2)
        validate_visual_reviews(self.sources, [self.source])
        inventory_source = self.base / "raw"
        inventory_source.mkdir()
        shutil.copyfile(self.source / "詳情_01.png", inventory_source / "main.png")
        (inventory_source / "._main.png").write_bytes(b"\x00\x05\x16\x07" + bytes(64))
        self.assertEqual(len(inventory(inventory_source)["images"]), 1)

    def test_oddly_named_corrupted_source_is_not_silently_ignored(self):
        (self.source / "._broken.jpg").write_bytes(b"not an AppleDouble file")
        with self.assertRaises(Exception):
            resolve_m(self.root)

    def test_existing_output_is_not_overwritten(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.run_build()
        self.assertEqual(marker.read_text(), "keep")

    def test_output_cannot_contain_source(self):
        with self.assertRaisesRegex(ValueError, "包含來源"):
            self.run_build(output=self.base)
        self.assertTrue((self.source / "詳情_01.png").is_file())

    def test_oversize_pages_have_one_review_set_and_no_formal_copy(self):
        path = Path(self.pages[1]["path"])
        image = Image.frombytes("RGB", (1000, 1500), random.Random(42).randbytes(1000*1500*3))
        self.assertGreater(len(encode(image)[0]), 500000)
        image.save(path)
        self.plan["pages"][1] = reviewed(path, page=2)
        result = self.run_build()
        self.assertFalse((self.output / "5_M詳情頁").exists())
        files = list((self.output / "6_待人工審核").rglob("*.jpg"))
        self.assertEqual(len(files), 2)
        self.assertEqual(result["files_status"], "REVIEW")
        self.report_stub()
        self.assertEqual(audit(self.output, mode="m")["status"], "REVIEW")

    def test_fixed_mode_preserves_short_tail(self):
        result = self.run_build(self.fixed_plan(1601))
        self.assertEqual([x["height"] for x in result["slices"]], [1500, 101])
        self.assertTrue((self.output / "5_M詳情頁").is_dir())
        self.report_stub()
        checked = audit(self.output, mode="m")
        self.assertTrue(all(x["status"] == "FILE_OK" for x in checked["files"]))

    def test_fixed_overflow_preserves_all_pixels_without_duplicate_jpegs(self):
        result = self.run_build(self.fixed_plan(30001))
        self.assertEqual(result["actual_count"], 21)
        self.assertFalse((self.output / "5_M詳情頁").exists())
        files = list((self.output / "6_待人工審核").rglob("*.jpg"))
        self.assertEqual(len(files), 21)
        reconstructed = Image.new("RGB", (1000, 30001))
        for row in result["slices"]:
            with Image.open(self.output / row["lossless_path"]) as image:
                reconstructed.paste(image, (0, row["y0"]))
        with Image.open(self.source / "詳情_01.png") as original:
            self.assertIsNone(ImageChops.difference(original, reconstructed).getbbox())
        self.report_stub()
        self.assertEqual(audit(self.output, mode="m")["status"], "REVIEW")

    def test_audit_detects_duplicate_review_copy(self):
        result = self.run_build()
        self.report_stub()
        folder = self.output / "6_待人工審核"
        folder.mkdir()
        shutil.copyfile(self.output / result["pages"][0]["path"], folder / "copy.jpg")
        checked = audit(self.output, mode="m")
        self.assertTrue(any("相同的M副本" in x["reason"] for x in checked["issues"]))

    def test_audit_detects_changed_coverage_record(self):
        self.run_build()
        self.report_stub()
        (self.output / "0_處理紀錄/M必要資訊覆蓋.csv").write_text("changed", encoding="utf-8")
        checked = audit(self.output, mode="m")
        self.assertEqual(checked["status"], "FAIL")
        self.assertTrue(any("追溯紀錄" in x["reason"] for x in checked["issues"]))

    def test_audit_detects_changed_export(self):
        result = self.run_build()
        self.report_stub()
        (self.output / result["pages"][0]["path"]).write_bytes(b"changed")
        self.assertEqual(audit(self.output, mode="m")["status"], "FAIL")

    def test_audit_requires_editorial_record_when_plan_exists(self):
        self.run_build()
        self.report_stub()
        (self.output / "0_處理紀錄/M切片紀錄.json").unlink()
        self.assertEqual(audit(self.output, mode="m")["status"], "FAIL")

    def test_cli_exports_default_mode_and_rejects_overflow_before_output(self):
        self.plan.pop("mode")
        plan_path = self.base / "cli-plan.json"
        plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "skills/shopee-image-batch/scripts/build_m.py"
        command = [sys.executable, str(script), "--plan", str(plan_path), "--output", str(self.output)]
        good = subprocess.run(command, cwd=self.base, capture_output=True, text=True, check=False)
        self.assertEqual(good.returncode, 0, good.stderr + good.stdout)
        self.assertEqual(json.loads(good.stdout)["mode"], "momo-editorial")
        self.plan["pages"] = (self.pages * 11)[:21]
        plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        invalid_output = self.base / "invalid-output"
        command[-1] = str(invalid_output)
        bad = subprocess.run(command, cwd=self.base, capture_output=True, text=True, check=False)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(json.loads(bad.stdout)["status"], "FAIL")
        self.assertFalse(invalid_output.exists())


if __name__ == "__main__":
    unittest.main()
