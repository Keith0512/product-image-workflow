"""Build an offline, independently scrollable original/remade image preview."""
import argparse
import hashlib
import io
import json
import re
from pathlib import Path

from PIL import Image


PREVIEW_NAME = "主圖詳情頁差異預覽.html"
ASSET_DIR = Path("0_處理紀錄") / "差異預覽素材"
INDEX_FILE = Path("0_處理紀錄") / "差異預覽索引.json"
TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "compare-preview.html"


def build(plan_path, output_root):
    plan_path = Path(plan_path).resolve(strict=True)
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    if not isinstance(plan, dict):
        raise ValueError("計畫必須是JSON物件")
    output_root = Path(output_root).resolve()
    targets = [output_root / p for p in (PREVIEW_NAME, ASSET_DIR, INDEX_FILE)]
    for target in targets:
        if target.exists():
            raise FileExistsError(f"比較頁或素材已存在，請使用新版本：{target}")
    groups = plan.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("計畫必須包含非空的groups清單")
    title = plan.get("title", "主圖／詳情頁差異預覽")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title必須是非空文字")
    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count("__COMPARISON_DATA__") != 1:
        raise ValueError("比較頁模板缺少唯一資料位置")

    data = {"title": title, "groups": []}
    snapshots, provenance, seen_ids = [], [], set()
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("每個groups項目必須是物件")
        group_id, label = group.get("id"), group.get("label")
        if not isinstance(group_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", group_id):
            raise ValueError("分類id須為英數、底線或連字號，並以英文字母開頭")
        if group_id in seen_ids:
            raise ValueError(f"分類id重複：{group_id}")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("分類label必須是非空文字")
        seen_ids.add(group_id)
        rendered = {"id": group_id, "label": label}
        for side in ("original", "remade"):
            entries = group.get(side)
            if not isinstance(entries, list) or not entries:
                raise ValueError(f"{label}的{side}必須有圖片；不適用分類請勿建立")
            rendered[side] = []
            for n, entry in enumerate(entries, 1):
                if not isinstance(entry, str) or not entry:
                    raise ValueError("圖片清單須提供檔案路徑文字")
                source = Path(entry)
                if not source.is_absolute():
                    source = plan_path.parent / source
                source = source.resolve(strict=True)
                raw = source.read_bytes()
                with Image.open(io.BytesIO(raw)) as image:
                    image.load()
                    suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image.format)
                    if suffix is None:
                        raise ValueError(f"不支援的圖片格式：{source}")
                    width, height = image.size
                    if image.getexif().get(274, 1) in (5, 6, 7, 8):
                        width, height = height, width
                relative = ASSET_DIR / f"{group_id}-{side}-{n:03}{suffix}"
                digest = hashlib.sha256(raw).hexdigest()
                item = {"src": relative.as_posix(), "name": source.name,
                        "width": width, "height": height, "sha256": digest}
                rendered[side].append(item)
                snapshots.append((relative, raw))
                provenance.append({"group": group_id, "side": side,
                                   "source_path": str(source), **item})
        data["groups"].append(rendered)

    # Preflight all images before creating any preview files; snapshots preserve source bytes.
    payload = json.dumps(data, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    html = template.replace("__COMPARISON_DATA__", payload)
    (output_root / ASSET_DIR).mkdir(parents=True)
    for relative, raw in snapshots:
        (output_root / relative).write_bytes(raw)
    (output_root / PREVIEW_NAME).write_text(html, encoding="utf-8")
    result = {"preview": PREVIEW_NAME, "template_sha256": hashlib.sha256(template.encode()).hexdigest(),
              "groups": [{"id": g["id"], "label": g["label"], "original": len(g["original"]),
                          "remade": len(g["remade"])} for g in data["groups"]],
              "images": provenance, "visual_status": "NOT_VERIFIED_BY_TOOL"}
    (output_root / INDEX_FILE).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, help="已確認來源與順序的JSON計畫")
    parser.add_argument("--output", required=True, help="本次成果根目錄；不覆寫既有比較頁")
    args = parser.parse_args()
    try:
        result = build(args.plan, args.output)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        parser.exit(2, f"比較頁未完成：{error}\n")
    print(json.dumps({"preview": result["preview"], "groups": result["groups"],
                      "visual_status": result["visual_status"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
