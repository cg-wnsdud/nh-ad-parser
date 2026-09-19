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
    assert region["lines"][0]["text"] == "OCR이 다르게 읽은 본문"
    assert [line["text"] for line in region["content_gap_candidates"]] == [
        "OCR이 다르게 읽은 본문"
    ]


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
    assert [line["source"] for line in region["lines"]] == ["digital"]


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
    assert len(child["lines"]) == 1
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


def test_block_order_is_scoped_to_each_tile():
    page = adapters.build_page_evidence(
        [
            {"bbox": [0, 10, 100, 20], "label": "text", "order": 1,
             "piece": 0, "content": "첫 타일 첫 영역"},
            {"bbox": [0, 30, 100, 40], "label": "text", "order": 2,
             "piece": 0, "content": "첫 타일 둘째 영역"},
            {"bbox": [0, 110, 100, 120], "label": "text", "order": 1,
             "piece": 1, "content": "둘째 타일 첫 영역"},
        ],
        [],
        page_no=1,
        canvas=[100, 150],
    )
    assert [r["text"] for r in page["regions"]] == [
        "첫 타일 첫 영역", "첫 타일 둘째 영역", "둘째 타일 첫 영역",
    ]


def test_tile_boundary_dedupe_keeps_higher_score_and_alternate():
    lines, merged = adapters.dedupe_ocr_lines([
        {"bbox": [10, 10, 200, 40], "text": "30.2%p 우대", "score": 0.98, "piece": 2},
        {"bbox": [12, 10, 200, 41], "text": "0.2%p 우대", "score": 0.95, "piece": 3},
        {"bbox": [10, 60, 200, 90], "text": "다른 줄", "score": 0.99, "piece": 3},
    ])
    assert merged == 1
    assert len(lines) == 2
    winner = next(line for line in lines if line["text"] == "30.2%p 우대")
    assert winner["tile_alternates"][0]["text"] == "0.2%p 우대"
