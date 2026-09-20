"""fc87 Gemma 페이지 판정 — 소유권 판정과 라벨링을 **두 단계로 나눈다**.

1단계 소유권: 상품을 나누고 Region을 상품에 배정하며 복구 후보의 처리를 정한다.
2단계 라벨링: 상품마다 **그 상품의 템플릿 구분값만** enum으로 주고 라벨을 붙인다.

나눈 이유는 두 가지다.

- 템플릿은 상품군에 따라 달라지는데, 상품군은 소유권 판정이 나와야 알 수 있다.
  한 번에 하면 문서 템플릿 하나를 모든 상품에 강요하게 된다.
- 한 요청에 두 상품의 구분값을 함께 넣으면 모델이 상품1 Region에 템플릿B의
  구분값을 붙일 수 있다. enum은 둘 다 허용 목록에 있으니 막아주지 못한다.
"""
from __future__ import annotations

import copy
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from nh_parser.vlm import client as vlm_client


PRODUCT_IDS = ["page_common", "product_1", "product_2", "product_3", "product_4", "unknown"]
ACTIONS = ["new_region", "attach_context", "page_common", "decorative", "needs_review"]
PRODUCT_GROUPS = ["예금성", "대출성", "카드", "투자성", "판단불가"]
NAME_SHOWN = ["노출", "미노출", "판단불가"]
# 표처럼 보이는 영역의 두 갈래. `table` 만 하나로 합친다.
TABLE_KINDS = ["table", "field_list"]
ABSTAIN = "해당없음"

# 한 라벨링 요청이 감당할 Region 수. 넘으면 나눠 부른다. 목록이 길어지면 뒤쪽
# 항목의 정확도가 떨어지고, 응답이 잘리면 그 묶음이 통째로 망가진다.
# 실측(2026-09-19): 모델이 Region 하나당 약 440자를 쓴다. 43개를 한 번에 물었더니
# 18,840자에서 잘렸다. 15개면 약 6,600자로 넉넉히 들어간다.
LABEL_CHUNK = 15


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


# ── 1단계 · 소유권 ──────────────────────────────────────────────────


def _ownership_schema(region_ids: list[str], candidate_ids: list[str]) -> dict[str, Any]:
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
                        # 상품별 템플릿을 고르려면 상품군과 상품명 노출 여부가
                        # 상품 단위로 필요하다. 문서 단위 분류로는 섞인 문서를
                        # 처리할 수 없다.
                        "product_group": {"type": "string", "enum": PRODUCT_GROUPS},
                        "product_name_shown": {"type": "string", "enum": NAME_SHOWN},
                        "confidence": {"type": "number"},
                    },
                    "required": [
                        "product_id", "name", "product_group", "product_name_shown",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            },
            "region_decisions": {
                "type": "array",
                # 상한이 없으면 모델이 같은 ID를 계속 다시 뱉어 응답이 끝나지 않는다.
                # 실측(2026-09-19): region_labels 15개짜리 요청이 35,376자까지 늘었다.
                # 각 ID를 정확히 한 번씩 받는 것이 계약이므로 개수를 고정한다.
                "minItems": len(region_ids),
                "maxItems": len(region_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "region_id": {"type": "string", "enum": region_enum},
                        "product_id": {"type": "string", "enum": PRODUCT_IDS},
                        "needs_split": {"type": "boolean"},
                        "confidence": {"type": "number"},
                        # Region마다 자유 서술 reason을 받으면 응답의 대부분이
                        # 거기에 들어간다. 실측: Region 30개 페이지에서 응답이
                        # 14,827자까지 늘어 잘렸고, 예산을 키우자 120초 타임아웃이
                        # 났다. 근거는 analysis 한 곳에 모으고 행마다 두지 않는다.
                    },
                    "required": ["region_id", "product_id", "needs_split", "confidence"],
                    "additionalProperties": False,
                },
            },
            "recovery_decisions": {
                "type": "array",
                "minItems": len(candidate_ids),
                "maxItems": len(candidate_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate_id": {"type": "string", "enum": candidate_enum},
                        "action": {"type": "string", "enum": ACTIONS},
                        "target_region_id": {"type": "string", "enum": ["", *region_ids]},
                        "product_id": {"type": "string", "enum": PRODUCT_IDS},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "candidate_id", "action", "target_region_id", "product_id",
                        "confidence", "reason",
                    ],
                    "additionalProperties": False,
                },
            },
            "table_areas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "approx_bbox_pct": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4,
                        },
                        # 시각적으로는 둘 다 격자라 기하학으로 구분할 수 없다.
                        # 합칠지 말지는 의미 판정이므로 여기서 받는다.
                        "kind": {"type": "string", "enum": TABLE_KINDS},
                        "note": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["approx_bbox_pct", "kind", "note", "confidence"],
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
            "analysis", "products", "region_decisions", "recovery_decisions",
            "table_areas", "missing_visible_text",
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


def analyze_page_ownership(
    image: Image.Image,
    page: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    crop_offset: tuple[int, int] = (0, 0),
    band_note: str = "페이지 전체",
) -> dict[str, Any]:
    """상품 소유권과 복구 후보 처리를 판정한다. 라벨은 여기서 붙이지 않는다."""
    regions = page["regions"]
    region_ids = [str(region["region_id"]) for region in regions]
    candidate_ids = [str(candidate["candidate_id"]) for candidate in candidates]
    prompt = f"""당신은 농협 금융광고 페이지 구조 판독기입니다.

이미지의 파란 박스는 기존 PaddleX Region이고 주황 박스는 OCR/PDF 줄로 만든 복구 후보입니다.
다음 작업을 한 번에 수행하세요. **이 단계에서는 라벨을 붙이지 않습니다.**

1. products: 실제 상품이 여러 개면 product_1부터 시각적 순서대로 정의하세요.
   상품마다 product_group(예금성/대출성/카드/투자성)과 상품명 노출 여부를 따로 판정하세요.
   한 광고 안에 성격이 다른 상품이 섞여 있을 수 있으므로 파일명에 끌려가지 마세요.
2. region_decisions: 아래 REGION ID를 정확히 한 번씩 반환하고 상품 소유권을 정하세요.
   회사명·로고·심의번호·연락처·공통 안내문구, 그리고 특정 상품 하나가 아니라
   광고 전체에 걸리는 유의사항은 **반드시 page_common**입니다.
   unknown은 상품 소속도 공통도 아니라고 판단될 때만 쓰는 마지막 선택지입니다.
   서로 다른 의미가 실제로 섞였을 때만 needs_split=true로 하세요.
3. recovery_decisions: 아래 CANDIDATE ID를 정확히 한 번씩 반환하세요.
   - new_region: 독립 의미 영역
   - attach_context: target_region_id의 설명·유의사항이지만 bbox는 별도 보존
   - page_common: 회사명·심의번호 등 공통 영역
   - decorative: 심의 텍스트로 쓰지 않는 순수 장식
   - needs_review: 확정 불가
4. table_areas: 행과 열로 나란히 놓인 영역이 있으면 대략 위치를 **페이지 대비
   백분율** [x0, y0, x1, y1]로 알려주세요(0~100). 픽셀 값이 아닙니다.
   PaddleX가 표로 잡지 못한 영역도 보이는 대로 적고, kind를 반드시 구분하세요.
   - table: 머리글이 있고 칸 전체가 **하나의 항목**을 설명하는 진짜 표.
     예) `구분 | 적립율` 머리글 아래 값이 들어찬 표
   - field_list: 왼쪽이 항목명, 오른쪽이 그 값인 **서로 다른 항목의 나열**.
     예) `대출대상 | …`, `대출한도 | …`, `대출기간 | …` 이 세로로 이어지는 블록
   둘을 헷갈리면 서로 다른 항목이 한 덩어리로 묶여 항목별 구분이 사라집니다.
5. missing_visible_text: 이미지에는 분명히 보이지만 REGION/CANDIDATE 목록에 전혀 없는 문구만 적으세요.

중요 규칙:
- analysis는 세 문장 이내로 쓰세요. 길면 응답이 잘립니다.
- bbox를 새로 만들지 말고 제공된 ID만 선택하세요(table_areas의 근사 위치는 예외).
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
        schema_name="parser_v2_page_ownership",
        schema=_ownership_schema(region_ids, candidate_ids),
        # 한글은 토큰당 글자 수가 적어 영문 기준 예산으로 잡으면 잘린다. 실측:
        # `4. 카드상품`(Region 30 + 후보 2)의 응답이 14,827자였고 5,360토큰
        # 예산에서 잘려 JSON 파싱이 3회 모두 실패했다.
        max_tokens=min(10000, 1500 + 160 * len(region_ids) + 320 * len(candidate_ids)),
    )
    return validate_ownership(result, region_ids, candidate_ids)


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
) -> dict[str, Any]:
    """일반 페이지는 1회, 긴 페이지는 2~4개 밴드로 소유권 판정을 수행한다."""
    bands = partition_for_semantic_bands(page, candidates)
    if not bands:
        result = analyze_page_ownership(image, page, candidates)
        result["semantic_bands"] = [{"index": 1, "count": 1, "mode": "whole"}]
        return result

    combined: dict[str, Any] = {
        "analysis": "",
        "products": [],
        "region_decisions": [],
        "recovery_decisions": [],
        "table_areas": [],
        "missing_visible_text": [],
        "semantic_bands": [],
    }
    products: dict[str, dict[str, Any]] = {}
    width, height = (int(value) for value in page["canvas"])
    for band in bands:
        x0, y0, x1, y1 = band["crop"]
        cropped = image.crop((x0, y0, x1, y1))
        result = analyze_page_ownership(
            cropped,
            {**page, "regions": band["regions"]},
            band["candidates"],
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
        # 밴드 crop 기준 백분율을 페이지 전체 기준으로 되돌린다. 그러지 않으면
        # 밴드 2의 표가 페이지 상단에 있는 것으로 기록된다.
        for area in result.get("table_areas") or []:
            box = [float(value) for value in area.get("approx_bbox_pct") or []]
            if len(box) != 4:
                continue
            combined["table_areas"].append({
                **area,
                "approx_bbox_pct": [
                    (x0 + box[0] / 100 * (x1 - x0)) / width * 100,
                    (y0 + box[1] / 100 * (y1 - y0)) / height * 100,
                    (x0 + box[2] / 100 * (x1 - x0)) / width * 100,
                    (y0 + box[3] / 100 * (y1 - y0)) / height * 100,
                ],
            })
        for product in result.get("products") or []:
            products.setdefault(str(product.get("product_id")), copy.deepcopy(product))
        combined["semantic_bands"].append({
            key: copy.deepcopy(band[key])
            for key in ("index", "count", "axis", "range", "crop")
        })
    combined["products"] = list(products.values())
    validated = validate_ownership(
        combined,
        [str(region["region_id"]) for region in page["regions"]],
        [str(candidate["candidate_id"]) for candidate in candidates],
    )
    validated["semantic_bands"] = combined["semantic_bands"]
    return validated


def validate_ownership(
    result: dict[str, Any],
    region_ids: list[str],
    candidate_ids: list[str],
) -> dict[str, Any]:
    """모르는 ID·중복을 버리고 누락 ID는 검수 대상으로 보충한다."""

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
            "needs_split": False,
            "confidence": 0.0,
            "reason": "VLM 응답에서 누락되어 검수 필요",
        })
    for item in region_map.values():
        # 소유권 스키마에서 행별 reason을 뺐다. 누락 보충분만 사유를 갖는다.
        item.setdefault("reason", "")
    candidate_map = unique(
        result.get("recovery_decisions") or [], "candidate_id", set(candidate_ids)
    )
    for candidate_id in candidate_ids:
        candidate_map.setdefault(candidate_id, {
            "candidate_id": candidate_id,
            "action": "needs_review",
            "target_region_id": "",
            "product_id": "unknown",
            "confidence": 0.0,
            "reason": "VLM 응답에서 누락되어 검수 필요",
        })
    for item in [*region_map.values(), *candidate_map.values()]:
        if item.get("product_id") not in PRODUCT_IDS:
            item["product_id"] = "unknown"
    return {
        "analysis": str(result.get("analysis") or ""),
        "products": list(result.get("products") or []),
        "region_decisions": list(region_map.values()),
        "recovery_decisions": list(candidate_map.values()),
        "table_areas": list(result.get("table_areas") or []),
        "missing_visible_text": list(result.get("missing_visible_text") or []),
    }


# ── 2단계 · 상품별 라벨링 ───────────────────────────────────────────


def _label_schema(region_ids: list[str], labels: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
            "region_labels": {
                "type": "array",
                # 비어 있는 배열도 스키마상 유효하면 재시도가 걸리지 않는다.
                # 실측: 모델이 판정을 analysis 문장에만 쓰고 배열을 비워 보냈다.
                # 상한이 없으면 반대로 같은 ID를 반복해 응답이 끝나지 않는다.
                "minItems": len(region_ids),
                "maxItems": len(region_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "region_id": {"type": "string", "enum": region_ids or ["__none__"]},
                        "label": {"type": "string", "enum": [*labels, ABSTAIN]},
                        "needs_split": {"type": "boolean"},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["region_id", "label", "needs_split", "confidence", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["analysis", "region_labels"],
        "additionalProperties": False,
    }


def analyze_product_labels(
    image: Image.Image,
    page: dict[str, Any],
    regions: list[dict[str, Any]],
    *,
    product_id: str,
    product_name: str | None,
    template_id: str | None,
    labels: list[str],
) -> dict[str, Any]:
    """한 상품(또는 페이지 공통) 소속 Region에만 그 상품의 구분값을 붙인다."""
    if not regions or not labels:
        return {"analysis": "", "region_labels": [], "calls": 0}

    scope = (
        "페이지 공통 영역입니다. 특정 상품에 속하지 않는 회사명·유의사항·심의번호입니다."
        if product_id == "page_common"
        else f"상품 {product_id}({product_name or '이름 미상'}) 소속 영역입니다."
    )
    merged: list[dict[str, Any]] = []
    analyses: list[str] = []
    calls = 0
    for start in range(0, len(regions), LABEL_CHUNK):
        chunk = regions[start:start + LABEL_CHUNK]
        region_ids = [str(region["region_id"]) for region in chunk]
        rows = "\n".join(
            f"- {region['region_id']} bbox={region.get('bbox')} "
            f"text={_short_text(region.get('text'))}"
            for region in chunk
        )
        prompt = f"""당신은 농협 금융광고 구분값 라벨러입니다.

{scope}
적용 템플릿: {template_id or '공통(모든 템플릿 교집합)'}
허용 라벨: {', '.join(labels)}

아래 REGION ID {len(region_ids)}개를 **모두** region_labels 배열에 정확히 한 번씩
넣고 주 라벨 **하나**를 고르세요.

- 판정은 반드시 region_labels 배열에 넣으세요. analysis에 문장으로 적으면 무효입니다.
- analysis에는 전체 요약 한 문장만, 각 reason은 40자 이내로 쓰세요.
- 허용 라벨 중 맞는 것이 없으면 {ABSTAIN}을 고르세요. 억지로 고르지 마세요.
- 한 Region에 서로 다른 구분값이 실제로 섞여 있으면 needs_split=true로 표시하고
  둘 중 하나를 임의로 고르지 마세요.
- 이 목록은 이미 이 상품 소속으로 확정된 영역입니다. 상품 소유권을 다시 판정하지 마세요.
- 파란 박스가 이미지 위의 해당 영역입니다. 위치와 주변 문맥을 함께 보세요.

페이지 크기: {page['canvas']}
{rows}
"""
        parts = [
            {"type": "text", "text": prompt},
            vlm_client.image_part(
                _overlay(image, chunk, []), box=(1400, 2400), quality=90,
            ),
        ]
        schema = _label_schema(region_ids, labels)
        result = vlm_client.chat_json(
            parts,
            schema_name="parser_v2_product_labels",
            schema=schema,
            max_tokens=min(12000, 1200 + 500 * len(region_ids)),
        )
        calls += 1
        if not (result.get("region_labels") or []):
            # 배열이 비면 모든 Region이 "누락되어 검수 필요"로 떨어진다. 실측에서
            # 모델이 판정을 analysis 문장에만 담아 보낸 적이 있어 한 번 다시 묻는다.
            retry = list(parts)
            retry[0] = {
                "type": "text",
                "text": prompt + (
                    "\n\n직전 응답은 region_labels가 비어 무효였습니다. "
                    f"설명 없이 REGION ID {len(region_ids)}개의 판정만 배열로 반환하세요."
                ),
            }
            result = vlm_client.chat_json(
                retry,
                schema_name="parser_v2_product_labels",
                schema=schema,
                max_tokens=min(12000, 1200 + 500 * len(region_ids)),
            )
            calls += 1
        analyses.append(str(result.get("analysis") or ""))
        merged.extend(
            validate_labels(result, region_ids, labels)["region_labels"]
        )
    return {
        "analysis": "\n".join(value for value in analyses if value),
        "region_labels": merged,
        "calls": calls,
    }


def validate_labels(
    result: dict[str, Any], region_ids: list[str], labels: list[str],
) -> dict[str, Any]:
    """허용 라벨 밖의 값은 버리고 누락 Region은 검수 대상으로 채운다."""
    allowed = set(labels)
    output: dict[str, dict[str, Any]] = {}
    for item in result.get("region_labels") or []:
        region_id = str(item.get("region_id") or "")
        if region_id not in set(region_ids) or region_id in output:
            continue
        label = item.get("label")
        output[region_id] = {
            "region_id": region_id,
            "label": label if label in allowed else None,
            "needs_split": bool(item.get("needs_split")),
            "confidence": float(item.get("confidence") or 0.0),
            "reason": str(item.get("reason") or ""),
        }
    for region_id in region_ids:
        output.setdefault(region_id, {
            "region_id": region_id,
            "label": None,
            "needs_split": False,
            "confidence": 0.0,
            "reason": "VLM 응답에서 누락되어 검수 필요",
        })
    return {
        "analysis": str(result.get("analysis") or ""),
        "region_labels": [output[region_id] for region_id in region_ids],
    }


# ── 3단계 · 줄 단위 분할 ────────────────────────────────────────────


def _line_label_schema(line_refs: list[str], labels: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
            "line_labels": {
                "type": "array",
                "minItems": len(line_refs),
                "maxItems": len(line_refs),
                "items": {
                    "type": "object",
                    "properties": {
                        "line_ref": {"type": "string", "enum": line_refs or ["__none__"]},
                        "label": {"type": "string", "enum": [*labels, ABSTAIN]},
                        "confidence": {"type": "number"},
                    },
                    "required": ["line_ref", "label", "confidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["analysis", "line_labels"],
        "additionalProperties": False,
    }


def analyze_region_lines(
    image: Image.Image,
    region: dict[str, Any],
    *,
    template_id: str | None,
    labels: list[str],
) -> dict[str, Any]:
    """한 Region 안의 줄마다 구분값을 정한다.

    Region 하나에 구분값이 여럿 섞여 있을 때만 부른다. 실측(2026-09-20,
    `1. 예금성상품(거치식)` p1_r001): `가입대상개인 / 가입금액100만원 이상 /
    (원 단위)` 세 줄이 한 Region 이라 라벨이 `가입대상` 하나만 붙고 `가입금액`
    구분값이 사라졌다.

    줄에는 이미 bbox 가 있으므로 좌표를 새로 만들 필요가 없다. 모델은 줄 ID 에
    라벨만 붙인다.
    """
    lines = [line for line in region.get("lines") or [] if line.get("line_ref")]
    if len(lines) < 2 or not labels:
        return {"analysis": "", "line_labels": [], "calls": 0}
    line_refs = [str(line["line_ref"]) for line in lines]
    rows = "\n".join(
        f"- {line['line_ref']} {' '.join(str(line.get('text') or '').split())}"
        for line in lines
    )
    prompt = f"""당신은 농협 금융광고 구분값 라벨러입니다.

아래는 영역 하나 안의 줄 목록입니다. 이 영역에는 서로 다른 구분값이 섞여 있습니다.
**줄마다** 어느 구분값에 속하는지 정하세요.

적용 템플릿: {template_id or '공통'}
허용 라벨: {', '.join(labels)}

- 아래 LINE ID {len(line_refs)}개를 **모두** line_labels 배열에 정확히 한 번씩 넣으세요.
- 판정은 반드시 배열에 넣으세요. analysis 에 문장으로 적으면 무효입니다.
- 앞줄에 딸린 설명·단위·괄호는 **앞줄과 같은 라벨**을 주세요.
  예) `가입금액100만원 이상` 다음의 `(원 단위)` 는 같은 가입금액입니다.
- 허용 라벨 중 맞는 것이 없으면 {ABSTAIN} 을 고르세요.

영역 텍스트 줄:
{rows}
"""
    result = vlm_client.chat_json(
        [
            {"type": "text", "text": prompt},
            vlm_client.image_part(image, box=(1200, 1200), quality=92),
        ],
        schema_name="parser_v2_region_line_labels",
        schema=_line_label_schema(line_refs, labels),
        max_tokens=min(10000, 800 + 200 * len(line_refs)),
    )
    allowed = set(labels)
    seen: dict[str, dict[str, Any]] = {}
    for item in result.get("line_labels") or []:
        ref = str(item.get("line_ref") or "")
        if ref not in set(line_refs) or ref in seen:
            continue
        label = item.get("label")
        seen[ref] = {
            "line_ref": ref,
            "label": label if label in allowed else None,
            "confidence": float(item.get("confidence") or 0.0),
        }
    return {
        "analysis": str(result.get("analysis") or ""),
        "line_labels": [
            seen.get(ref, {"line_ref": ref, "label": None, "confidence": 0.0})
            for ref in line_refs
        ],
        "calls": 1,
    }
