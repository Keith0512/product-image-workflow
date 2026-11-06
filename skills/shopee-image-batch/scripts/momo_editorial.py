"""Validate and export reviewed MOMO pages; content selection remains an agent task."""
import csv
import json
import re
import shutil
import unicodedata
from pathlib import Path

from PIL import Image, ImageOps
from shopee_tools import inspect_image, resolve_m, save_json_new, sha256, validate_visual_reviews

CATEGORIES = {
    "identity", "specifications", "package_contents", "limitations",
    "operation", "safety", "key_features",
}
CORE_CATEGORIES = {"identity", "specifications", "package_contents"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def box_valid(box, width, height):
    return (isinstance(box, list) and len(box) == 4
            and all(type(x) is int for x in box)
            and 0 <= box[0] < box[2] <= width
            and 0 <= box[1] < box[3] <= height)


def compact(value):
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def contains_protected(text, value):
    token = compact(value)
    # Do not accept 6PSI inside 16PSI, 6 inside 6.5, or +6/-6 as the same value.
    if any(c.isdigit() for c in token):
        return re.search(r"(?<![\d.,+\-−–])" + re.escape(token) + r"(?![\d.])", compact(text)) is not None
    return token in compact(text)


def validate_plan(plan):
    """Validate declared coverage and file versions, not the truth of image content."""
    require(plan.get("source_reviewed") is True and nonempty(plan.get("review_evidence")),
            "M編輯前須完成來源、SKU及接縫檢視")
    base = Path(plan.get("result_root", ""))
    explicit = plan.get("explicit_source")
    require(base.is_absolute() and (not explicit or Path(explicit).is_absolute()),
            "result_root與explicit_source須用絕對路徑")
    selected = resolve_m(base, explicit)
    sources = plan.get("sources")
    require(isinstance(sources, list) and len(sources) == len(selected["images"]),
            "計畫須列出唯一來源的全部圖片，取捨另記於內容清單")
    actual_by_path, sizes = {}, {}
    for index, (row, actual) in enumerate(zip(sources, selected["images"])):
        require(isinstance(row, dict) and Path(row.get("path", "")).is_absolute(),
                "來源須為絕對路徑")
        path = str(Path(row["path"]).resolve())
        require(path == actual["path"] and row.get("sha256") == actual["sha256"],
                "來源雜湊、清單或排序已變更")
        require(row.get("trim_top_normalized_px", 0) == 0,
                "編輯模式以source_regions記錄取材，不使用固定切片的頂端去重")
        if index:
            boundary = row.get("boundary_before", {})
            require(boundary.get("type") in {"CONTINUOUS", "OVERLAP", "INDEPENDENT"}
                    and nonempty(boundary.get("evidence")), "M來源接縫尚未確認")
        actual_by_path[path] = actual
        w, h = actual["width"], actual["height"]
        sizes[path] = (h, w) if actual["orientation"] in {5, 6, 7, 8} else (w, h)
    source_reviews = validate_visual_reviews(sources)

    pages = plan.get("pages")
    require(isinstance(pages, list) and 1 <= len(pages) <= 20,
            "MOMO編輯版必須先排成1至20頁；不得只取前20頁")
    page_hashes = set()
    for number, page in enumerate(pages, 1):
        require(isinstance(page, dict) and type(page.get("page")) is int
                and page["page"] == number, "M頁碼須由1連續編號")
        path = Path(page.get("path", ""))
        require(path.is_absolute() and path.is_file(), "M編輯稿須為存在的絕對路徑")
        info = inspect_image(path)
        require((info["width"], info["height"], info["mode"], info["format"], info["orientation"])
                == (1000, 1500, "RGB", "PNG", 1),
                "M編輯稿須為已排好的1000×1500 RGB PNG，不得用工具拉伸補滿")
        require(page.get("sha256") not in page_hashes, "M編輯稿含內容完全相同的重複頁")
        page_hashes.add(page.get("sha256"))
    page_reviews = validate_visual_reviews(pages)

    categories = plan.get("coverage_categories")
    require(isinstance(categories, list), "缺少必要資訊類別盤點")
    category_map = {}
    for entry in categories:
        require(isinstance(entry, dict) and entry.get("id") in CATEGORIES
                and entry["id"] not in category_map, "必要資訊類別不符或重複")
        require(entry.get("applicability") in {"required", "not_applicable"}
                and nonempty(entry.get("reason")), "各資訊類別須記適用性及實際依據")
        require(entry["id"] not in CORE_CATEGORIES or entry["applicability"] == "required",
                "商品識別、主要規格及包裝內容不得省略")
        category_map[entry["id"]] = entry
    require(set(category_map) == CATEGORIES, "必要資訊類別盤點不完整")

    review = plan.get("content_review", {})
    require(review.get("status") == "PASS" and nonempty(review.get("evidence")),
            "必要資訊、條件、安全事項及規格矛盾須先完成實際內容核對")
    items = plan.get("content_items")
    require(isinstance(items, list) and items, "缺少逐項內容取捨與必要資訊覆蓋表")
    item_ids, accounted_sources, used_pages, required_categories = set(), set(), set(), set()
    for item in items:
        require(isinstance(item, dict) and nonempty(item.get("id"))
                and item["id"] not in item_ids, "內容項目ID缺失或重複")
        item_ids.add(item["id"])
        category = item.get("category")
        require(category in CATEGORIES | {"supporting"}, "內容項目類別不符")
        require(type(item.get("required")) is bool, "每項資訊須明確標記是否必要")
        decision = item.get("decision")
        require(decision in {"keep", "merge", "omit"} and nonempty(item.get("reason")),
                "內容取捨須為keep/merge/omit並記理由")
        require(not item["required"] or decision != "omit", "不得省略必要資訊")
        if category in category_map and category_map[category]["applicability"] == "not_applicable":
            require(not item["required"] and decision == "omit", "不適用類別不得同時宣稱有必要資訊")
        refs = item.get("source_regions")
        require(isinstance(refs, list) and refs, "每項內容須可追溯至唯一來源的實際區域")
        for ref in refs:
            require(isinstance(ref, dict) and Path(ref.get("path", "")).is_absolute(),
                    "內容來源須為絕對路徑")
            path = str(Path(ref["path"]).resolve())
            require(path in actual_by_path and ref.get("sha256") == actual_by_path[path]["sha256"],
                    "內容引用超出唯一來源或雜湊不符")
            require(box_valid(ref.get("box"), *sizes[path]), "來源區域超出EXIF轉正後原圖範圍")
            accounted_sources.add(path)
        original, final = item.get("original_text"), item.get("final_text")
        require(isinstance(original, str) and isinstance(final, str), "須記來源文案/影像摘要與最終文案")
        values = item.get("protected_values")
        require(isinstance(values, list) and all(nonempty(v) for v in values), "protected_values須為字串清單")
        placements = item.get("placements")
        require(isinstance(placements, list), "內容須記成品頁碼及區域")
        if decision == "omit":
            require(not placements and not final.strip(), "省略項目不得同時列入成品")
            continue
        require(placements, "保留或整併的資訊未對應任何成品頁")
        for value in values:
            require(contains_protected(original, value) and contains_protected(final, value),
                    f"受保護的數值/單位/條件遺漏或變更：{item['id']}／{value}")
        if item["required"]:
            required_categories.add(category)
        for placement in placements:
            require(isinstance(placement, dict) and type(placement.get("page")) is int
                    and 1 <= placement["page"] <= len(pages), "內容指向不存在的M頁碼")
            require(box_valid(placement.get("box"), 1000, 1500), "內容區域超出M頁面或為空")
            operation = placement.get("operation")
            require(operation in {"text_reflow", "image_crop"}, "內容操作須為文字重排或原圖裁切縮放")
            if operation == "text_reflow":
                require(nonempty(final), "文字重排須記最終文案")
            else:
                index = placement.get("source_region")
                require(type(index) is int and 0 <= index < len(refs), "原圖裁切須指定source_region索引")
                a, b = refs[index]["box"], placement["box"]
                sw, sh, dw, dh = a[2]-a[0], a[3]-a[1], b[2]-b[0], b[3]-b[1]
                require(abs(dh - dw * sh / sw) <= 1.5, "商品原圖裁切縮放不可改變長寬比例")
            used_pages.add(placement["page"])
    require(accounted_sources == set(actual_by_path), "有來源圖片未記保留、整併或省略理由")
    require(used_pages == set(range(1, len(pages)+1)), "M編輯版包含未對應內容的空頁")
    needed = {x for x, row in category_map.items() if row["applicability"] == "required"}
    require(needed <= required_categories, "適用的必要資訊類別尚未覆蓋到成品")
    return selected, source_reviews, page_reviews


def write_csv(path, columns, rows):
    with path.open("x", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def build_editorial(plan, output, encoder):
    selected, source_reviews, page_reviews = validate_plan(plan)
    output = Path(output).resolve()
    source = Path(selected["source_root"])
    require(output != source and not source.is_relative_to(output), "輸出不得等同來源或包含來源")
    require(not output.is_relative_to(source) or (output != source / "輸出"
            and output.is_relative_to(source / "輸出")), "來源內輸出僅允許放在「輸出」下的新版本目錄")
    output.mkdir(parents=True, exist_ok=False)
    records = output / "0_處理紀錄"
    drafts, staged = records / "work/M編輯稿", records / "work/M輸出暫存"
    drafts.mkdir(parents=True)
    staged.mkdir()
    save_json_new(records / "M計畫.json", plan)
    pages, pending = [], []
    # Recheck all source versions immediately before creating derived files.
    for row in plan["sources"]:
        require(sha256(row["path"]) == row["sha256"], "處理中來源變動；須重新核對與驗收")
    for row in plan["pages"]:
        require(sha256(row["path"]) == row["sha256"], "處理中M編輯稿變動；須重新驗收")
        number = row["page"]
        working = drafts / f"M詳情_{number:02d}.png"
        shutil.copyfile(row["path"], working)
        with Image.open(working) as im:
            image = ImageOps.exif_transpose(im).copy()
            icc = im.info.get("icc_profile")
        if icc:
            import io
            from PIL import ImageCms
            image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                              ImageCms.createProfile("sRGB"), outputMode="RGB")
        data, quality = encoder(image)
        path = staged / f"M詳情_{number:02d}.jpg"
        with path.open("xb") as f:
            f.write(data)
        if len(data) > 500000:
            pending.append(f"{path.name}容量超限，整套保留待重排")
        pages.append({"page": number, "width": 1000, "height": 1500,
                      "bytes": len(data), "quality": quality, "sha256": sha256(path),
                      "working_path": str(working.relative_to(output)),
                      "working_sha256": sha256(working), "reviewed_draft_sha256": row["sha256"]})
    relative = "6_待人工審核/M容量異常" if pending else "5_M詳情頁"
    destination = output / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged.rename(destination)
    for page in pages:
        page["path"] = f"{relative}/M詳情_{page['page']:02d}.jpg"

    coverage, choices, mappings = [], [], []
    for item in plan["content_items"]:
        numbers = sorted({p["page"] for p in item["placements"]})
        choices.append({"item_id": item["id"], "category": item["category"],
                        "required": item["required"], "decision": item["decision"],
                        "reason": item["reason"], "original_text": item["original_text"],
                        "final_text": item["final_text"], "page_numbers": json.dumps(numbers),
                        "source_regions": json.dumps(item["source_regions"], ensure_ascii=False)})
        if item["required"]:
            coverage.append({"item_id": item["id"], "category": item["category"],
                             "page_numbers": json.dumps(numbers), "final_text": item["final_text"],
                             "protected_values": json.dumps(item["protected_values"], ensure_ascii=False),
                             "record_check": "VALID_NOT_SEMANTIC_VERIFICATION"})
        for placement in item["placements"]:
            page = pages[placement["page"]-1]
            refs = ([item["source_regions"][placement["source_region"]]]
                    if placement["operation"] == "image_crop" else item["source_regions"])
            for ref in refs:
                mappings.append({"output_path": page["path"], "output_sha256": page["sha256"],
                                 "item_id": item["id"], "source_id": str(Path(ref["path"]).resolve()),
                                 "source_sha256": ref["sha256"], "source_box": json.dumps(ref["box"]),
                                 "source_coordinate_space": "EXIF轉正後來源像素，半開區間",
                                 "output_box": json.dumps(placement["box"]),
                                 "change_type": placement["operation"],
                                 "evidence_path": "0_處理紀錄/M計畫.json", "status": "REVIEW"})
    write_csv(output / "圖片來源對應.csv", ["output_path", "output_sha256", "item_id", "source_id",
              "source_sha256", "source_box", "source_coordinate_space", "output_box", "change_type",
              "evidence_path", "status"], mappings)
    write_csv(records / "M必要資訊覆蓋.csv", ["item_id", "category", "page_numbers", "final_text",
              "protected_values", "record_check"], coverage)
    write_csv(records / "M內容取捨.csv", ["item_id", "category", "required", "decision", "reason",
              "original_text", "final_text", "page_numbers", "source_regions"], choices)
    manifest = {
        "status": "REVIEW", "mode": "momo-editorial", "source_mode": selected["source_mode"],
        "source_root": selected["source_root"], "width": 1000, "page_height": 1500,
        "expected_count": len(pages), "actual_count": len(pages), "pages": pages,
        "sources": plan["sources"], "coverage_categories": plan["coverage_categories"],
        "content_items": plan["content_items"], "content_review": plan["content_review"],
        "content_records": "VALID_NOT_SEMANTIC_VERIFICATION",
        "pre_export_visual_reviews": {"sources": source_reviews, "pages": page_reviews},
        "files_status": "REVIEW" if pending else "FILE_OK", "upload_ready": False,
        "visual_status": "NOT_VERIFIED_AFTER_COMPRESSION",
        "pending": pending + ["壓縮後逐頁視覺、必要資訊實際覆蓋及完整HTML待驗證"],
        "record_hashes": {name: sha256(records / name)
                          for name in ["M計畫.json", "M必要資訊覆蓋.csv", "M內容取捨.csv"]},
        "note": "工具僅核對已填紀錄、版本與規格；不自動選圖、理解安全內容或判定平台已接受。",
    }
    save_json_new(records / "M切片紀錄.json", manifest)
    return manifest
