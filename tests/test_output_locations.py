import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] /
                       "skills" / "shopee-image-batch" / "scripts"))
from build_m import build
from shopee_tools import inventory, resolve_m, sha256


class OutputLocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "商品素材"
        self.source.mkdir()
        self.original = self.source / "詳情_01.jpg"
        Image.new("RGB", (1000, 1700), "white").save(self.original)
        self.original_hash = sha256(self.original)
        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps({
            "result_root": str(self.source),
            "explicit_source": str(self.source),
            "source_reviewed": True,
            "review_evidence": "Synthetic solid-white fixture, no seams or SKU variants",
            "sources": [{"path": str(self.original), "sha256": self.original_hash}],
        }), encoding="utf-8")

    def test_scan_excludes_output_tree_without_report(self):
        draft = self.source / "輸出" / "20261025-v01" / "draft.jpg"
        draft.parent.mkdir(parents=True)
        Image.new("RGB", (10, 10), "red").save(draft)
        paths = [row["relative_path"] for row in inventory(self.source)["images"]]
        self.assertEqual(paths, [self.original.name])

    def test_resolve_m_ignores_generated_output(self):
        draft = self.source / "輸出" / "20261025-v01" / "draft.jpg"
        draft.parent.mkdir(parents=True)
        Image.new("RGB", (10, 10), "red").save(draft)
        selected = resolve_m(self.source, self.source)
        self.assertEqual([row["path"] for row in selected["images"]],
                         [str(self.original)])

    def test_resolve_m_still_rejects_other_nested_images(self):
        nested = self.source / "其他款式" / "extra.jpg"
        nested.parent.mkdir()
        Image.new("RGB", (10, 10), "red").save(nested)
        with self.assertRaisesRegex(ValueError, "子目錄圖片"):
            resolve_m(self.source, self.source)

    def test_build_inside_output_preserves_source_and_versions(self):
        output = self.source / "輸出" / "20261025-v01"
        build(self.plan, output)
        files = sorted((output / "5_M詳情頁").glob("*.jpg"))
        sizes = []
        for path in files:
            with Image.open(path) as image:
                sizes.append(image.size)
        self.assertEqual(sizes, [(1000, 1500), (1000, 200)])
        old_hashes = {path: sha256(path) for path in output.rglob("*") if path.is_file()}
        with self.assertRaises(FileExistsError):
            build(self.plan, output)
        build(self.plan, self.source / "輸出" / "20261025-v02")
        self.assertEqual(sha256(self.original), self.original_hash)
        self.assertEqual({path: sha256(path) for path in old_hashes}, old_hashes)
        self.assertEqual(len(inventory(self.source)["images"]), 1)

    def test_build_external_output_remains_supported(self):
        output = self.root / "自訂成果" / "v01"
        build(self.plan, output)
        self.assertTrue((output / "5_M詳情頁" / "M詳情_01.jpg").is_file())
        self.assertEqual(sha256(self.original), self.original_hash)

    def test_build_rejects_unreserved_nested_output(self):
        for output in (self.source / "隨意子目錄" / "v01", self.source / "輸出"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                build(self.plan, output)
            self.assertFalse(output.exists())

    def test_build_rejects_source_and_its_parent(self):
        for output in (self.source, self.root):
            with self.subTest(output=output), self.assertRaises(ValueError):
                build(self.plan, output)
        self.assertEqual(sha256(self.original), self.original_hash)


if __name__ == "__main__":
    unittest.main()
