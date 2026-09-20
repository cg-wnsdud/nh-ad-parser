"""표를 찾고 행·열로 복원하는 경로를 검증한다.

좌표와 문구는 끝까지 OCR 줄에서 나와야 한다. VLM은 배치만 한다.
"""
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PARSER_V2 = ROOT / "experiments" / "parser-v2"
OCR_LAB = ROOT / "experiments" / "ocr-lab"
sys.path.insert(0, str(PARSER_V2))
sys.path.insert(0, str(OCR_LAB))
sys.path.insert(0, str(ROOT / "src"))


def _module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PARSER_V2 / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tables = _module("parser_v2_tables", "tables.py")
full_pipeline = _module("parser_v2_full_pipeline_tables", "full_pipeline.py")


def _line(ref, x0, y0, x1, y1, text):
    return {"line_ref": ref, "bbox": [x0, y0, x1, y1], "text": text}


def _benefit_table():
    """`14. 대출성상품`의 부가서비스 표를 실측 좌표로 재현한다.

    PaddleX가 이 표를 검출하지 못해 13줄이 미배정으로 남았고, 복구 후보
    생성기가 낱개 영역 13개로 흩뿌렸다.
    """
    return [
        _line("L01", 633, 1842, 684, 1862, "담보명"),
        _line("L02", 796, 1842, 870, 1862, "보장금액"),
        _line("L03", 984, 1842, 1035, 1862, "담보명"),
        _line("L04", 1130, 1842, 1204, 1862, "보장금액"),
        _line("L05", 1318, 1842, 1369, 1862, "담보명"),
        _line("L06", 601, 1873, 717, 1892, "24시간 사고접수"),
        _line("L07", 793, 1875, 875, 1893, "10,000,000"),
        _line("L08", 938, 1873, 1198, 1893, "일상생활배상책임 손해"),
        _line("L09", 560, 1905, 875, 1924, "국내치료비(재해사망)보장"),
        _line("L10", 937, 1905, 1198, 1924, "화재벌금비용 손해"),
        _line("L11", 1305, 1905, 1381, 1923, "휴스치료비"),
        _line("L12", 1475, 1906, 1534, 1924, "100,000"),
    ]


def test_grid_detection_finds_a_table_paddlex_never_detected():
    lines = _benefit_table()

    grids = tables.find_grids(lines)

    assert len(grids) == 1
    assert len(grids[0]) == len(lines)
    assert tables.looks_like_grid(lines)


def test_paragraph_lines_are_not_mistaken_for_a_table():
    """한 열로 흐르는 문단은 표가 아니다."""
    lines = [
        _line("L01", 100, 100, 900, 125, "※ 대출대상 : 만 19세 이상 실명의 개인"),
        _line("L02", 100, 130, 900, 155, "※ 재직기간 3개월 이상인 급여소득자"),
        _line("L03", 100, 160, 900, 185, "※ 연소득 2천만원 이상"),
        _line("L04", 100, 190, 900, 215, "※ 신용평점 일정 수준 이상"),
        _line("L05", 100, 220, 900, 245, "※ 기타 은행 내규에 따름"),
    ]

    assert tables.find_grids(lines) == []
    assert not tables.looks_like_grid(lines)


def test_table_lines_become_one_candidate_instead_of_thirteen_fragments():
    """복구 후보 생성 **앞에서** 표를 떼어내야 낱개로 흩어지지 않는다."""
    page = {
        "page_no": 1,
        "canvas": [1654, 2339],
        "regions": [],
        "unassigned_lines": [
            *_benefit_table(),
            _line("L90", 200, 2100, 900, 2125, "※ 자세한 내용은 영업점에 문의"),
        ],
    }

    prepared = full_pipeline._prepare_page(page)

    candidates = prepared["recovery_candidates"]
    kinds = [item.get("kind") for item in candidates]
    assert kinds.count("table") == 1
    table = next(item for item in candidates if item.get("kind") == "table")
    assert len(table["line_refs"]) == 12
    assert table["bbox"] == [560, 1842, 1534, 1924]
    assert table["bbox_source"] == "ocr_pdf_lines"
    # 표가 아닌 줄은 평소대로 복구 후보로 남는다. 줄은 하나도 잃지 않는다.
    # `_prepare_page`가 line_ref를 `p1/unassigned/Lnnn`으로 다시 매기므로 그 뒤의
    # 값으로 대조한다.
    every = {ref for item in candidates for ref in item["line_refs"]}
    assert every == {
        str(line["line_ref"]) for line in prepared["raw_unassigned_lines"]
    }
    assert len(every) == 13


def test_build_grid_keeps_ocr_text_and_reports_unplaced_lines():
    by_ref = {line["line_ref"]: line for line in _benefit_table()[:6]}
    result = {
        "analysis": "2행 3열",
        "rows": 2, "cols": 3,
        "cells": [
            {"line_ref": "L01", "row": 0, "col": 0, "is_header": True},
            {"line_ref": "L02", "row": 0, "col": 1, "is_header": True},
            {"line_ref": "L03", "row": 0, "col": 2, "is_header": True},
            {"line_ref": "L06", "row": 1, "col": 0, "is_header": False},
            # L04, L05 는 배치되지 않았다.
        ],
        "confidence": 0.9,
    }

    grid = tables.build_grid(result, by_ref)

    assert grid["grid"] == {"rows": 2, "cols": 3}
    assert [cell["text"] for cell in grid["cells"]] == [
        "담보명", "보장금액", "담보명", "24시간 사고접수",
    ]
    assert grid["cells"][0]["bbox"] == [633, 1842, 684, 1862]
    assert sorted(grid["unplaced_line_refs"]) == ["L04", "L05"]
    assert "| 담보명 | 보장금액 | 담보명 |" in grid["text_grid"]


def test_build_grid_drops_duplicate_placement_of_the_same_line():
    by_ref = {line["line_ref"]: line for line in _benefit_table()[:2]}
    result = {
        "analysis": "", "rows": 1, "cols": 2, "confidence": 1.0,
        "cells": [
            {"line_ref": "L01", "row": 0, "col": 0, "is_header": True},
            {"line_ref": "L01", "row": 0, "col": 1, "is_header": True},
            {"line_ref": "L02", "row": 0, "col": 1, "is_header": True},
        ],
    }

    grid = tables.build_grid(result, by_ref)

    refs = [ref for cell in grid["cells"] for ref in cell["line_refs"]]
    assert refs == ["L01", "L02"]
    assert len(refs) == len(set(refs))


def test_build_grid_returns_none_when_the_model_says_not_a_table():
    by_ref = {line["line_ref"]: line for line in _benefit_table()[:6]}

    assert tables.build_grid(
        {"analysis": "표 아님", "rows": 0, "cols": 0, "cells": [], "confidence": 0.0},
        by_ref,
    ) is None


def test_vlm_table_area_promotes_scattered_recovery_regions_without_new_bbox():
    """VLM은 위치만 알려주고 좌표는 합쳐진 OCR 줄에서 나와야 한다."""
    lines = _benefit_table()
    page = {
        "page_no": 1,
        "canvas": [1654, 2339],
        "table_areas": [{
            "approx_bbox_pct": [33.0, 78.0, 93.0, 82.5],
            "note": "담보명/보장금액 표", "confidence": 0.9,
        }],
        "regions": [
            {"region_id": f"p1_x{index:03d}", "origin": "recovery", "kind": "text",
             "product_id": "product_1", "bbox": line["bbox"], "lines": [line],
             "text": line["text"]}
            for index, line in enumerate(lines, start=1)
        ],
    }

    promoted = full_pipeline._promote_vlm_table_areas(page)

    assert len(promoted) == 1
    merged = promoted[0]
    assert merged["region_id"] == "p1_t001"
    assert merged["kind"] == "table"
    assert merged["bbox"] == [560, 1842, 1534, 1924]
    assert merged["bbox_source"] == "ocr_pdf_lines"
    assert len(merged["lines"]) == 12
    # 합쳐진 원본 Region은 사라지고 줄은 새 Region 하나에만 남는다.
    assert len(page["regions"]) == 1
    assert merged["merged_from"][0] == "p1_x001"


def test_vlm_table_area_is_ignored_when_too_few_lines_sit_inside():
    page = {
        "page_no": 1,
        "canvas": [1000, 1000],
        "table_areas": [{"approx_bbox_pct": [0, 0, 100, 100], "note": "", "confidence": 0.9}],
        "regions": [
            {"region_id": "p1_x001", "origin": "recovery", "kind": "text",
             "bbox": [10, 10, 100, 30], "lines": [_line("L01", 10, 10, 100, 30, "가")]},
            {"region_id": "p1_x002", "origin": "recovery", "kind": "text",
             "bbox": [10, 40, 100, 60], "lines": [_line("L02", 10, 40, 100, 60, "나")]},
        ],
    }

    assert full_pipeline._promote_vlm_table_areas(page) == []
    assert len(page["regions"]) == 2


def _card_table_regions():
    """`2. 카드상품` 의 2행 3열 표. 5칸이 Region 5개로 흩어져 있다.

    복구 3칸(`구분`·`적립율`·`2%`)과 PaddleX 2칸이 섞여 있어, 복구 Region 만
    보면 칸 수가 모자라 통째로 버려졌다.
    """
    cells = [
        ("p1_x001", "recovery", [477, 2315, 560, 2369], "구분"),
        ("p1_r011", "paddlex", [1175, 2315, 1521, 2372], "GS리테일 점내 가맹점"),
        ("p1_x003", "recovery", [467, 2410, 573, 2458], "적립율"),
        ("p1_x002", "recovery", [954, 2393, 1082, 2478], "2%"),
        ("p1_r012", "paddlex", [1471, 2384, 1901, 2485], "GS25, GS THE FRESH"),
    ]
    return [
        {"region_id": rid, "origin": origin, "kind": "text", "product_id": "product_1",
         "bbox": box, "text": text,
         "lines": [{"line_ref": f"p1/{rid}/L000", "bbox": box, "text": text}]}
        for rid, origin, box, text in cells
    ]


def test_vlm_table_area_merges_paddlex_cells_too():
    page = {
        "page_no": 1, "canvas": [4032, 4032],
        "table_areas": [{"approx_bbox_pct": [10, 55, 40, 65], "kind": "table",
                         "note": "GS리테일 적립율 표", "confidence": 1.0}],
        "regions": _card_table_regions(),
    }

    promoted = full_pipeline._promote_vlm_table_areas(page)

    assert len(promoted) == 1
    merged = promoted[0]
    assert merged["region_id"] == "p1_t001"
    assert merged["kind"] == "table"
    assert merged["bbox"] == [467, 2315, 1901, 2485]
    assert merged["bbox_source"] == "ocr_pdf_lines"
    assert len(merged["lines"]) == 5
    assert sorted(merged["merged_from"]) == [
        "p1_r011", "p1_r012", "p1_x001", "p1_x002", "p1_x003",
    ]
    assert len(page["regions"]) == 1


def test_field_list_areas_are_never_merged():
    """항목명–값 나열은 행마다 구분값이 달라 합치면 라벨이 하나만 남는다."""
    page = {
        "page_no": 1, "canvas": [4032, 4032],
        "table_areas": [{"approx_bbox_pct": [10, 55, 40, 65], "kind": "field_list",
                         "note": "대출대상~필요서류", "confidence": 1.0}],
        "regions": _card_table_regions(),
    }

    assert full_pipeline._promote_vlm_table_areas(page) == []
    assert len(page["regions"]) == 5


def test_areas_spanning_two_products_are_not_merged():
    regions = _card_table_regions()
    regions[1]["product_id"] = "product_2"
    page = {
        "page_no": 1, "canvas": [4032, 4032],
        "table_areas": [{"approx_bbox_pct": [10, 55, 40, 65], "kind": "table",
                         "note": "", "confidence": 1.0}],
        "regions": regions,
    }

    assert full_pipeline._promote_vlm_table_areas(page) == []


def test_pixel_valued_areas_are_accepted():
    """백분율을 요구했지만 모델이 픽셀을 주기도 한다 — 실측 11건 중 4건."""
    canvas = [1654, 2339]
    pct = tables.area_to_bbox(
        {"approx_bbox_pct": [30, 60, 95, 80]}, canvas)
    pixels = tables.area_to_bbox(
        {"approx_bbox_pct": [550, 1400, 1000, 1800]}, canvas)

    assert pct == [496, 1403, 1571, 1871]
    assert pixels == [550, 1400, 1000, 1800]
    # 0~100 으로 잘라내면 높이가 0이 돼 통째로 버려졌다.
    assert pixels is not None


def test_growing_does_not_reach_across_to_another_column_block():
    """멀리 떨어진 다른 단은 세로로 겹쳐도 끌어오지 않는다."""
    regions = _card_table_regions()
    far = {
        "region_id": "p1_r024", "origin": "paddlex", "kind": "text",
        "product_id": "product_1", "bbox": [2411, 2376, 3660, 2565],
        "text": "생활영역 추가적립",
        "lines": [{"line_ref": "p1/p1_r024/L000",
                   "bbox": [2411, 2376, 3660, 2565], "text": "생활영역 추가적립"}],
    }
    page = {
        "page_no": 1, "canvas": [4032, 4032],
        "table_areas": [{"approx_bbox_pct": [10, 55, 40, 65], "kind": "table",
                         "note": "", "confidence": 1.0}],
        "regions": [*regions, far],
    }

    promoted = full_pipeline._promote_vlm_table_areas(page)

    assert "p1_r024" not in promoted[0]["merged_from"]
    assert any(r["region_id"] == "p1_r024" for r in page["regions"])
