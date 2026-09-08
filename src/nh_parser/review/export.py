"""전체 근거(P1)와 영역 중심 다음 단계 입력(P3)을 생성한다."""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any

from ..config import SETTINGS
from .catalog import load_catalog


EVIDENCE_VERSION = "nh-ad-parse-evidence-v1"
REVIEW_INPUT_VERSION = "nh-ad-region-review-input-v1"


def _as_dict(doc: Any) -> dict[str, Any]:
    if hasattr(doc, "model_dump"):
        return doc.model_dump(mode="json")
    if not isinstance(doc, dict):
        raise TypeError("doc은 AdDocument 또는 dict여야 합니다")
    return copy.deepcopy(doc)


def _parser_source(lines: list[dict[str, Any]]) -> str:
    sources = {str(line.get("source") or "") for line in lines}
    if sources == {"ocr"}:
        return "ocr_parser"
    if sources == {"digital"}:
        return "digital_parser"
    return "hybrid_parser"


def select_region_text(region: dict[str, Any]) -> dict[str, Any]:
    """Judge 관측을 P1 변경 없이 P3용 단일 텍스트 결정으로 투영한다."""
    lines = region.get("lines") or []
    parser_text = "\n".join(str(line.get("text") or "") for line in lines).strip()
    parser_source = _parser_source(lines)
    reading = region.get("reading_adjudication") or {}
    status = str(reading.get("status") or "")
    proposed_source = reading.get("proposed_source")
    proposed_text = str(reading.get("proposed_text") or "").strip()
    decision = reading.get("judge_decision")

    # shadow가 정본 승격을 막아 status=uncertain으로 기록했더라도 Judge가 이미지 기준으로
    # VLM 후보를 명시 선택했다면 다음 단계에서는 그 하나를 사용한다. 다만 같은 계열
    # Reader/Judge 오독 위험을 숨기지 않도록 항상 검수 대상으로 표시한다.
    if (
        proposed_source == "element_vlm"
        and proposed_text
        and decision in {"candidate_a", "candidate_b"}
    ):
        return {
            "selected_text": proposed_text,
            "selected_source": "vlm_judge",
            "selection_status": "judge_selected_vlm_requires_review",
            "needs_review": True,
            "confidence": reading.get("confidence"),
            "reason": reading.get("reason") or "Judge가 VLM 영역 판독을 선택",
        }

    if status == "agreed":
        return {
            "selected_text": parser_text,
            "selected_source": parser_source,
            "selection_status": "parser_and_vlm_agree",
            "needs_review": False,
            "confidence": reading.get("confidence"),
            "reason": reading.get("reason") or "파서와 VLM 판독이 일치",
        }

    unresolved = status in {"uncertain", "judge_failed", "reader_failed"}
    if proposed_source == "merged" or decision == "merge":
        unresolved = True
    low_confidence = any(
        line.get("source") == "ocr"
        and line.get("confidence") is not None
        and float(line["confidence"]) < SETTINGS.lowconf_reread_threshold
        for line in lines
    )
    if status and proposed_source == "canonical" and not unresolved:
        selection_status = "judge_selected_parser"
    elif unresolved:
        selection_status = "judge_unresolved_parser_fallback"
    elif not status:
        selection_status = "parser_without_vlm_comparison"
    else:
        selection_status = "parser_fallback"
    reason = reading.get("reason") or reading.get("error")
    if not reason and low_confidence:
        reason = "저신뢰 OCR 줄이 있으나 확정된 VLM 선택이 없음"
    if not reason:
        reason = "파서 원문을 전달값으로 선택"
    return {
        "selected_text": parser_text,
        "selected_source": parser_source,
        "selection_status": selection_status,
        "needs_review": bool(unresolved or low_confidence),
        "confidence": reading.get("confidence"),
        "reason": reason,
    }


def _classification(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "product_group": doc.get("product_group"),
        "ad_type": doc.get("ad_type"),
        "product_name_shown": doc.get("product_name_shown"),
        "source": doc.get("category_source"),
        "confidence": doc.get("classification_confidence"),
    }


def _attach_span_refs(labels: list[dict[str, Any]], refs: list[str]) -> list[dict[str, Any]]:
    output = copy.deepcopy(labels)
    for label in output:
        for span in label.get("spans") or []:
            start = max(0, int(span.get("line_from", 0)))
            end = min(len(refs) - 1, int(span.get("line_to", start)))
            span["line_from"], span["line_to"] = start, end
            span["line_refs"] = refs[start:end + 1] if start <= end else []
    return output


def build_evidence(
    doc: Any,
    resolution: dict[str, Any],
    labeling: dict[str, Any],
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """parse JSON을 한 글자도 버리지 않고 템플릿·Judge 근거로 보강한다."""
    evidence = _as_dict(doc)
    active = catalog or load_catalog()
    template_id = resolution.get("template_id")
    selected_template = active["templates"].get(template_id) if template_id else None
    if labeling.get("template_id") not in {None, template_id}:
        raise ValueError(
            "템플릿 선택과 구분값 판정의 template_id가 다릅니다: "
            f"{template_id!r} != {labeling.get('template_id')!r}"
        )
    allowed_labels = {
        item["gubun"] for item in (selected_template or {}).get("items") or []
    }
    evidence["contract"] = {
        "version": EVIDENCE_VERSION,
        "purpose": "all parser observations and review projection evidence",
        "text_policy": "parser text and VLM/Judge observations are both preserved",
    }
    evidence["classification"] = _classification(evidence)
    evidence["template"] = {
        **copy.deepcopy(resolution),
        "catalog_version": active["version"],
        "catalog_source": copy.deepcopy(active.get("source") or {}),
        "items": copy.deepcopy((selected_template or {}).get("items") or []),
        "labeling_status": labeling.get("status"),
        "labeling_notes": copy.deepcopy(labeling.get("notes") or []),
    }

    refs: list[str] = []
    labelled_regions = needs_review_regions = 0
    for page in evidence.get("pages") or []:
        page_no = int(page.get("page_no") or 0)
        for region in page.get("regions") or []:
            region_id = str(region.get("region_id") or "")
            region_refs: list[str] = []
            for index, line in enumerate(region.get("lines") or []):
                ref = f"p{page_no}/{region_id}/L{index:03d}"
                line["line_ref"] = ref
                line["parser_text"] = line.get("text") or ""
                region_refs.append(ref)
                refs.append(ref)
            labels = _attach_span_refs(
                (labeling.get("by_region") or {}).get(region_id) or [], region_refs,
            )
            unknown_labels = {
                str(label.get("label") or "") for label in labels
            } - allowed_labels
            if unknown_labels:
                raise ValueError(
                    f"선택 템플릿에 없는 구분값: {sorted(unknown_labels)!r}"
                )
            region["template_labels"] = labels
            if labels:
                labelled_regions += 1
            selection = select_region_text(region)
            if selection["needs_review"]:
                needs_review_regions += 1
            region["text_evidence"] = {
                "parser_primary_text": "\n".join(
                    str(line.get("text") or "") for line in region.get("lines") or []
                ).strip(),
                "parser_primary_line_refs": region_refs,
                "vlm_region_reading": region.get("element_vlm_reading"),
                "vlm_region_confidence": region.get("element_vlm_confidence"),
                "judge": copy.deepcopy(region.get("reading_adjudication")),
                "review_selection": selection,
            }
        for index, line in enumerate(page.get("unassigned_lines") or []):
            ref = f"p{page_no}/unassigned/L{index:03d}"
            line["line_ref"] = ref
            line["parser_text"] = line.get("text") or ""
            refs.append(ref)

    if len(refs) != len(set(refs)):
        raise ValueError("P1 line_ref가 중복됩니다")
    regions = [
        region
        for page in evidence.get("pages") or []
        for region in page.get("regions") or []
    ]
    evidence["summary"] = {
        "page_count": len(evidence.get("pages") or []),
        "region_count": len(regions),
        "line_count": len(refs),
        "labelled_region_count": labelled_regions,
        "unlabelled_region_count": len(regions) - labelled_regions,
        "needs_review_region_count": needs_review_regions,
        "recovery_candidate_count": sum(
            len(page.get("recovery_candidates") or []) for page in evidence.get("pages") or []
        ),
    }
    return evidence


def _label_index(pages: list[dict[str, Any]], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    references: dict[tuple[str | None, str], list[dict[str, Any]]] = defaultdict(list)
    for page in pages:
        for region in page["regions"]:
            for label in region.get("labels") or []:
                references[(label.get("label_id"), label["label"])].append({
                    "page_no": page["page_no"],
                    "region_id": region["region_id"],
                    "spans": copy.deepcopy(label.get("spans") or []),
                })
    item_by_label = {item["gubun"]: item for item in items}
    return [
        {
            "label_id": label_id,
            "label": label,
            "template_item": copy.deepcopy(item_by_label.get(label)),
            "references": values,
        }
        for (label_id, label), values in references.items()
    ]


def build_review_input(evidence: dict[str, Any]) -> dict[str, Any]:
    """P1의 모든 줄을 영역 소유권 그대로 유지하며 단일 선택 텍스트로 투영한다."""
    pages_out: list[dict[str, Any]] = []
    input_refs: list[str] = []
    output_refs: list[str] = []
    empty_regions: list[dict[str, Any]] = []
    recovery: list[dict[str, Any]] = []
    review_regions: list[dict[str, Any]] = []

    for page in evidence.get("pages") or []:
        page_no = page.get("page_no")
        regions_out: list[dict[str, Any]] = []
        for region in page.get("regions") or []:
            lines = region.get("lines") or []
            refs = [str(line.get("line_ref") or "") for line in lines]
            if any(not ref for ref in refs):
                raise ValueError(f"line_ref가 없는 영역 줄: {region.get('region_id')!r}")
            input_refs.extend(refs)
            if not lines:
                empty_regions.append({
                    "page_no": page_no,
                    "region_id": region.get("region_id"),
                    "bbox": region.get("bbox"),
                    "layout_label": region.get("label"),
                })
                continue
            selection = copy.deepcopy(
                ((region.get("text_evidence") or {}).get("review_selection") or {})
            )
            region_out = {
                "region_id": region.get("region_id"),
                "scope": {"page_no": page_no, "card_no": region.get("card_no")},
                "bbox": region.get("bbox"),
                "layout": {"label": region.get("label"), "score": region.get("layout_score")},
                **selection,
                "line_refs": refs,
                "labels": copy.deepcopy(region.get("template_labels") or []),
            }
            if region.get("table") is not None:
                region_out["table"] = copy.deepcopy(region["table"])
            regions_out.append(region_out)
            output_refs.extend(refs)
            if selection.get("needs_review"):
                review_regions.append({
                    "page_no": page_no,
                    "region_id": region.get("region_id"),
                    "selection_status": selection.get("selection_status"),
                    "reason": selection.get("reason"),
                })

        unassigned_out: list[dict[str, Any]] = []
        for line in page.get("unassigned_lines") or []:
            ref = str(line.get("line_ref") or "")
            if not ref:
                raise ValueError(f"line_ref가 없는 미배정 줄: page={page_no!r}")
            input_refs.append(ref)
            output_refs.append(ref)
            unassigned_out.append({
                "line_ref": ref,
                "selected_text": line.get("text") or "",
                "selected_source": f"{line.get('source') or 'parser'}_parser",
                "selection_status": "unassigned_parser_text",
                "needs_review": True,
            })
        for candidate in page.get("recovery_candidates") or []:
            recovery.append({"page_no": page_no, **copy.deepcopy(candidate)})
        pages_out.append({
            "page_no": page_no,
            "parse_status": page.get("parse_status"),
            "regions": regions_out,
            "unassigned_text": unassigned_out,
        })

    if len(input_refs) != len(set(input_refs)):
        raise ValueError("P1 line_ref가 중복됩니다")
    if len(output_refs) != len(set(output_refs)):
        raise ValueError("P3 텍스트 소유권이 중복됩니다")
    if set(input_refs) != set(output_refs):
        raise ValueError(
            "P3 텍스트 분할 오류: "
            f"누락={sorted(set(input_refs) - set(output_refs))[:3]!r}, "
            f"초과={sorted(set(output_refs) - set(input_refs))[:3]!r}"
        )

    template = evidence.get("template") or {}
    label_index = _label_index(pages_out, template.get("items") or [])
    region_count = sum(len(page["regions"]) for page in pages_out)
    return {
        "contract": {
            "version": REVIEW_INPUT_VERSION,
            "source_evidence_version": (evidence.get("contract") or {}).get("version"),
            "review_unit": "region",
            "text_policy": "one selected_text per region; P1 keeps all parser/VLM evidence",
            "label_policy": "labels point to parser line spans and never duplicate text ownership",
        },
        "document": {
            "doc_id": evidence.get("doc_id"),
            "source_file": evidence.get("source_file"),
            "file_type": evidence.get("file_type"),
            "classification": copy.deepcopy(evidence.get("classification") or {}),
            "template": {
                key: copy.deepcopy(template.get(key))
                for key in ("template_id", "status", "source", "confidence", "reason", "catalog_version")
            },
        },
        "pages": pages_out,
        "label_index": label_index,
        "unverified_recovery_candidates": recovery,
        "diagnostics": {
            "empty_regions": empty_regions,
            "needs_review_regions": review_regions,
            "non_ok_pages": [
                {"page_no": page["page_no"], "parse_status": page["parse_status"]}
                for page in pages_out if page["parse_status"] != "ok"
            ],
        },
        "summary": {
            "region_count": region_count,
            "empty_region_count": len(empty_regions),
            "selected_text_region_count": region_count,
            "parser_primary_line_total": len(input_refs),
            "unassigned_line_count": sum(len(page["unassigned_text"]) for page in pages_out),
            "label_count": len(label_index),
            "labelled_region_count": sum(
                1 for page in pages_out for region in page["regions"] if region["labels"]
            ),
            "needs_review_region_count": len(review_regions),
            "recovery_candidate_count": len(recovery),
        },
    }
