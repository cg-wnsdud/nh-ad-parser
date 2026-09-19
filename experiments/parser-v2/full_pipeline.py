"""parser-v2 OCR 결과를 fc87 Gemma 판정과 P1/P3까지 연결한다."""
from __future__ import annotations

import copy
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from PIL import Image

from nh_parser.review.catalog import load_catalog
from nh_parser.vlm import client as vlm_client

from export_v2 import build_p1, build_p3
from recovery import build_recovery_candidates
from semantic import analyze_page_context, analyze_product_labels
from templates import PAGE_COMMON, resolve_product_templates, review_units


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _media_map(tasks: list[dict[str, Any]], media_dir: Path) -> dict[tuple[str, int], Path]:
    output = {}
    for task in tasks:
        data = task["data"]
        name = unquote(str(data["image"]).split("pages/", 1)[1])
        output[(str(data["source_file"]), int(data["page_no"]))] = media_dir / name
    return output


def _prepare_page(page: dict[str, Any]) -> dict[str, Any]:
    prepared = copy.deepcopy(page)
    page_no = int(prepared["page_no"])
    for region_order, region in enumerate(prepared.get("regions") or [], start=1):
        region["origin"] = "paddlex"
        region["engine_order"] = region_order
        region["bbox_source"] = "paddlex_layout"
        region["bbox_quality"] = "exact"
        for index, line in enumerate(region.get("lines") or []):
            line["line_ref"] = f"p{page_no}/{region['region_id']}/L{index:03d}"
    for index, line in enumerate(prepared.get("unassigned_lines") or []):
        line["line_ref"] = f"p{page_no}/unassigned/L{index:03d}"
    prepared["raw_unassigned_lines"] = copy.deepcopy(prepared.get("unassigned_lines") or [])
    prepared["recovery_candidates"] = build_recovery_candidates(
        prepared.get("unassigned_lines") or [], page_no=page_no,
    )
    return prepared


def _document_template(product_templates: dict[str, Any]) -> dict[str, Any]:
    """문서 단위 요약. 상품이 하나면 그 템플릿, 여럿이면 목록을 남긴다.

    P3의 `document.template`은 기존 계약이라 유지하되, 상품이 섞인 문서에서
    임의로 하나를 고르지 않는다. 실제 판정 근거는 `product_templates`다.
    """
    products = {
        product_id: resolution
        for product_id, resolution in product_templates.items()
        if product_id != PAGE_COMMON
    }
    ids = sorted({
        str(resolution.get("template_id"))
        for resolution in products.values()
        if resolution.get("template_id")
    })
    if len(ids) == 1:
        only = next(
            resolution for resolution in products.values()
            if resolution.get("template_id") == ids[0]
        )
        return {**only, "scope": "document"}
    return {
        "template_id": None,
        "status": "multi_product" if ids else "unresolved",
        "source": "per_product",
        "confidence": None,
        "reason": (
            f"상품별 템플릿 {len(ids)}종: {', '.join(ids)}" if ids
            else "상품별 템플릿을 확정하지 못함"
        ),
        "candidates": ids,
        "scope": "per_product",
    }


def _assign_reading_order(page: dict[str, Any]) -> None:
    """엔진 순서를 유지하고 연결된 복구 Region만 대상 바로 뒤에 둔다.

    전체 y 정렬은 2단 문서의 좌우 열을 교차시키므로 사용하지 않는다. 같은 대상에
    붙는 복구 후보끼리만 원본 bbox의 y/x 순서로 배치한다.
    """
    original = [region for region in page["regions"] if region.get("origin") == "paddlex"]
    recovered = [region for region in page["regions"] if region.get("origin") == "recovery"]
    attached: dict[str, list[dict[str, Any]]] = {}
    unattached = []
    original_ids = {str(region["region_id"]) for region in original}
    for region in recovered:
        target = str(region.get("related_region_id") or "")
        if target in original_ids:
            attached.setdefault(target, []).append(region)
        else:
            unattached.append(region)
    for rows in attached.values():
        rows.sort(key=lambda region: ((region.get("bbox") or [0, 0])[1], (region.get("bbox") or [0, 0])[0]))

    ordered = []
    for region in original:
        ordered.append(region)
        ordered.extend(attached.get(str(region["region_id"]), []))
    # 명시적 대상이 없는 복구 후보는 위치로 임의 재배열하지 않고 후보 생성 순서를 둔다.
    ordered.extend(unattached)

    product_orders: dict[str, int] = {}
    for order, region in enumerate(ordered, start=1):
        product_id = str(region.get("product_id") or "unknown")
        product_orders[product_id] = product_orders.get(product_id, 0) + 1
        region["reading_order"] = order
        region["product_reading_order"] = product_orders[product_id]
    page["regions"] = ordered


def _apply_ownership(
    page: dict[str, Any], result: dict[str, Any],
) -> None:
    """1단계 결과를 적용한다. 라벨은 상품별 템플릿이 정해진 뒤 2단계에서 붙인다."""
    by_region = {item["region_id"]: item for item in result["region_decisions"]}
    for region in page.get("regions") or []:
        decision = by_region[region["region_id"]]
        region["product_id"] = decision["product_id"]
        region["semantic_label"] = None
        region["needs_split"] = bool(decision.get("needs_split"))
        region["needs_review"] = bool(
            decision.get("confidence", 0.0) < 0.7 or decision.get("product_id") == "unknown"
        )
        region["semantic_decision"] = decision

    candidates = {item["candidate_id"]: item for item in page["recovery_candidates"]}
    remaining = {line["line_ref"]: line for line in page.get("unassigned_lines") or []}
    ignored_lines = []
    for decision in result["recovery_decisions"]:
        candidate = candidates[decision["candidate_id"]]
        candidate["decision"] = decision
        if decision["action"] == "decorative":
            # VLM의 decorative 판정은 오판할 수 있다. 특히 표의 `담보명`,
            # `보장금액` 같은 짧은 머리글을 장식으로 보는 사례가 있었다.
            # OCR/PDF가 실제 텍스트와 좌표를 준 이상 P3에서 삭제하지 않고,
            # 일반 복구 Region으로 보존한 뒤 검수 대상으로 표시한다.
            ignored_lines.extend(
                {**copy.deepcopy(line), "ignore_reason": decision["reason"]}
                for line in candidate["lines"]
            )
        lines = [copy.deepcopy(line) for line in candidate["lines"]]
        for line in lines:
            remaining.pop(line["line_ref"], None)
        region_id = candidate["candidate_id"]
        page["regions"].append({
            "region_id": region_id,
            "bbox": copy.deepcopy(candidate["bbox"]),
            "label": "recovery",
            "text": candidate["text"],
            "text_source": "ocr_pdf_recovery",
            "lines": lines,
            "origin": "recovery",
            "bbox_source": candidate["bbox_source"],
            "bbox_quality": candidate["bbox_quality"],
            "product_id": (
                "page_common" if decision["action"] == "page_common" else decision["product_id"]
            ),
            "semantic_label": None,
            "needs_split": False,
            "needs_review": bool(
                decision["action"] in {"needs_review", "decorative"}
                or decision.get("confidence", 0.0) < 0.7
            ),
            "vlm_excluded": decision["action"] == "decorative",
            "related_region_id": decision.get("target_region_id") or None,
            "semantic_decision": decision,
        })
    page["unassigned_lines"] = sorted(
        remaining.values(), key=lambda line: ((line.get("bbox") or [0, 0])[1], (line.get("bbox") or [0, 0])[0])
    )
    page["ignored_lines"] = ignored_lines
    page["coarse_missing_candidates"] = [
        {
            **item,
            "bbox": None,
            "bbox_source": "vlm_page_context",
            "bbox_quality": "coarse",
            "status": "requires_crop_ocr",
        }
        for item in result.get("missing_visible_text") or []
    ]
    page["semantic_analysis"] = result.get("analysis")
    page["products"] = result.get("products") or []
    page["table_areas"] = copy.deepcopy(result.get("table_areas") or [])
    page["semantic_bands"] = copy.deepcopy(result.get("semantic_bands") or [])
    _assign_reading_order(page)


def _label_pages(
    pages: list[dict[str, Any]],
    product_templates: dict[str, dict[str, Any]],
    images: dict[int, Image.Image],
) -> None:
    """상품별로 나눠 라벨링한다.

    한 요청에 두 상품의 구분값을 함께 넣으면 모델이 상품1 Region에 템플릿B의
    구분값을 붙일 수 있다. enum은 둘 다 허용 목록에 있으니 막아주지 못한다.
    """
    for page in pages:
        groups: dict[str, list[dict[str, Any]]] = {}
        for region in page.get("regions") or []:
            groups.setdefault(str(region.get("product_id") or "unknown"), []).append(region)

        notes = []
        for product_id, regions in groups.items():
            resolution = product_templates.get(product_id) or {}
            labels = list(resolution.get("labels") or [])
            if not labels:
                # 템플릿을 확정하지 못한 상품은 라벨을 억지로 붙이지 않고 남긴다.
                for region in regions:
                    region["semantic_label"] = None
                    region["needs_review"] = True
                    region["label_decision"] = {
                        "region_id": region["region_id"],
                        "label": None,
                        "needs_split": False,
                        "confidence": 0.0,
                        "reason": f"{product_id} 템플릿 미확정으로 라벨링 보류",
                    }
                continue
            result = analyze_product_labels(
                images[int(page["page_no"])],
                page,
                regions,
                product_id=product_id,
                product_name=resolution.get("product_name"),
                template_id=resolution.get("template_id"),
                labels=labels,
            )
            notes.append(result.get("analysis") or "")
            by_region = {item["region_id"]: item for item in result["region_labels"]}
            for region in regions:
                decision = by_region[str(region["region_id"])]
                region["semantic_label"] = decision["label"]
                region["label_decision"] = decision
                if decision["needs_split"]:
                    region["needs_split"] = True
                if decision["label"] is None or decision["confidence"] < 0.7:
                    region["needs_review"] = True
        page["label_analysis"] = "\n".join(value for value in notes if value)


def _append_p3_label_studio(
    tasks: list[dict[str, Any]], p3_documents: list[dict[str, Any]], out: Path,
) -> None:
    """기존 진단 탭 뒤에 최종 P3 Region을 시각 검수할 탭을 추가한다."""
    from lab import view

    pages: dict[tuple[str, int], list[dict[str, Any]]] = {}
    labels: set[str] = set()
    for document in p3_documents:
        source_file = str(document["document"]["source_file"])
        for page in document["pages"]:
            rows = []
            for region in page["regions"]:
                semantic = region.get("label") or "미분류"
                label = f"P3 {region.get('product_id') or 'unknown'} | {semantic}"
                labels.add(label)
                rows.append({
                    "bbox": region["bbox"],
                    "label": label,
                    "content": (
                        f"{region['region_id']} | origin={region['origin']} | "
                        f"review={region['needs_review']} | {region['selected_text']}"
                    ),
                })
            pages[(source_file, int(page["page_no"]))] = rows

    enriched = copy.deepcopy(tasks)
    for task in enriched:
        key = (str(task["data"]["source_file"]), int(task["data"]["page_no"]))
        rows = pages.get(key, [])
        if not rows:
            continue
        # 모든 P3 bbox는 이 task의 원본 canvas 좌표계다.
        first = next(
            page
            for document in p3_documents
            if str(document["document"]["source_file"]) == key[0]
            for page in document["pages"]
            if int(page["page_no"]) == key[1]
        )
        width, height = (int(value) for value in first["canvas"])
        task["predictions"].append({
            "model_version": f"{task['data']['run_name']} 4-p3-semantic",
            "result": [
                result
                for index, row in enumerate(rows)
                if (result := view.rectangle(
                    row, width, height, box_id=f"p3_{index:03d}",
                ))
            ],
        })
    _write_json(out / "label-studio.json", enriched)

    config_path = out / "labeling-config.xml"
    config = config_path.read_text(encoding="utf-8")
    new_rows = "\n".join(
        f'    <Label value="{label}" background="{view.color_for(label, sorted(labels))}" />'
        for label in sorted(labels)
        if f'value="{label}"' not in config
    )
    if new_rows:
        config = config.replace("  </RectangleLabels>", f"{new_rows}\n  </RectangleLabels>")
    config = config.replace(
        "parser-v2 — 1-raw parsing / 2-text source / 3-unassigned",
        "parser-v2 — 1-raw / 2-canonical / 3-unassigned / 4-P3 semantic",
    )
    config_path.write_text(config, encoding="utf-8")


def _write_stage_views(documents: list[dict[str, Any]], out: Path) -> None:
    """상품 소유권과 VLM 판정을 P1보다 작은 중간 산출물로 분리한다."""
    ownership, vlm_evidence = [], []
    for document in documents:
        ownership.append({
            "source_file": document["source_file"],
            "product_templates": copy.deepcopy(document.get("product_templates") or {}),
            "review_units": copy.deepcopy(document.get("review_units") or []),
            "pages": [{
                "page_no": page["page_no"],
                "products": copy.deepcopy(page.get("products") or []),
                "regions": [{
                    "region_id": region["region_id"],
                    "product_id": region.get("product_id"),
                    "label": region.get("semantic_label"),
                    "reading_order": region.get("reading_order"),
                    "product_reading_order": region.get("product_reading_order"),
                    "related_region_id": region.get("related_region_id"),
                } for region in page.get("regions") or []],
            } for page in document.get("pages") or []],
        })
        vlm_evidence.append({
            "source_file": document["source_file"],
            "classification": copy.deepcopy(document.get("classification")),
            "template": copy.deepcopy(document.get("template")),
            "product_templates": copy.deepcopy(document.get("product_templates") or {}),
            "pages": [{
                "page_no": page["page_no"],
                "status": page.get("semantic_status"),
                "analysis": page.get("semantic_analysis"),
                "label_analysis": page.get("label_analysis"),
                "table_areas": copy.deepcopy(page.get("table_areas") or []),
                "semantic_bands": copy.deepcopy(page.get("semantic_bands") or []),
                "region_decisions": [
                    copy.deepcopy(region.get("semantic_decision"))
                    for region in page.get("regions") or []
                    if region.get("semantic_decision")
                ],
                "label_decisions": [
                    copy.deepcopy(region.get("label_decision"))
                    for region in page.get("regions") or []
                    if region.get("label_decision")
                ],
                "recovery_decisions": [
                    copy.deepcopy(candidate.get("decision"))
                    for candidate in page.get("recovery_candidates") or []
                    if candidate.get("decision")
                ],
                "missing_visible_text": copy.deepcopy(
                    page.get("coarse_missing_candidates") or []
                ),
            } for page in document.get("pages") or []],
        })
    _write_json(out / "03-ownership.json", ownership)
    _write_json(out / "04-vlm-evidence.json", vlm_evidence)


def run_full_pipeline(
    documents: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    *,
    out: Path,
    media_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """기준 OCR 결과에 fc87 Gemma 의미 판정을 붙이고 P1/P3를 저장한다."""
    vlm_client.reset_stats()
    started = time.time()
    media = _media_map(tasks, media_dir)
    catalog = load_catalog()
    p1_documents, p3_documents = [], []

    for raw_document in documents:
        pages = [_prepare_page(page) for page in raw_document["pages"]]
        source_file = str(raw_document["source_file"])
        first_image = Image.open(media[(source_file, int(pages[0]["page_no"]))]).convert("RGB")
        classification = vlm_client.classify(first_image, source_file)
        doc = {
            "doc_id": Path(source_file).stem,
            "source_file": source_file,
            "file_type": Path(source_file).suffix.lower().lstrip("."),
            "product_group": classification.product_group,
            "ad_type": classification.ad_type,
            "product_name_shown": classification.product_name_shown,
            "category_source": classification.category_source,
            "classification_confidence": classification.confidence,
            "classification": asdict(classification),
            "pages": pages,
        }
        # 1단계 — 상품 소유권. 템플릿을 아직 모르므로 라벨은 붙이지 않는다.
        images = {
            int(page["page_no"]): Image.open(
                media[(source_file, int(page["page_no"]))]
            ).convert("RGB")
            for page in pages
        }
        for page in pages:
            result = analyze_page_context(
                images[int(page["page_no"])], page, page["recovery_candidates"],
            )
            _apply_ownership(page, result)
            page["semantic_status"] = "complete"

        # 2단계 — 상품별 템플릿. 소유권이 나와야 상품군을 알 수 있으므로 여기서 푼다.
        product_templates = resolve_product_templates(doc, catalog)
        doc["product_templates"] = product_templates
        doc["template"] = _document_template(product_templates)

        # 3단계 — 상품별 라벨링.
        _label_pages(pages, product_templates, images)
        doc["review_units"] = review_units(pages, product_templates)

        p1 = build_p1(doc)
        p3 = build_p3(p1)
        p1_documents.append(p1)
        p3_documents.append(p3)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in source_file)
        final_dir = out / "final"
        final_dir.mkdir(parents=True, exist_ok=True)
        _write_json(final_dir / f"{safe}.p1.json", p1)
        _write_json(final_dir / f"{safe}.p3.json", p3)

    _write_stage_views(p1_documents, out)
    _write_json(out / "05-p1.json", p1_documents)
    _write_json(out / "06-p3.json", p3_documents)
    _append_p3_label_studio(tasks, p3_documents, out)
    stats = {
        "elapsed_seconds": round(time.time() - started, 3),
        "by_schema": copy.deepcopy(vlm_client.STATS),
    }
    _write_json(out / "vlm-stats.json", stats)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["semantic_pipeline"] = stats
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return p1_documents, p3_documents
