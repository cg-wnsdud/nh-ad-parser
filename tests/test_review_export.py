import copy

import pytest

from nh_parser.review.export import build_evidence, build_review_input


def _doc(reading: dict | None = None) -> dict:
    return {
        "doc_id": "sample",
        "source_file": "sample.png",
        "file_type": "image",
        "product_group": "예금성",
        "ad_type": "상세페이지",
        "product_name_shown": "노출",
        "category_source": "filename_and_vlm",
        "classification_confidence": 0.95,
        "notes": [],
        "pages": [{
            "page_no": 1,
            "canvas_w": 100,
            "canvas_h": 100,
            "dpi": None,
            "parse_route": "ocr",
            "parse_status": "ok",
            "triage": None,
            "notes": [],
            "recovery_candidates": [],
            "unassigned_lines": [],
            "regions": [{
                "region_id": "p1_r000",
                "bbox": [0, 0, 100, 50],
                "label": "text",
                "layout_score": 0.9,
                "role": "본문",
                "card_no": None,
                "table": None,
                "element_vlm_reading": reading.get("reader_text") if reading else None,
                "element_vlm_confidence": reading.get("reader_confidence") if reading else None,
                "reading_adjudication": reading,
                "lines": [{
                    "text": "기본금리 연 3.1%",
                    "bbox": [0, 0, 80, 20],
                    "confidence": 0.99,
                    "source": "ocr",
                    "style": None,
                }],
            }],
        }],
    }


RESOLUTION = {
    "template_id": "예금성상품-적립식",
    "status": "confirmed",
    "source": "rules",
    "confidence": 1.0,
    "reason": "적금",
    "candidates": ["예금성상품-적립식"],
}
LABELING = {
    "status": "complete",
    "notes": [],
    "by_region": {"p1_r000": [{
        "label_id": "예금성상품-적립식:금리",
        "label": "금리",
        "spans": [{
            "line_from": 0, "line_to": 0,
            "sources": ["semantic_vlm"], "confidence": 0.9,
        }],
    }]},
}


def test_p1은_두_판독을_보존하고_p3는_judge가_고른_vlm을_하나만_전달한다() -> None:
    reading = {
        "status": "uncertain",
        "reader_text": "기본금리 연 3.7%",
        "reader_confidence": 0.96,
        "judge_decision": "candidate_b",
        "proposed_text": "기본금리 연 3.7%",
        "proposed_source": "element_vlm",
        "confidence": 0.94,
        "reason": "이미지의 숫자는 3.7",
    }
    evidence = build_evidence(_doc(reading), RESOLUTION, LABELING)
    review = build_review_input(evidence)
    p1_region = evidence["pages"][0]["regions"][0]
    p3_region = review["pages"][0]["regions"][0]

    assert p1_region["lines"][0]["text"] == "기본금리 연 3.1%"
    assert p1_region["text_evidence"]["vlm_region_reading"] == "기본금리 연 3.7%"
    assert p3_region["selected_text"] == "기본금리 연 3.7%"
    assert p3_region["selected_source"] == "vlm_judge"
    assert p3_region["needs_review"] is True
    assert p3_region["labels"][0]["spans"][0]["line_refs"] == ["p1/p1_r000/L000"]


def test_vlm과_일치하면_parser를_선택하고_검수불필요로_표시한다() -> None:
    reading = {
        "status": "agreed",
        "reader_text": "기본금리 연 3.1%",
        "reader_confidence": 0.98,
        "proposed_text": "기본금리 연 3.1%",
        "proposed_source": "canonical",
        "confidence": 0.98,
        "reason": "일치",
    }
    review = build_review_input(build_evidence(_doc(reading), RESOLUTION, LABELING))
    region = review["pages"][0]["regions"][0]

    assert region["selected_source"] == "ocr_parser"
    assert region["selection_status"] == "parser_and_vlm_agree"
    assert region["needs_review"] is False


def test_judge_실패면_parser를_선택하되_검수대상으로_남긴다() -> None:
    reading = {"status": "judge_failed", "error": "timeout"}
    review = build_review_input(build_evidence(_doc(reading), RESOLUTION, LABELING))
    region = review["pages"][0]["regions"][0]

    assert region["selected_text"] == "기본금리 연 3.1%"
    assert region["selection_status"] == "judge_unresolved_parser_fallback"
    assert region["needs_review"] is True


def test_p3가_p1_줄을_중복_소유하면_거부한다() -> None:
    evidence = build_evidence(_doc(), RESOLUTION, LABELING)
    duplicate = copy.deepcopy(evidence["pages"][0]["regions"][0]["lines"][0])
    evidence["pages"][0]["unassigned_lines"].append(duplicate)

    with pytest.raises(ValueError, match="중복"):
        build_review_input(evidence)


def test_선택_템플릿에_없는_구분값은_p1에서_거부한다() -> None:
    labeling = copy.deepcopy(LABELING)
    labeling["by_region"]["p1_r000"][0]["label"] = "대출한도"

    with pytest.raises(ValueError, match="없는 구분값"):
        build_evidence(_doc(), RESOLUTION, labeling)
