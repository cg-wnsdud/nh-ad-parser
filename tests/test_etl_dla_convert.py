# -*- coding: utf-8 -*-
"""농협 ETL with LLM Default JSON → IR 변환 테스트.

픽스처는 **가이드 원문의 실제 예시**를 그대로 옮긴 것이다 (58쪽 이미지 PDF 를 판독).
실물 응답을 받기 전이라 이 값들이 유일한 근거다 — 임의로 고치면 근거가 사라진다.
출처 쪽수를 각 픽스처에 남긴다.
"""

import pytest

from nh_parser.layout.regions import _LABEL_TO_ROLE
from nh_parser.ocr.etlwithllm import (
    DLA_TYPES,
    page_result_from_dla,
    page_results_from_dla,
    rescale,
)


# 원문 p.48~49 — callback 결과(res_type default)의 doc_result 본문.
PAGE_TEXT = {
    "pageId": 1,
    "width": 1275,
    "height": 1650,
    "paragraphs": [
        {
            "paragraphId": 0,
            "type": "Text",
            "bbox": [[184, 206], [568, 206], [568, 232], [184, 232]],
            "contents": "This is a sample test document.",
            "confidence": 0.998,
            "lines": [
                {
                    "bbox": [[187, 206], [567, 206], [567, 235], [187, 235]],
                    "contents": "This is a sample test document.",
                }
            ],
            "bullet_id": "",
            "parentId": [],
        },
        {
            "paragraphId": 1,
            "type": "Title",
            "bbox": [[183, 267], [445, 267], [445, 291], [183, 291]],
            "contents": "Section 1: Introduction",
            "confidence": 0.852,
            "lines": [
                {
                    "bbox": [[187, 267], [444, 267], [444, 294], [187, 294]],
                    "contents": "Section 1: Introduction",
                }
            ],
            "bullet_id": "",
            "parentId": [],
        },
    ],
}

# 원문 p.56~57 — 표 1개(3x3)와 셀 1개가 있는 페이지.
PAGE_TABLE = {
    "pageId": 1,
    "width": 1275,
    "height": 1650,
    "paragraphs": [
        {
            "paragraphId": 10,
            "type": "Table",
            "bbox": [[149, 776], [1126, 776], [1126, 909], [149, 909]],
            "contents": "<table>\n <tr>\n  <td>나는 표 테이터</td>\n </tr>\n</table>",
            "confidence": 0.999,
            "lines": [],
            "bullet_id": "",
            "parentId": [],
            "error_type": [],
            "rows": 3,
            "cols": 3,
            "childId": [],
            "cells": [
                {
                    "bbox": [[153, 776], [474, 776], [474, 822], [153, 822]],
                    "contents": "나는 표 테이터",
                    "cellId": "C0",
                    "confidence": -1,
                    "cell_info": [0, 0, 0, 0],
                }
            ],
        }
    ],
    "list_hierarchy": "",
    "warning_messages": [{"code": "USE_OCR_PAGE", "detail": {}}],
    "n_cols": 1,
}


def test_페이지_크기를_그대로_담는다():
    result = page_result_from_dla(PAGE_TEXT)
    assert (result.source_width, result.source_height) == (1275, 1650)


def test_4점_폴리곤_bbox를_사각형으로_바꾼다():
    """원문 bbox 는 [[x,y] x 4] 다 — [x0,y0,x1,y1] 로 접혀야 한다."""
    result = page_result_from_dla(PAGE_TEXT)
    assert result.blocks[0].bbox == [184, 206, 568, 232]
    assert result.ocr_lines[0].bbox == [187, 206, 567, 235]


def test_paragraph는_블록으로_lines는_줄로_나온다():
    result = page_result_from_dla(PAGE_TEXT)
    assert [b.label for b in result.blocks] == ["text", "title"]
    assert [b.score for b in result.blocks] == [0.998, 0.852]
    assert [line.text for line in result.ocr_lines] == [
        "This is a sample test document.",
        "Section 1: Introduction",
    ]
    assert all(line.source == "ocr" for line in result.ocr_lines)


def test_줄_신뢰도는_None이다():
    """ETL 은 줄 단위 인식 신뢰도를 주지 않는다 (전환 계획 G1).

    paragraph 의 DLA confidence 를 줄에 몰래 넣지 않는다 — 다른 물리량이다.
    """
    result = page_result_from_dla(PAGE_TEXT)
    assert all(line.confidence is None for line in result.ocr_lines)


def test_표는_cell_info로_격자를_만든다():
    result = page_result_from_dla(PAGE_TABLE)
    block = result.blocks[0]
    assert block.label == "table"
    assert block.table is not None
    assert (block.table["n_rows"], block.table["n_cols"]) == (3, 3)
    assert block.table["note"] is None
    assert block.table["html"].startswith("<table>")

    cell = block.table["cells"][0]
    assert (cell["row"], cell["col"]) == (0, 0)
    assert (cell["row_span"], cell["col_span"]) == (1, 1)
    assert cell["bbox"] == [153, 776, 474, 822]
    assert cell["text"] == "나는 표 테이터"


def test_표에는_정본_줄이_없다():
    """G11 회귀 가드. 표에 줄이 안 오는 것은 버그가 아니라 규격이다.

    우리 설계는 표의 정본을 Region.lines 로 두는데 ETL 은 표에 lines 를 주지 않는다.
    이 테스트가 깨진다면(줄이 생겼다면) 정본 정책을 다시 검토해야 한다.
    """
    result = page_result_from_dla(PAGE_TABLE)
    assert result.ocr_lines == []


def test_병합셀_span은_양끝_포함으로_해석한다():
    """`cell_info` = [row_start, row_end, col_start, col_end], 양끝 포함.

    원문에 병합셀 예시가 없어 3x3 표 첫 셀 [0,0,0,0](span 1)에서 유추한 해석이다.
    실물 응답을 받으면 가장 먼저 확인할 것.
    """
    page = {
        "pageId": 1, "width": 1000, "height": 1000,
        "paragraphs": [{
            "paragraphId": 0, "type": "Table",
            "bbox": [[0, 0], [900, 0], [900, 400], [0, 400]],
            "contents": "<table></table>", "rows": 4, "cols": 3,
            "cells": [{
                "bbox": [[0, 0], [300, 0], [300, 400], [0, 400]],
                "contents": "일반", "cellId": "C0",
                "cell_info": [0, 3, 0, 0],
            }],
        }],
    }
    cell = page_result_from_dla(page).blocks[0].table["cells"][0]
    assert (cell["row"], cell["row_span"]) == (0, 4)
    assert (cell["col"], cell["col_span"]) == (0, 1)


def test_cell_info가_없으면_조용히_버리지_않고_note를_남긴다():
    page = {
        "pageId": 1, "width": 1000, "height": 1000,
        "paragraphs": [{
            "paragraphId": 0, "type": "Table",
            "bbox": [[0, 0], [900, 0], [900, 400], [0, 400]],
            "contents": "<table></table>", "rows": 2, "cols": 2,
            "cells": [
                {"bbox": [[0, 0], [300, 0], [300, 200], [0, 200]],
                 "contents": "가", "cell_info": [0, 0, 0, 0]},
                {"bbox": [[300, 0], [600, 0], [600, 200], [300, 200]],
                 "contents": "나"},
            ],
        }],
    }
    table = page_result_from_dla(page).blocks[0].table
    assert len(table["cells"]) == 1
    assert table["note"] is not None and "cell_info" in table["note"]


def test_bbox가_없는_paragraph는_건너뛴다():
    page = {
        "pageId": 1, "width": 100, "height": 100,
        "paragraphs": [
            {"paragraphId": 0, "type": "Text", "contents": "좌표 없음"},
            {"paragraphId": 1, "type": "Text",
             "bbox": [[10, 10], [50, 10], [50, 30], [10, 30]], "contents": "정상"},
        ],
    }
    result = page_result_from_dla(page)
    assert [b.content for b in result.blocks] == ["정상"]


def test_문서_전체는_pageId_순서로_낸다():
    doc = {
        "pdfName": "test.pdf", "pageLen": 2,
        "pages": [
            {"pageId": 2, "width": 10, "height": 20, "paragraphs": []},
            {"pageId": 1, "width": 30, "height": 40, "paragraphs": []},
        ],
    }
    results = page_results_from_dla(doc)
    assert [r.source_width for r in results] == [30, 10]


def test_모든_DLA_타입에_역할이_정해진다():
    """타입 9종이 전부 `_LABEL_TO_ROLE` 에 있어야 한다 (기본값에 조용히 떨어지지 않게)."""
    미매핑 = [t for t in DLA_TYPES if t.lower() not in _LABEL_TO_ROLE]
    assert 미매핑 == []


def test_rescale은_축별_배율로_좌표를_옮긴다():
    result = page_result_from_dla(PAGE_TABLE)          # 1275 x 1650
    scaled = rescale(result, 2550, 3300)               # 정확히 2배
    assert (scaled.source_width, scaled.source_height) == (2550, 3300)
    assert scaled.blocks[0].bbox == [298, 1552, 2252, 1818]
    assert scaled.blocks[0].table["cells"][0]["bbox"] == [306, 1552, 948, 1644]


def test_rescale은_원본을_바꾸지_않는다():
    result = page_result_from_dla(PAGE_TABLE)
    before = list(result.blocks[0].bbox)
    rescale(result, 2550, 3300)
    assert result.blocks[0].bbox == before


@pytest.mark.parametrize("width,height", [(0, 1650), (1275, 0)])
def test_크기를_모르면_rescale하지_않는다(width, height):
    """배율을 모를 때 1 로 가정하거나 0 으로 나누지 않는다."""
    page = {"pageId": 1, "width": width, "height": height,
            "paragraphs": [{"paragraphId": 0, "type": "Text",
                            "bbox": [[1, 1], [5, 1], [5, 3], [1, 3]], "contents": "x"}]}
    result = page_result_from_dla(page)
    assert rescale(result, 100, 200) is result
