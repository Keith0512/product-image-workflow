"""Build one offline image comparison page per product at the output version root."""
import argparse
import hashlib
import io
import json
import re
import shutil
import unicodedata
from pathlib import Path

from PIL import Image


PREVIEW_NAME = "主圖詳情頁差異預覽.html"
ASSET_ROOT = Path("0_處理紀錄") / "差異預覽素材"
INDEX_ROOT = Path("0_處理紀錄") / "差異預覽索引"
TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "compare-preview.html"
MODE_LABELS = {"original": "原圖", "localized": "原風格繁中", "redesigned": "繁中重設計"}


def version_order(name):
    match = re.fullmatch(r"(\d{8})-v(\d{2,})", name, flags=re.ASCII)
    return tuple(map(int, match.groups())) if match else None


def safe_title(title):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]+', "_", unicodedata.normalize("NFC", title))
    return name.encode("utf-8")[:150].decode("utf-8", errors="ignore").strip(" ._") or "商品"


def inside(path, root):
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"路徑超出比較頁素材目錄：{path}")
    return resolved


def read_image(source, expected_hash=None):
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_hash is not None and digest != expected_hash:
        raise ValueError(f"圖片SHA-256不符合舊版索引：{source.name}")
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image.format)
        if suffix is None:
            raise ValueError(f"不支援的圖片格式：{source}")
        width, height = image.size
        if image.getexif().get(274, 1) in (5, 6, 7, 8):
            width, height = height, width
    return {"name": source.name, "width": width, "height": height, "sha256": digest}, suffix, raw


def previous_versions(output_root):
    current = version_order(output_root.name)
    if current is None or not output_root.parent.is_dir():
        return []
    versions = [
        path for path in output_root.parent.iterdir()
        if (order := version_order(path.name)) is not None
        and order < current and not path.is_symlink() and path.is_dir()
    ]
    return sorted(versions, key=lambda path: version_order(path.name), reverse=True)


def previous_mode(output_root, asset_dir, index_file, product_id, side, categories, trace):
    for version in previous_versions(output_root):
        index_path = version / index_file
        if not index_path.is_file():
            continue
        try:
            index = json.loads(inside(index_path, version).read_text(encoding="utf-8"))
            if index["product_id"] != product_id:
                raise ValueError("商品識別不一致")
            metadata = index["variants"].get(side)
            if metadata is None:
                continue
            if metadata.get("version") != version.name or metadata.get("status") != "PASS":
                raise ValueError("不是此版直接產出的已驗收版本")
            images = []
            for item in index["images"]:
                if item["side"] != side or item["group"] not in categories:
                    continue
                relative = Path(item["src"])
                if not relative.is_relative_to(asset_dir):
                    raise ValueError("歷史素材不在此商品的比較素材目錄")
                source = inside(version / relative, version / asset_dir)
                if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                    raise ValueError("歷史素材缺少有效SHA-256")
                picture, suffix, raw = read_image(source, item["sha256"])
                if not isinstance(item["name"], str) or not item["name"]:
                    raise ValueError("歷史素材缺少檔名")
                # The prior index uses the original display name, not the snapshot filename.
                picture["name"] = item["name"]
                images.append((item["group"], source, picture, suffix, raw))
            if not images:
                raise ValueError("沒有相同分類可比較")
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as error:
            trace.append({"side": side, "version": version.name, "status": "SKIPPED", "reason": str(error)})
            continue
        trace.append({"side": side, "version": version.name, "status": "SELECTED"})
        return version.name, images
    return None, []


def build(plan_path, output_root):
    plan_path = Path(plan_path).resolve(strict=True)
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    if not isinstance(plan, dict):
        raise ValueError("計畫必須是JSON物件")
    output_root = Path(output_root).resolve()
    title = plan.get("title", "主圖／詳情頁差異預覽")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title必須是非空文字")
    groups = plan.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("計畫必須包含非空的groups清單")
    if not all(isinstance(group, dict) for group in groups):
        raise ValueError("每個groups項目必須是物件")

    legacy = "product_id" not in plan and "variants" not in plan and all("remade" in group for group in groups)
    product_id = "舊計畫：" + title if legacy else plan.get("product_id")
    if not isinstance(product_id, str) or not product_id.strip():
        raise ValueError("product_id必須是可識別同商品的非空文字")
    labels = {"original": "原圖", "remade": "重製版本"} if legacy else MODE_LABELS
    default_variants = {"original": {}, "remade": {}}
    variants = default_variants if legacy else plan.get("variants")
    if not isinstance(variants, dict) or variants.keys() - labels.keys():
        raise ValueError("variants只能包含original、localized及redesigned")

    product_key = hashlib.sha256(product_id.encode()).hexdigest()
    asset_dir = ASSET_ROOT / product_key
    index_file = INDEX_ROOT / f"{product_key}.json"
    name = safe_title(title)
    preview = f"{name}_{PREVIEW_NAME}"
    if (output_root / preview).exists() or (output_root / preview).is_symlink():
        preview = f"{name}_{product_key[:12]}_{PREVIEW_NAME}"
    for target in (asset_dir, index_file, Path(preview)):
        inside(output_root / target, output_root)
        path = output_root / target
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"比較頁或素材已存在，請使用新版本：{path}")

    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count("__COMPARISON_DATA__") != 1:
        raise ValueError("比較頁模板缺少唯一資料位置")

    data = {"title": title, "variants": {}, "groups": []}
    rendered_groups = {}
    snapshots, provenance, trace = [], [], []

    def add_image(group, side, source, picture, suffix, raw):
        items = group.setdefault(side, [])
        relative = asset_dir / f"{group['id']}-{side}-{len(items) + 1:03}{suffix}"
        item = {"src": relative.as_posix(), **picture}
        items.append(item)
        snapshots.append((relative, raw))
        provenance.append({"group": group["id"], "side": side, "source_path": str(source), **item})

    included_sides = set()
    for group in groups:
        group_id, label = group.get("id"), group.get("label")
        if not isinstance(group_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", group_id):
            raise ValueError("分類id須為英數、底線或連字號，並以英文字母開頭")
        if group_id in rendered_groups:
            raise ValueError(f"分類id重複：{group_id}")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("分類label必須是非空文字")
        unexpected = group.keys() - {"id", "label", *labels}
        if unexpected:
            raise ValueError(f"分類中有不支援的欄位：{sorted(unexpected)}")
        rendered = rendered_groups[group_id] = {"id": group_id, "label": label}
        for side in labels:
            entries = group.get(side, [])
            if not isinstance(entries, list):
                raise ValueError(f"{label}的{side}必須是圖片清單")
            for entry in entries:
                if not isinstance(entry, str) or not entry:
                    raise ValueError("圖片清單須提供檔案路徑文字")
                source = (plan_path.parent / entry).resolve(strict=True)
                add_image(rendered, side, source, *read_image(source))
                included_sides.add(side)
        if len(rendered) == 2:
            raise ValueError(f"{label}至少需要一套圖片")
        if legacy and len(rendered) != 4:
            raise ValueError(f"{label}的舊格式需要原圖和重製圖片")
        data["groups"].append(rendered)

    for side in included_sides:
        metadata = variants.get(side, {} if side == "original" else None)
        if not isinstance(metadata, dict):
            raise ValueError(f"{side}需要驗收狀態")
        status = metadata.get("status")
        if status not in ("PASS", "REVIEW") and (status is not None or side not in ("original", "remade")):
            raise ValueError(f"{side}的status必須為PASS或REVIEW")
        version = metadata.get("version", output_root.name)
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"{side}的version必須是非空文字")
        if side != "original" and version != output_root.name and status != "PASS":
            raise ValueError(f"{side}的歷史成果須已驗收通過")
        data["variants"][side] = {"label": labels[side], "version": version, "status": status}
    if "original" not in included_sides or len(included_sides) < 2:
        raise ValueError("比較頁需要原圖及至少一套本次結果或已驗收的歷史成果")

    if not legacy:
        for side in ("localized", "redesigned"):
            if side in included_sides:
                continue
            version, images = previous_mode(
                output_root, asset_dir, index_file, product_id, side, rendered_groups, trace
            )
            if version is not None:
                data["variants"][side] = {"label": labels[side], "version": version, "status": "PASS"}
                for group_id, source, picture, suffix, raw in images:
                    add_image(rendered_groups[group_id], side, source, picture, suffix, raw)

    if not any(sum(bool(group.get(side)) for side in labels) >= 2 for group in data["groups"]):
        raise ValueError("比較頁至少需要一個含兩套圖片的分類")

    # Keep the mode order stable in both the page and its index.
    data["variants"] = {side: data["variants"][side] for side in labels if side in data["variants"]}
    counts = [{"id": group["id"], "label": group["label"],
               **{side: len(group.get(side, [])) for side in data["variants"]}} for group in data["groups"]]
    index = {"preview": preview, "product_id": product_id, "title": title, "variants": data["variants"],
             "template_sha256": hashlib.sha256(template.encode()).hexdigest(),
             "groups": counts, "images": provenance, "history": trace, "visual_status": "NOT_VERIFIED_BY_TOOL"}
    payload = json.dumps(data, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    html = template.replace("__COMPARISON_DATA__", payload).encode("utf-8")
    serialized_index = json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8")

    # Preflight completes before any write; clean up only files this run created.
    asset_path = output_root / asset_dir
    asset_path.mkdir(parents=True)
    written = []
    try:
        for relative, raw in snapshots:
            (output_root / relative).write_bytes(raw)
        (output_root / index_file).parent.mkdir(parents=True, exist_ok=True)
        for relative, content in ((index_file, serialized_index), (Path(preview), html)):
            path = output_root / relative
            with path.open("xb") as stream:
                written.append(path)
                stream.write(content)
    except OSError:
        for path in reversed(written):
            path.unlink(missing_ok=True)
        shutil.rmtree(asset_path)
        raise
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, help="已確認來源、模式與順序的JSON計畫")
    parser.add_argument("--output", required=True, help="本次輸出的版本目錄")
    args = parser.parse_args()
    try:
        result = build(args.plan, args.output)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        parser.exit(2, f"比較頁未完成：{error}\n")
    print(json.dumps({key: result[key] for key in ("preview", "variants", "groups", "history", "visual_status")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
