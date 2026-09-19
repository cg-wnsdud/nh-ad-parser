"""fc87 Gemma로 페이지의 상품 소유권·복구·단일 라벨을 함께 판정한다."""
from __future__ import annotations

import copy
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from nh_parser.vlm import client as vlm_client


PRODUCT_IDS = ["page_common", "product_1", "product_2", "product_3", "product_4", "unknown"]
ACTIONS = ["new_region", "attach_context", "page_common", "decorative", "needs_review"]
ABSTAIN = "해당없음"


def _short_text(value: Any, limit: int = 700) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _overlay(
    image: Image.Image,
    regions: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    offset: tuple[int, int] = (0, 0),
) -> Image.Image:
    out = image.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    font_size = max(14, min(30, out.width // 55))
    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError:  # Pillow 구버전 호환
        font = ImageFont.load_default()
    width = max(2, out.width // 600)
    for item, color, key in (
        *((region, "#1976D2", "region_id") for region in regions),
        *((candidate, "#F57C00", "candidate_id") for candidate in candidates),
    ):
        box = item.get("bbox")
        if not box:
            continue
        ox, oy = offset
        local_box = [box[0] - ox, box[1] - oy, box[2] - ox, box[3] - oy]
        draw.rectangle(local_box, outline=color, width=width)
        label = str(item[key])
        x0, y0 = int(local_box[0]), max(0, int(local_box[1]) - font_size - 4)
        text_box = draw.textbbox((x0, y0), label, font=font)
        draw.rectangle(text_box, fill=color)
        draw.text((x0, y0), label, fill="white", font=font)
    return out


def _schema(region_ids: list[str], candidate_ids: list[str], labels: list[str]) -> dict[str, Any]:
    region_enum = region_ids or ["__none__"]
    candidate_enum = candidate_ids or ["__none__"]
    return {
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
            "products": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "string", "enum": PRODUCT_IDS[1:5]},
                        "name": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["product_id", "name", "confidence"],
                    "additionalProperties": False,
                },
            },
            "region_decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "region_id": {"type": "string", "enum": region_enum},
                        "product_id": {"type": "string", "enum": PRODUCT_IDS},
                        "label": {"type": "string", "enum": [*labels, ABSTAIN]},
                        "needs_split": {"type": "boolean"},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "region_id", "product_id", "label", "needs_split", "confidence", "reason",
                    ],
                    "additionalProperties": False,
                },
            },
            "recovery_decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate_id": {"type": "string", "enum": candidate_enum},
                        "action": {"type": "string", "enum": ACTIONS},
                        "target_region_id": {"type": "string", "enum": ["", *region_ids]},
                        "product_id": {"type": "string", "enum": PRODUCT_IDS},
                        "label": {"type": "string", "enum": [*labels, ABSTAIN]},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "candidate_id", "action", "target_region_id", "product_id", "label",
                        "confidence", "reason",
                    ],
                    "additionalProperties": False,
                },
            },
            "missing_visible_text": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "near_id": {"type": "string"},
                        "y_ratio": {"type": "number"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["text", "near_id", "y_ratio", "confidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "analysis", "products", "region_decisions", "recovery_decisions", "missing_visible_text",
        ],
        "additionalProperties": False,
    }


def _listing(regions: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> str:
    rows = ["[기존 REGION — 파란색]"]
    for region in regions:
        rows.append(
            f"- {region['region_id']} bbox={region.get('bbox')} "
            f"layout={region.get('label')} text={_short_text(region.get('text'))}"
        )
    rows.append("\n[복구 CANDIDATE — 주황색]")
    for candidate in candidates:
        rows.append(
            f"- {candidate['candidate_id']} bbox={candidate['bbox']} "
            f"text={_short_text(candidate.get('text'))}"
        )
    return "\n".join(rows)


def analyze_page(
    image: Image.Image,
    page: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    template_id: str,
    labels: list[str],
    crop_offset: tuple[int, int] = (0, 0),
    band_note: str = "페이지 전체",
) -> dict[str, Any]:
    """제공된 ID만 사용하도록 제한한 페이지 문맥 판정을 수행한다."""
    regions = page["regions"]
    region_ids = [str(region["region_id"]) for region in regions]
    candidate_ids = [str(candidate["candidate_id"]) for candidate in candidates]
    prompt = f"""당신은 농협 금융광고 페이지 구조 판독기입니다.

선택된 광고 템플릿: {template_id}
허용 라벨: {', '.join(labels)}

이미지의 파란 박스는 기존 PaddleX Region이고 주황 박스는 OCR/PDF 줄로 만든 복구 후보입니다.
다음 작업을 한 번에 수행하세요.

1. products: 실제 상품이 여러 개면 product_1부터 시각적 순서대로 정의하세요.
2. region_decisions: 아래 REGION ID를 정확히 한 번씩 반환하고 상품 소유권과 주 라벨 하나를 정하세요.
   페이지 공통 제목·회사명·심의번호는 page_common, 불명확하면 unknown입니다.
   서로 다른 의미가 실제로 섞였을 때만 needs_split=true로 하세요.
3. recovery_decisions: 아래 CANDIDATE ID를 정확히 한 번씩 반환하세요.
   - new_region: 독립 의미 영역
   - attach_context: target_region_id의 설명·유의사항이지만 bbox는 별도 보존
   - page_common: 회사명·심의번호 등 공통 영역
   - decorative: 심의 텍스트로 쓰지 않는 순수 장식
   - needs_review: 확정 불가
4. missing_visible_text: 이미지에는 분명히 보이지만 REGION/CANDIDATE 목록에 전혀 없는 문구만 적으세요.

중요 규칙:
- bbox를 새로 만들지 말고 제공된 ID만 선택하세요.
- 한 Region에는 허용 라벨 하나 또는 {ABSTAIN}만 선택하세요.
- 다른 상품의 내용을 같은 product_id에 섞지 마세요.
- 표·고지·상품 설명이라는 PaddleX layout 이름은 힌트일 뿐 정답으로 믿지 마세요.
- 원문에 없는 문구를 추측하지 마세요.

페이지 크기: {page['canvas']}
현재 이미지 범위: {band_note}
{_listing(regions, candidates)}
"""
    result = vlm_client.chat_json(
        [
            {"type": "text", "text": prompt},
            vlm_client.image_part(
                _overlay(image, regions, candidates, offset=crop_offset),
                box=(1400, 2400), quality=90,
            ),
        ],
        schema_name="parser_v2_page_semantics",
        schema=_schema(region_ids, candidate_ids, labels),
        max_tokens=min(8000, 1200 + 150 * (len(region_ids) + len(candidate_ids))),
    )
    return validate_decisions(result, region_ids, candidate_ids, labels)


def _center(item: dict[str, Any], axis: str) -> float:
    box = item.get("bbox") or [0, 0, 0, 0]
    if axis == "x":
        return (float(box[0]) + float(box[2])) / 2
    return (float(box[1]) + float(box[3])) / 2


def partition_for_semantic_bands(
    page: dict[str, Any], candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """긴 페이지 ID를 겹치지 않는 2~4개 의미 판정 밴드에 정확히 한 번 배정한다."""
    tiling = page.get("tiling") or {}
    if tiling.get("decision") != "split":
        return []
    axis = str(tiling.get("axis") or "y")
    width, height = (int(value) for value in page["canvas"])
    limit = width if axis == "x" else height
    count = min(4, max(2, int(tiling.get("pieces") or 2)))
    overlap = min(200, max(60, int(limit * 0.03)))
    output = []
    for index in range(count):
        start = round(limit * index / count)
        end = round(limit * (index + 1) / count)
        upper = end if index + 1 < count else limit + 1
        regions = [
            item for item in page["regions"]
            if start <= _center(item, axis) < upper
        ]
        selected_candidates = [
            item for item in candidates
            if start <= _center(item, axis) < upper
        ]
        if not regions and not selected_candidates:
            continue
        crop_start, crop_end = max(0, start - overlap), min(limit, end + overlap)
        crop = (
            [crop_start, 0, crop_end, height]
            if axis == "x" else [0, crop_start, width, crop_end]
        )
        output.append({
            "index": index + 1,
            "count": count,
            "axis": axis,
            "range": [start, end],
            "crop": crop,
            "regions": regions,
            "candidates": selected_candidates,
        })
    return output


def analyze_page_context(
    image: Image.Image,
    page: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    template_id: str,
    labels: list[str],
) -> dict[str, Any]:
    """일반 페이지는 1회, 긴 페이지는 2~4개 밴드로 의미 판정을 수행한다."""
    bands = partition_for_semantic_bands(page, candidates)
    if not bands:
        result = analyze_page(
            image, page, candidates, template_id=template_id, labels=labels,
        )
        result["semantic_bands"] = [{"index": 1, "count": 1, "mode": "whole"}]
        return result

    combined: dict[str, Any] = {
        "analysis": "",
        "products": [],
        "region_decisions": [],
        "recovery_decisions": [],
        "missing_visible_text": [],
        "semantic_bands": [],
    }
    products: dict[str, dict[str, Any]] = {}
    for band in bands:
        x0, y0, x1, y1 = band["crop"]
        cropped = image.crop((x0, y0, x1, y1))
        result = analyze_page(
            cropped,
            {**page, "regions": band["regions"]},
            band["candidates"],
            template_id=template_id,
            labels=labels,
            crop_offset=(x0, y0),
            band_note=(
                f"긴 페이지 {band['axis']}축 band {band['index']}/{band['count']}, "
                f"원본 crop={band['crop']}. product_1은 문서 전체에서 같은 상품 ID입니다."
            ),
        )
        combined["analysis"] += (
            ("\n" if combined["analysis"] else "")
            + f"band {band['index']}: {result.get('analysis') or ''}"
        )
        combined["region_decisions"].extend(result["region_decisions"])
        combined["recovery_decisions"].extend(result["recovery_decisions"])
        combined["missing_visible_text"].extend(result.get("missing_visible_text") or [])
        for product in result.get("products") or []:
            products.setdefault(str(product.get("product_id")), copy.deepcopy(product))
        combined["semantic_bands"].append({
            key: copy.deepcopy(band[key])
            for key in ("index", "count", "axis", "range", "crop")
        })
    combined["products"] = list(products.values())
    validated = validate_decisions(
        combined,
        [str(region["region_id"]) for region in page["regions"]],
        [str(candidate["candidate_id"]) for candidate in candidates],
        labels,
    )
    validated["semantic_bands"] = combined["semantic_bands"]
    return validated


def validate_decisions(
    result: dict[str, Any],
    region_ids: list[str],
    candidate_ids: list[str],
    labels: list[str],
) -> dict[str, Any]:
    """모르는 ID·중복을 버리고 누락 ID는 검수 대상으로 보충한다."""
    allowed_labels = set(labels)

    def unique(items: list[dict[str, Any]], key: str, known: set[str]) -> dict[str, dict[str, Any]]:
        output = {}
        for item in items or []:
            identifier = str(item.get(key) or "")
            if identifier in known and identifier not in output:
                output[identifier] = item
        return output

    region_map = unique(result.get("region_decisions") or [], "region_id", set(region_ids))
    for region_id in region_ids:
        region_map.setdefault(region_id, {
            "region_id": region_id,
            "product_id": "unknown",
            "label": ABSTAIN,
            "needs_split": False,
            "confidence": 0.0,
            "reason": "VLM 응답에서 누락되어 검수 필요",
        })
    candidate_map = unique(
        result.get("recovery_decisions") or [], "candidate_id", set(candidate_ids)
    )
    for candidate_id in candidate_ids:
        candidate_map.setdefault(candidate_id, {
            "candidate_id": candidate_id,
            "action": "needs_review",
            "target_region_id": "",
            "product_id": "unknown",
            "label": ABSTAIN,
            "confidence": 0.0,
            "reason": "VLM 응답에서 누락되어 검수 필요",
        })
    for item in [*region_map.values(), *candidate_map.values()]:
        if item.get("label") not in allowed_labels:
            item["label"] = None
        if item.get("product_id") not in PRODUCT_IDS:
            item["product_id"] = "unknown"
    return {
        "analysis": str(result.get("analysis") or ""),
        "products": list(result.get("products") or []),
        "region_decisions": list(region_map.values()),
        "recovery_decisions": list(candidate_map.values()),
        "missing_visible_text": list(result.get("missing_visible_text") or []),
    }
