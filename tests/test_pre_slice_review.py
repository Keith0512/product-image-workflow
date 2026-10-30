import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "shopee-image-batch" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from build_m import build
from shopee_tools import sha256, validate_visual_reviews


class PreSliceReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "整理稿"
        self.source.mkdir()
        self.image = self.source / "詳情_01.png"
        Image.new("RGB", (1000, 1700), "white").save(self.image)
        digest = sha256(self.image)
        self.row = {
            "path": str(self.image), "sha256": digest,
            "visual_review": {
                "sha256": digest, "status": "PASS", "typography": "PASS",
                "text_cleanup": "PASS", "background": "PASS", "product_integrity": "PASS",
                "evidence": "Synthetic flat-white fixture: no text, product, shadows or repair regions",
            },
        }
        self.plan = {
            "result_root": str(self.source), "explicit_source": str(self.source),
            "source_reviewed": True, "review_evidence": "Synthetic fixture without adjacent seams",
            "sources": [self.row],
        }
        self.plan_path = self.root / "plan.json"
        self.output = self.root / "成果"

    def save_plan(self):
        self.plan_path.write_text(json.dumps(self.plan), encoding="utf-8")

    def assert_build_blocked(self):
        self.save_plan()
        with self.assertRaises(ValueError):
            build(self.plan_path, self.output)
        self.assertFalse(self.output.exists(), "Blocked review must not create any slice output")

    def test_old_source_reviewed_flag_does_not_bypass_visual_gate(self):
        del self.row["visual_review"]
        self.assert_build_blocked()

    def test_unresolved_review_or_failure_blocks_before_output(self):
        for status in ("REVIEW", "FAIL", "NOT_VERIFIED", "", True):
            with self.subTest(status=status):
                self.row["visual_review"]["status"] = status
                self.assert_build_blocked()

    def test_each_required_check_must_explicitly_pass(self):
        for field in ("typography", "text_cleanup", "background", "product_integrity"):
            for status in (None, "REVIEW", "FAIL", True):
                with self.subTest(field=field, status=status):
                    original = self.row["visual_review"].pop(field)
                    if status is not None:
                        self.row["visual_review"][field] = status
                    self.assert_build_blocked()
                    self.row["visual_review"][field] = original

    def test_empty_or_nontext_evidence_does_not_count_as_inspection(self):
        for evidence in (None, "", " \n\t", True, []):
            with self.subTest(evidence=evidence):
                self.row["visual_review"]["evidence"] = evidence
                self.assert_build_blocked()

    def test_repair_requires_new_review_even_if_source_hash_is_updated(self):
        Image.new("RGB", (1000, 1700), "gray").save(self.image)
        self.row["sha256"] = sha256(self.image)
        self.assert_build_blocked()

    def test_changed_file_with_old_plan_is_blocked(self):
        Image.new("RGB", (1000, 1700), "gray").save(self.image)
        self.assert_build_blocked()

    def test_all_sources_need_reviews(self):
        second = self.source / "詳情_02.png"
        Image.new("RGB", (1000, 600), "gray").save(second)
        self.plan["sources"].append({
            "path": str(second), "sha256": sha256(second),
            "boundary_before": {"type": "INDEPENDENT", "evidence": "Separate synthetic block"},
        })
        self.assert_build_blocked()

    def test_valid_review_keeps_final_slice_review_pending(self):
        self.save_plan()
        manifest = build(self.plan_path, self.output)
        self.assertEqual(manifest["actual_count"], 2)
        sizes = []
        for file in sorted((self.output / "5_M詳情頁").glob("*.jpg")):
            with Image.open(file) as image:
                sizes.append(image.size)
        self.assertEqual(sizes, [(1000, 1500), (1000, 200)])
        saved = json.loads((self.output / "0_處理紀錄" / "M切片紀錄.json").read_text())
        self.assertEqual(saved["pre_slice_visual_reviews"][0]["visual_review"],
                         self.row["visual_review"])
        self.assertEqual(saved["visual_status"], "NOT_VERIFIED")
        self.assertEqual(manifest["status"], "REVIEW")

    def test_coverage_rejects_omitted_added_or_duplicate_images(self):
        self.assertEqual(len(validate_visual_reviews([self.row], [self.source])), 1)
        second = self.source / "詳情_02.png"
        Image.new("RGB", (1000, 600), "gray").save(second)
        with self.assertRaisesRegex(ValueError, "完整對應"):
            validate_visual_reviews([self.row], [self.source])
        other = self.root / "其他商品"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "完整對應"):
            validate_visual_reviews([self.row], [other])
        with self.assertRaisesRegex(ValueError, "重複"):
            validate_visual_reviews([self.row, self.row])

    def test_empty_review_list_is_blocked(self):
        with self.assertRaises(ValueError):
            validate_visual_reviews([], [self.source])

    def test_check_visual_cli_validates_records_without_claiming_visual_pass(self):
        self.save_plan()
        command = [sys.executable, "-B", str(SCRIPTS / "shopee_tools.py"),
                   "check-visual", str(self.plan_path), "--source", str(self.source)]
        passed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(passed.returncode, 0, passed.stdout)
        result = json.loads(passed.stdout)
        self.assertEqual(result["review_records"], "VALID")
        self.assertEqual(result["visual_status"], "NOT_VERIFIED_BY_TOOL")
        self.row["visual_review"]["text_cleanup"] = "REVIEW"
        self.save_plan()
        blocked = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(blocked.returncode, 2)
        self.assertEqual(json.loads(blocked.stdout)["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
