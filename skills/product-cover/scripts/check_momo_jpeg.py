#!/usr/bin/env python3
"""Read-only MOMO JPEG checks; image composition still requires visual review."""

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image


def inspect_jpeg(filename):
    path = Path(filename).resolve()
    row = {"path": str(path), "issues": [], "visual_status": "NOT_VERIFIED"}
    try:
        row["bytes"] = path.stat().st_size
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        row["sha256"] = digest.hexdigest()
        with Image.open(path) as image:
            image.load()
            components = [part[0] for part in getattr(image, "layer", ())]
            transform = image.info.get("adobe_transform")
            jfif = image.info.get("jfif")
            row.update(
                format=image.format,
                width=image.width,
                height=image.height,
                decoded_mode=image.mode,
                orientation=image.getexif().get(274, 1),
                jpeg_evidence={
                    "component_ids": components,
                    "adobe_transform": transform,
                    "jfif_version": jfif,
                },
            )
        if row["format"] != "JPEG" or path.suffix.lower() not in {".jpg", ".jpeg"}:
            row["issues"].append("實際格式與副檔名均須為 JPEG")
        if (row["width"], row["height"]) != (1000, 1000):
            row["issues"].append("尺寸須為 1000 × 1000")
        if not 50_000 <= row["bytes"] <= 1_000_000:
            row["issues"].append("容量不在本套件採用的 50,000–1,000,000 bytes")
        if row["orientation"] != 1:
            row["issues"].append("EXIF 方向尚未規格化")

        # JFIF three-component data is YCbCr. Adobe transform 1 explicitly
        # specifies YCbCr; transform 0 or RGB component IDs must not pass.
        ycbcr = (
            row["format"] == "JPEG"
            and row["decoded_mode"] == "RGB"
            and components == [1, 2, 3]
            and (transform == 1 or (transform is None and jfif is not None))
        )
        row["encoded_color"] = "YCbCr" if ycbcr else "NOT_CONFIRMED_YCbCr"
        if not ycbcr:
            row["issues"].append("非可確認的 YCbCr JPEG；須重新編碼或提供等效證據")
        row["status"] = "REVIEW" if row["issues"] else "FILE_OK"
    except Exception as error:
        row["status"] = "FAIL"
        row["issues"].append(str(error))
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+")
    parser.add_argument("--out", help="建立新 JSON 紀錄，不覆寫既有檔案")
    args = parser.parse_args()
    rows = [inspect_jpeg(filename) for filename in args.images]
    file_ok = all(row["status"] == "FILE_OK" for row in rows)
    failed = any(row["status"] == "FAIL" for row in rows)
    result = {
        "status": "FAIL" if failed else "REVIEW",
        "mechanical_status": "FILE_OK" if file_ok else "FAIL" if failed else "REVIEW",
        "visual_status": "NOT_VERIFIED",
        "files": rows,
        "note": "FILE_OK 不等於整體 PASS，仍須實際檢查 MOMO 版面及商品保真。",
    }
    if args.out:
        try:
            output = Path(args.out)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
        except OSError as error:
            print(json.dumps({"status": "FAIL", "reason": str(error)}, ensure_ascii=False))
            return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if file_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
