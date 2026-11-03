import importlib.util
import json
import random
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / "skills/product-cover/scripts/check_momo_jpeg.py"
SPEC = importlib.util.spec_from_file_location("momo_checker", SCRIPT)
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class MomoJpegTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = Image.frombytes("RGB", (1000, 1000),
                                     random.Random(729).randbytes(3_000_000))
        self.valid = self.root / "ycbcr.jpg"
        self.image.save(self.valid, quality=70, subsampling=2)

    def check(self, path, expected):
        row = CHECKER.inspect_jpeg(path)
        self.assertEqual(row["status"], expected, row)
        self.assertEqual(row["visual_status"], "NOT_VERIFIED")
        return row

    def test_distinguishes_encoding_when_both_decode_to_rgb(self):
        valid = self.check(self.valid, "FILE_OK")
        rgb = self.root / "rgb.jpg"
        self.image.save(rgb, quality=30, keep_rgb=True)
        rejected = self.check(rgb, "REVIEW")
        self.assertEqual(valid["decoded_mode"], "RGB")
        self.assertEqual(rejected["decoded_mode"], "RGB")
        self.assertNotEqual(valid["encoded_color"], rejected["encoded_color"])

    def test_requires_unambiguous_jfif_or_adobe_ycbcr_evidence(self):
        data = self.valid.read_bytes()
        self.assertEqual(data[2:4], b"\xff\xe0")
        length = int.from_bytes(data[4:6], "big")
        without_jfif = data[:2] + data[4 + length:]
        unknown = self.root / "unknown.jpg"
        unknown.write_bytes(without_jfif)
        self.check(unknown, "REVIEW")
        for transform, expected in ((1, "FILE_OK"), (0, "REVIEW")):
            with self.subTest(transform=transform):
                payload = b"Adobe" + struct.pack(">HHHB", 100, 0, 0, transform)
                marker = b"\xff\xee" + (len(payload) + 2).to_bytes(2, "big") + payload
                path = self.root / f"adobe-{transform}.jpg"
                path.write_bytes(without_jfif[:2] + marker + without_jfif[2:])
                self.check(path, expected)

    def test_rejects_cmyk_grayscale_and_disguised_png(self):
        for mode in ("CMYK", "L"):
            with self.subTest(mode=mode):
                path = self.root / f"{mode}.jpg"
                self.image.convert(mode).save(path, quality=70)
                self.check(path, "REVIEW")
        png = self.root / "disguised.jpg"
        self.image.save(png, format="PNG")
        self.check(png, "REVIEW")

    def test_checks_dimensions_capacity_and_orientation(self):
        wrong_size = self.root / "wrong-size.jpg"
        self.image.resize((900, 1000)).save(wrong_size, quality=70)
        too_large = self.root / "large.jpg"
        self.image.save(too_large, quality=100, subsampling=0)
        too_small = self.root / "small.jpg"
        Image.new("RGB", (1000, 1000), "white").save(too_small)
        exif = Image.Exif()
        exif[274] = 6
        rotated = self.root / "rotated.jpg"
        self.image.save(rotated, quality=70, exif=exif)
        for path in (wrong_size, too_large, too_small, rotated):
            with self.subTest(path=path.name):
                self.check(path, "REVIEW")

    def test_missing_or_corrupt_images_fail(self):
        self.check(self.root / "missing.jpg", "FAIL")
        corrupt = self.root / "corrupt.jpg"
        corrupt.write_bytes(b"not an image")
        self.check(corrupt, "FAIL")

    def test_cli_preserves_inputs_and_existing_report_without_claiming_pass(self):
        before = self.valid.read_bytes()
        output = self.root / "check.json"
        command = [sys.executable, "-B", str(SCRIPT), str(self.valid), "--out", str(output)]
        first = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        report = json.loads(first.stdout)
        self.assertEqual(report["mechanical_status"], "FILE_OK")
        self.assertEqual(report["status"], "REVIEW")
        self.assertEqual(report["visual_status"], "NOT_VERIFIED")
        saved = output.read_bytes()
        second = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(second.returncode, 2)
        self.assertEqual(output.read_bytes(), saved)
        self.assertEqual(self.valid.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
