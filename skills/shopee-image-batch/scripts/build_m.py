#!/usr/bin/env python3
"""Build fixed-height M slices from an agent-reviewed, hash-bound source plan."""
import argparse
import csv
import io
import json
import math
from pathlib import Path
from PIL import Image, ImageOps, ImageCms
from shopee_tools import resolve_m, save_json_new, sha256

SEAM_TYPES = {"CONTINUOUS", "OVERLAP", "INDEPENDENT", "MISSING", "UNCERTAIN"}


def rgb_normalized(path, background=None):
    with Image.open(path) as original:
        original.load()
        image = ImageOps.exif_transpose(original)
        if "A" in image.getbands() or "transparency" in original.info:
            if not (isinstance(background, list) and len(background) == 3
                    and all(type(x) is int and 0 <= x <= 255 for x in background)):
                raise ValueError("透明來源須在計畫提供確認的background_rgb")
            alpha = image.convert("RGBA")
            base = Image.new("RGBA", alpha.size, tuple(background) + (255,))
            image = Image.alpha_composite(base, alpha).convert("RGB")
        elif image.mode != "RGB":
            image = image.convert("RGB")
        icc = original.info.get("icc_profile")
        if icc:
            image = ImageCms.profileToProfile(
                image, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                ImageCms.createProfile("sRGB"), outputMode="RGB")
        h = max(1, round(image.height * 1000 / image.width))
        return image.resize((1000, h), Image.Resampling.LANCZOS)


def encode(image):
    # Enforce a quality floor; oversized images remain complete and REVIEW.
    for quality in (95, 93, 91, 89, 87, 85, 83, 82):
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=quality, optimize=True, subsampling=0)
        data = buffer.getvalue()
        if len(data) <= 480000:
            return data, quality
    return data, quality


def build(plan_path, output):
    plan_path = Path(plan_path).resolve(strict=True)
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    if plan.get("mode", "fixed-1500") != "fixed-1500":
        raise ValueError("工具僅支援fixed-1500")
    if plan.get("source_reviewed") is not True or not plan.get("review_evidence"):
        raise ValueError("必須先完成來源、SKU與接縫檢視並提供實際證據")
    base = Path(plan["result_root"])
    explicit = plan.get("explicit_source")
    if not base.is_absolute() or (explicit and not Path(explicit).is_absolute()):
        raise ValueError("result_root與explicit_source須用絕對路徑")
    selected = resolve_m(base, explicit)
    rows = plan["sources"]
    if len(rows) != len(selected["images"]):
        raise ValueError("計畫清單與唯一來源不符")
    for index, (row, actual) in enumerate(zip(rows, selected["images"])):
        if Path(row["path"]).resolve() != Path(actual["path"]) or row["sha256"] != actual["sha256"]:
            raise ValueError("來源雜湊、清單或排序已變更")
        trim = row.get("trim_top_normalized_px", 0)
        if type(trim) is not int or trim < 0:
            raise ValueError("去重高度須為非負整數")
        if index == 0:
            if trim:
                raise ValueError("首張不得去除頂端")
        else:
            boundary = row.get("boundary_before", {})
            if boundary.get("type") not in SEAM_TYPES or not boundary.get("evidence"):
                raise ValueError("每一接縫均須分類與證據")
            if trim and boundary["type"] != "OVERLAP":
                raise ValueError("只允許有證據的OVERLAP去重")
    output = Path(output).resolve()
    source = Path(selected["source_root"])
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("輸出與來源不得互相包含")
    # New root is mandatory: no overwrite or partial reuse.
    output.mkdir(parents=True, exist_ok=False)
    records = output / "0_處理紀錄"
    normalized = records / "本次M中間來源"
    lossless = records / "M無損切片"
    normalized.mkdir(parents=True)
    lossless.mkdir()
    destination = output / "5_M詳情頁"
    destination.mkdir()
    save_json_new(records / "M計畫.json", plan)
    sources, slices, pending = [], [], []
    buffer = Image.new("RGB", (1000, 1500))
    filled, y, index = 0, 0, 0

    def flush(height):
        nonlocal index, y
        index += 1
        image = buffer.crop((0, 0, 1000, height))
        lossless_path = lossless / f"M詳情_{index:02d}.png"
        image.save(lossless_path, "PNG")
        data, quality = encode(image)
        path = destination / f"M詳情_{index:02d}.jpg"
        with path.open("xb") as f:
            f.write(data)
        slices.append({"path": str(path.relative_to(output)),
                       "lossless_path": str(lossless_path.relative_to(output)),
                       "y0": y, "y1": y + height, "height": height,
                       "bytes": len(data), "quality": quality,
                       "sha256": sha256(path), "lossless_sha256": sha256(lossless_path)})
        if len(data) > 500000:
            pending.append(f"{path.name}容量超限")
        y += height

    for source_index, row in enumerate(rows):
        # Recheck immediately before reading, after the initial full-list validation.
        if sha256(row["path"]) != row["sha256"]:
            raise ValueError("處理中來源變動，保留部分成果但不得宣稱完成")
        image = rgb_normalized(row["path"], plan.get("background_rgb"))
        original_height = image.height
        trim = row.get("trim_top_normalized_px", 0)
        if trim >= original_height:
            raise ValueError("不得去除整張來源")
        if trim:
            image = image.crop((0, trim, 1000, original_height))
        saved = normalized / f"來源_{source_index+1:04d}.png"
        image.save(saved, "PNG")
        start = y + filled
        sources.append({"source_path": row["path"], "source_sha256": row["sha256"],
                        "working_path": str(saved.relative_to(output)),
                        "working_sha256": sha256(saved),
                        "normalized_height_before_trim": original_height,
                        "trim_top_normalized_px": trim,
                        "y0": start, "y1": start + image.height,
                        "source_width": selected["images"][source_index]["width"],
                        "source_height": selected["images"][source_index]["height"],
                        "orientation": selected["images"][source_index]["orientation"],
                        "boundary_before": row.get("boundary_before")})
        if source_index and row["boundary_before"]["type"] in {"MISSING", "UNCERTAIN"}:
            pending.append(f"來源{source_index+1}接縫疑點")
        offset = 0
        while offset < image.height:
            take = min(1500 - filled, image.height - offset)
            buffer.paste(image.crop((0, offset, 1000, offset + take)), (0, filled))
            offset += take
            filled += take
            if filled == 1500:
                flush(1500)
                filled = 0
    if filled:
        flush(filled)
    expected = math.ceil(y / 1500)
    if len(slices) != expected:
        raise AssertionError("M切片數量不符")
    if expected > 20:
        pending.append(f"M超過20張：{expected}")
    manifest = {
        "status": "REVIEW", "mode": "fixed-1500",
        "source_mode": selected["source_mode"], "source_root": selected["source_root"],
        "coordinate_convention": "half-open [y0,y1)",
        "height": y, "width": 1000, "expected_count": expected,
        "actual_count": len(slices), "added_whitespace_px": 0,
        "removed_overlap_px": sum(s["trim_top_normalized_px"] for s in sources),
        "sources": sources, "slices": slices,
        "visual_status": "NOT_VERIFIED",
        "pending": pending + ["壓縮後視覺、切線字元、繁中、SKU、來源覆蓋及完整HTML待驗證"],
        "logical_long_image": "依sources工作圖與座標可完整重建；本工具不配置巨型單張點陣圖",
    }
    save_json_new(records / "M切片紀錄.json", manifest)
    csv_path = output / "圖片來源對應.csv"
    with csv_path.open("x", encoding="utf-8-sig", newline="") as f:
        columns = ["output_path", "output_sha256", "source_path", "source_sha256",
                   "working_path", "working_box", "output_box",
                   "source_orientation", "source_width", "source_height",
                   "oriented_source_box", "source_coordinate_space",
                   "trim_top_normalized_px", "change_type", "evidence_path", "status"]
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for sl in slices:
            for src in sources:
                lo, hi = max(sl["y0"], src["y0"]), min(sl["y1"], src["y1"])
                if lo >= hi:
                    continue
                writer.writerow({
                    "output_path": sl["path"], "output_sha256": sl["sha256"],
                    "source_path": src["source_path"], "source_sha256": src["source_sha256"],
                    "working_path": src["working_path"],
                    "working_box": json.dumps([0, lo-src["y0"], 1000, hi-src["y0"]]),
                    "output_box": json.dumps([0, lo-sl["y0"], 1000, hi-sl["y0"]]),
                    "source_orientation": src["orientation"],
                    "oriented_source_box": json.dumps([0,
                        (lo-src["y0"]+src["trim_top_normalized_px"])*(src["source_width"] if src["orientation"] in {5,6,7,8} else src["source_height"])/src["normalized_height_before_trim"],
                        src["source_height"] if src["orientation"] in {5,6,7,8} else src["source_width"],
                        (hi-src["y0"]+src["trim_top_normalized_px"])*(src["source_width"] if src["orientation"] in {5,6,7,8} else src["source_height"])/src["normalized_height_before_trim"]]),
                    "source_coordinate_space": "EXIF轉正後來源像素，半開區間",
                    "source_width": src["source_width"], "source_height": src["source_height"],
                    "trim_top_normalized_px": src["trim_top_normalized_px"],
                    "change_type": "規格化及固定切片；原圖映射見M切片紀錄",
                    "evidence_path": "0_處理紀錄/M計畫.json", "status": "REVIEW"})
    return manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    try:
        result = build(args.plan, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print(json.dumps({"status": "FAIL", "reason": str(e),
                          "note": "部分成果如已建立將保留，不能宣稱完整完成"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
