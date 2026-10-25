#!/usr/bin/env python3
"""Read-only inventory, strict M source resolution, and mechanical output audit."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from PIL import Image

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
OUTPUTS = {
    "1_主圖": ("主圖", 9, 2000000),
    "2_選項圖": ("選項", None, 2000000),
    "3_詳情頁": ("詳情", None, None),
    "4_S詳情頁": ("S詳情", 12, 2000000),
    "5_M詳情頁": ("M詳情", 20, 500000),
}
EXCLUDED = set(OUTPUTS) | {"輸出", "0_處理紀錄", "6_待人工審核", "M版來源"}
ALIASES = {
    "主圖": "main", "主图": "main", "main": "main",
    "選項圖": "option", "选项图": "option", "sku": "option",
    "詳情頁": "detail", "详情页": "detail", "詳情圖": "detail",
    "详情图": "detail", "detail": "detail",
}


def natural_key(value):
    return [(1, int(t)) if t.isdigit() else (0, t.casefold())
            for t in re.split(r"(\d+)", str(value))]


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect_image(path):
    with Image.open(path) as im:
        im.load()
        return {"width": im.width, "height": im.height, "mode": im.mode,
                "orientation": im.getexif().get(274, 1),
                "has_icc": bool(im.info.get("icc_profile")),
                "format": im.format}


def save_json_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def inventory(root, excludes=()):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("授權來源必須是資料夾")
    excluded_paths = [Path(p).resolve() for p in excludes]
    images, skipped = [], []
    for path in sorted(root.rglob("*"), key=lambda p: natural_key(p.relative_to(root))):
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            continue
        rel = path.relative_to(root)
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            skipped.append({"path": str(rel), "reason": "連結超出授權來源"})
            continue
        if set(rel.parts[:-1]) & EXCLUDED:
            continue
        if any(resolved.is_relative_to(p) for p in excluded_paths):
            continue
        # Known report-bearing output roots; other historical roots need --exclude.
        if any((parent / "處理報告.html").is_file()
               for parent in path.parents if parent != root and parent.is_relative_to(root)):
            continue
        row = {"source_id": f"src_{len(images)+1:05d}", "relative_path": str(rel),
               "bytes": path.stat().st_size, "sha256": sha256(path),
               "natural_order": len(images)+1, "sku": None,
               "category": "UNCLASSIFIED", "product_candidate": None}
        for index, part in enumerate(rel.parts[:-1]):
            if part.casefold() in ALIASES:
                row["category"] = ALIASES[part.casefold()]
                row["product_candidate"] = rel.parts[index-1] if index else root.name
                break
        try:
            row.update(inspect_image(path))
            row["decode_status"] = "OK"
        except Exception as e:
            row["decode_status"] = "FAIL"
            row["reason"] = str(e)
        images.append(row)
    return {"root": str(root), "status": "REVIEW", "images": images, "skipped": skipped,
            "pending": ["確認商品與SKU歸屬、歷史排除清單、排序及視覺內容"]}


def resolve_m(result_root, explicit=None):
    result_root = Path(result_root).resolve(strict=True)
    if not result_root.is_dir():
        raise ValueError("成果根目錄必須是資料夾")
    custom = result_root / "M版來源"
    if explicit:
        source, mode = Path(explicit).resolve(strict=True), "explicit"
    elif custom.exists():
        source, mode = custom.resolve(), "M版來源"
    else:
        source, mode = (result_root / "3_詳情頁").resolve(strict=True), "3_詳情頁"
    if not source.is_dir():
        raise ValueError("M來源存在但不是資料夾；不得回退")
    if any(p in {"4_S詳情頁", "5_M詳情頁"} for p in source.parts):
        raise ValueError("禁止S或舊M作為M來源")
    paths = sorted([p for p in source.iterdir()
                    if p.is_file() and p.suffix.lower() in EXTENSIONS],
                   key=lambda p: natural_key(p.name))
    if not paths:
        raise ValueError("選定M來源沒有有效圖片；不得回退")
    nested = [p for p in source.rglob("*") if p.is_file()
              and p.parent != source and p.suffix.lower() in EXTENSIONS
              and p.relative_to(source).parts[0] != "輸出"]
    if nested:
        raise ValueError("M來源含子目錄圖片，須先確認歸屬與順序")
    images = []
    for path in paths:
        if not path.resolve().is_relative_to(source):
            raise ValueError("M來源圖片連結超出選定來源")
        row = {"path": str(path.resolve()), "sha256": sha256(path),
               "bytes": path.stat().st_size}
        # Any corrupted source stops the entire selection, never silently skip.
        row.update(inspect_image(path))
        images.append(row)
    return {"source_root": str(source), "source_mode": mode,
            "result_root": str(result_root), "images": images, "status": "REVIEW",
            "pending": ["逐接縫、文字、商品及SKU檢視"]}


def audit(root, mode="full", na=(), sku_count=None):
    root = Path(root).resolve(strict=True)
    rows, issues = [], []
    names = ["5_M詳情頁"] if mode == "m" else list(OUTPUTS)
    for name in names:
        if name in na:
            issues.append({"status": "REVIEW", "reason": f"{name}標N/A，需確認使用者依據"})
            continue
        directory = root / name
        files = sorted([p for p in directory.iterdir() if p.is_file()
                        and p.suffix.lower() in EXTENSIONS], key=lambda p: natural_key(p.name)) if directory.is_dir() else []
        prefix, limit, cap = OUTPUTS[name]
        if not files:
            issues.append({"status": "FAIL", "reason": f"{name}必要輸出缺失"})
        if limit and len(files) > limit:
            issues.append({"status": "REVIEW", "reason": f"{name}超限：{len(files)}>{limit}"})
        if name == "2_選項圖":
            if sku_count is None:
                issues.append({"status": "REVIEW", "reason": "尚未核對有效SKU數量"})
            elif len(files) != sku_count:
                issues.append({"status": "REVIEW", "reason": "選項圖片與有效SKU數量不符"})
        for i, path in enumerate(files, 1):
            row = {"path": str(path.relative_to(root)), "bytes": path.stat().st_size,
                   "sha256": sha256(path), "issues": []}
            try:
                row.update(inspect_image(path))
                w, h = row["width"], row["height"]
                if w != 1000 or row["mode"] != "RGB":
                    row["issues"].append("寬度或RGB模式不符")
                if row["orientation"] != 1:
                    row["issues"].append("EXIF方向未規格化")
                if name in {"1_主圖", "2_選項圖"} and h != 1000:
                    row["issues"].append("高度不符1000")
                if name == "4_S詳情頁" and not 500 <= h <= 32000:
                    row["issues"].append("S高度不符")
                if name == "5_M詳情頁" and not (h == 1500 if i < len(files) else 1 <= h <= 1500):
                    row["issues"].append("M固定高度或末張高度不符")
                if cap and row["bytes"] > cap:
                    row["issues"].append("容量超限")
                match = re.fullmatch(re.escape(prefix) + r"_(\d{2,})\.(jpg|png)", path.name)
                if not match or int(match[1]) != i:
                    row["issues"].append("命名或連續序號不符")
                if (path.suffix.lower() == ".jpg" and row["format"] != "JPEG") or (path.suffix.lower() == ".png" and row["format"] != "PNG"):
                    row["issues"].append("副檔名與實際編碼不符")
                if row["format"] not in {"JPEG", "PNG"}:
                    row["issues"].append("正式格式不是JPG或PNG")
                row["status"] = "REVIEW" if row["issues"] else "FILE_OK"
            except Exception as e:
                row.update(status="FAIL", issues=[str(e)])
            rows.append(row)
    for filename in (["處理報告.html", "圖片來源對應.csv"] if mode == "m" else
                     ["處理報告.html", "圖片來源對應.csv", "文字轉換對照.csv", "預覽總覽.jpg"]):
        if not (root / filename).is_file():
            issues.append({"status": "FAIL", "reason": f"必要交付缺失：{filename}"})
    failed = any(r["status"] == "FAIL" for r in rows + issues)
    return {"status": "FAIL" if failed else "REVIEW", "files": rows, "issues": issues,
            "visual_status": "NOT_VERIFIED",
            "note": "只做機械規格檢查；FILE_OK不等於整體PASS，仍須核對來源／接縫／文字／SKU／視覺及報告。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("scan")
    p.add_argument("root")
    p.add_argument("--exclude", action="append", default=[])
    p.add_argument("--out")
    p = sub.add_parser("resolve-m")
    p.add_argument("root")
    p.add_argument("--source")
    p.add_argument("--out")
    p = sub.add_parser("audit")
    p.add_argument("root")
    p.add_argument("--mode", choices=["full", "m"], default="full")
    p.add_argument("--na", action="append", choices=list(OUTPUTS), default=[])
    p.add_argument("--sku-count", type=int)
    p.add_argument("--out")
    args = parser.parse_args()
    try:
        if args.command == "scan":
            result = inventory(args.root, args.exclude)
        elif args.command == "resolve-m":
            result = resolve_m(args.root, args.source)
        else:
            result = audit(args.root, args.mode, args.na, args.sku_count)
        if args.out:
            save_json_new(args.out, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result["status"] == "FAIL" else 0
    except Exception as e:
        print(json.dumps({"status": "FAIL", "reason": str(e)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
