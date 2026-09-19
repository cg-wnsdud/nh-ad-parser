"""parser-v2의 전체 근거(P1)와 심의 입력(P3)을 만든다."""
from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any


P1_VERSION = "nh-ad-parse-evidence-v2"
P3_VERSION = "nh-ad-region-review-input-v2"


def _line_refs(region: dict[str, Any]) -> list[str]:
    return [str(line.get("line_ref") or "") for line in region.get("lines") or []]


def build_p1(document: dict[str, Any]) -> dict[str, Any]:
    evidence = copy.deepcopy(document)
    evidence["contract"] = {
        "version": P1_VERSION,
        "purpose": "OCR/PDF/VLM 관측과 정확한 페이지 bbox 보존",
        "bbox_policy": "OCR/PDF/레이아웃 좌표만 exact; VLM은 ID 선택만 수행",
    }
    region_count = sum(len(page.get("regions") or []) for page in evidence.get("pages") or [])
    evidence["summary"] = {
        "page_count": len(evidence.get("pages") or []),
        "region_count": region_count,
        "recovery_region_count": sum(
            1
            for page in evidence.get("pages") or []
            for region in page.get("regions") or []
            if region.get("origin") == "recovery"
        ),
        "unassigned_line_count": sum(
            len(page.get("unassigned_lines") or []) for page in evidence.get("pages") or []
        ),
    }
    return evidence


def build_p3(evidence: dict[str, Any]) -> dict[str, Any]:
    """Region ID를 심의 근거와 화면 bbox 사이의 안정된 연결점으로 사용한다."""
    pages_out = []
    location_index: dict[str, dict[str, Any]] = {}
    labels: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_refs: list[str] = []
    region_count = 0

    product_templates = evidence.get("product_templates") or {}

    for page in evidence.get("pages") or []:
        page_no = int(page["page_no"])
        canvas = list(page["canvas"])
        regions_out = []
        for region in page.get("regions") or []:
            text = str(region.get("text") or "").strip()
            if not text and not region.get("bbox"):
                continue
            region_id = str(region["region_id"])
            refs = _line_refs(region)
            if any(not ref for ref in refs):
                raise ValueError(f"line_ref가 없는 Region: {region_id}")
            all_refs.extend(refs)
            item = {
                "region_id": region_id,
                "page_no": page_no,
                "product_id": region.get("product_id"),
                "scope": {
                    "page_no": page_no,
                    "product_id": region.get("product_id"),
                    "template_id": (
                        product_templates.get(str(region.get("product_id")), {})
                    ).get("template_id"),
                },
                "bbox": copy.deepcopy(region.get("bbox")),
                "canvas": canvas,
                "layout": {
                    "label": region.get("label"),
                    "score": region.get("layout_score"),
                },
                "selected_text": text,
                "selected_source": region.get("text_source"),
                "selection_status": (
                    "parser_v2_requires_review"
                    if region.get("needs_review") or region.get("needs_split")
                    else "parser_v2_selected"
                ),
                "selection_reason": (
                    (region.get("semantic_decision") or {}).get("reason")
                ),
                "label": region.get("semantic_label"),
                "labels": ([{
                    "label": region.get("semantic_label"),
                    "line_refs": refs,
                }] if region.get("semantic_label") else []),
                "line_refs": refs,
                "bbox_source": region.get("bbox_source") or "paddlex_layout",
                "bbox_quality": region.get("bbox_quality") or "exact",
                "needs_review": bool(region.get("needs_review") or region.get("needs_split")),
                "needs_split": bool(region.get("needs_split")),
                "related_region_id": region.get("related_region_id"),
                "origin": region.get("origin") or "paddlex",
                "reading_order": region.get("reading_order"),
                "product_reading_order": region.get("product_reading_order"),
                "vlm_excluded": bool(region.get("vlm_excluded")),
            }
            regions_out.append(item)
            region_count += 1
            location_index[region_id] = {
                "page_no": page_no,
                "bbox": copy.deepcopy(item["bbox"]),
                "canvas": canvas,
                "bbox_quality": item["bbox_quality"],
            }
            if item["label"]:
                labels[str(item["label"])].append({
                    "page_no": page_no,
                    "region_id": region_id,
                    "product_id": item["product_id"],
                    "line_refs": refs,
                })

        unassigned = []
        for line in page.get("unassigned_lines") or []:
            ref = str(line.get("line_ref") or "")
            if not ref:
                raise ValueError(f"line_ref가 없는 미배정 줄: page={page_no}")
            all_refs.append(ref)
            unassigned.append({
                "line_ref": ref,
                "page_no": page_no,
                "bbox": copy.deepcopy(line.get("bbox")),
                "canvas": canvas,
                "selected_text": line.get("text") or "",
                "selected_source": line.get("source") or "parser",
                "bbox_quality": "exact" if line.get("bbox") else "none",
                "needs_review": True,
            })
        pages_out.append({
            "page_no": page_no,
            "parse_status": page.get("parse_status") or "ok",
            "canvas": canvas,
            "products": copy.deepcopy(page.get("products") or []),
            "regions": regions_out,
            "unassigned_text": unassigned,
            "coarse_missing_candidates": copy.deepcopy(page.get("coarse_missing_candidates") or []),
        })

    if len(all_refs) != len(set(all_refs)):
        raise ValueError("P3 line_ref 소유권이 중복되었습니다")
    needs_review_regions = [
        {
            "page_no": page["page_no"],
            "region_id": region["region_id"],
            "selection_status": region["selection_status"],
            "reason": region.get("selection_reason"),
        }
        for page in pages_out
        for region in page["regions"]
        if region["needs_review"]
    ]
    coarse_candidates = [
        {"page_no": page["page_no"], **copy.deepcopy(candidate)}
        for page in pages_out
        for candidate in page["coarse_missing_candidates"]
    ]
    return {
        "contract": {
            "version": P3_VERSION,
            "source_evidence_version": P1_VERSION,
            "review_unit": "region",
            "result_reference": "심의 결과는 evidence_region_ids를 반환하고 bbox는 location_index로 조회",
        },
        "document": {
            key: copy.deepcopy(evidence.get(key))
            for key in ("doc_id", "source_file", "file_type", "classification", "template")
        },
        # 상품이 섞인 문서는 템플릿이 여럿이다. 심의는 `review_units`의 단위로
        # 돌리고, 각 단위의 허용 라벨은 그 상품의 템플릿에서 온다.
        "product_templates": copy.deepcopy(evidence.get("product_templates") or {}),
        "review_units": copy.deepcopy(evidence.get("review_units") or []),
        "pages": pages_out,
        "label_index": [
            {"label": label, "references": references}
            for label, references in sorted(labels.items())
        ],
        "location_index": location_index,
        "unverified_recovery_candidates": coarse_candidates,
        "diagnostics": {
            "needs_review_regions": needs_review_regions,
            "non_ok_pages": [
                {"page_no": page["page_no"], "parse_status": page["parse_status"]}
                for page in pages_out if page["parse_status"] != "ok"
            ],
        },
        "review_result_contract": {
            "required_fields": ["result", "reason", "evidence_region_ids"],
            "result_enum": ["위반", "판정불가", "충족"],
            "evidence_region_ids": "location_index에 존재하는 ID만 허용",
        },
        "summary": {
            "region_count": region_count,
            "label_count": len(labels),
            "unassigned_line_count": sum(len(page["unassigned_text"]) for page in pages_out),
            "needs_review_region_count": sum(
                1 for page in pages_out for region in page["regions"] if region["needs_review"]
            ),
            "selected_text_region_count": region_count,
            "labelled_region_count": sum(
                1 for page in pages_out for region in page["regions"] if region["labels"]
            ),
            "parser_primary_line_total": len(all_refs),
        },
    }
