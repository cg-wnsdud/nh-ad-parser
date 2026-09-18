from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "experiments" / "parser-v2" / "adapters.py"
SPEC = importlib.util.spec_from_file_location("parser_v2_adapters", MODULE_PATH)
assert SPEC and SPEC.loader
adapters = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapters)


def test_block_content_is_canonical_and_ocr_is_only_evidence():
    page = adapters.build_page_evidence(
        [{"bbox": [0, 0, 200, 100], "label": "text", "order": 1,
          "content": "PaddleX가 정리한 본문"}],
        [{"bbox": [10, 10, 180, 30], "text": "OCR이 다르게 읽은 본문", "score": 0.9}],
        page_no=1,
        canvas=[200, 100],
    )
    region = page["regions"][0]
    assert region["text"] == "PaddleX가 정리한 본문"
    assert region["text_source"] == "paddlex_block_content"
    assert region["text_selection_status"] == "conflict_pending_vlm"
    assert region["ocr_evidence"][0]["text"] == "OCR이 다르게 읽은 본문"
    assert region["content_gap_candidates"] == region["ocr_evidence"]


def test_empty_block_content_falls_back_to_owned_ocr_lines():
    page = adapters.build_page_evidence(
        [{"bbox": [0, 0, 200, 100], "label": "table", "order": 1, "content": ""}],
        [
            {"bbox": [10, 10, 180, 30], "text": "첫 줄", "score": 0.9},
            {"bbox": [10, 40, 180, 60], "text": "둘째 줄", "score": 0.9},
        ],
        page_no=2,
        canvas=[200, 100],
    )
    region = page["regions"][0]
    assert region["text"] == "첫 줄\n둘째 줄"
    assert region["text_source"] == "ocr_fallback"
    assert "card_no" not in region
    assert region["product_id"] is None


def test_digital_pdf_text_is_preferred_but_paddlex_candidate_is_retained():
    page = adapters.build_page_evidence(
        [{"bbox": [0, 0, 200, 100], "label": "text", "order": 1,
          "content": "OCR 2.8%"}],
        [{"bbox": [10, 10, 180, 30], "text": "OCR 2.8%", "score": 0.8}],
        page_no=1,
        canvas=[200, 100],
        digital_lines=[{"bbox": [10, 10, 180, 30], "text": "정확한 금리 2.8%"}],
    )
    region = page["regions"][0]
    assert region["text"] == "정확한 금리 2.8%"
    assert region["text_source"] == "digital_ocr_lines"
    assert region["text_candidates"]["paddlex_block_content"] == "OCR 2.8%"


def test_nested_regions_give_ocr_line_to_smaller_region_once():
    page = adapters.build_page_evidence(
        [
            {"bbox": [0, 0, 200, 200], "label": "image", "order": 1, "content": "부모"},
            {"bbox": [20, 20, 100, 60], "label": "text", "order": 2, "content": "자식"},
        ],
        [{"bbox": [25, 25, 90, 50], "text": "자식", "score": 0.9}],
        page_no=1,
        canvas=[200, 200],
    )
    parent, child = page["regions"]
    assert parent["ocr_evidence"] == []
    assert len(child["ocr_evidence"]) == 1
    assert child["parent_id"] == parent["region_id"]
    assert parent["child_ids"] == [child["region_id"]]


def test_unassigned_line_is_preserved_without_nearest_absorption():
    page = adapters.build_page_evidence(
        [{"bbox": [0, 0, 50, 50], "label": "text", "order": 1, "content": "본문"}],
        [{"bbox": [100, 100, 150, 120], "text": "밖의 줄", "score": 0.8}],
        page_no=1,
        canvas=[200, 200],
    )
    assert page["regions"][0]["ocr_evidence"] == []
    assert page["unassigned_lines"][0]["text"] == "밖의 줄"
