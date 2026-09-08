"""농협 ETL with LLM (AgileSoDA) Default JSON → 우리 IR 변환.

규격은 [docs/농협-ocr-연동-규격.md](../../../docs/농협-ocr-연동-규격.md),
전환 배경과 격차는 [docs/농협-ocr-전환-계획.md](../../../docs/농협-ocr-전환-계획.md).

**이 모듈은 변환만 한다.** HTTP 클라이언트(로그인·업로드·폴링·결과 조회)는 농협
접속 정보와 인증 필수 여부를 받은 뒤 따로 붙인다 — 지금 짜면 추측으로 채우는 부분이
생긴다(전환 계획 §6-1).

`paddlex.py` 와 같은 `PaddleXPageResult` 를 낸다. 이름이 PaddleX 지만 하류(영역 조립·
템플릿·검수)가 이미 그 모양을 소비하고 있어서, provider 를 바꿀 때 하류를 건드리지
않으려면 이 모양을 지키는 것이 맞다.

좌표는 **ETL 자체 렌더 기준**으로 그대로 낸다(`source_width`/`source_height` 에 그
크기를 담는다). 우리 캔버스(`pdf_render_dpi=200`)와 다르므로 VLM 크롭처럼 캔버스
좌표를 쓰는 단계 앞에서 `rescale` 을 거쳐야 한다 — 안 맞추면 재판독이 엉뚱한 자리를
본다(전환 계획 G5).
"""

from __future__ import annotations

from ..ir import Line
from .paddlex import LayoutBlock, PaddleXPageResult, _norm_bbox

# ETL `paragraphs[].type` 9종. 값은 그대로 소문자로 흘려보내고 역할 매핑은
# `layout/regions.py::_LABEL_TO_ROLE` 한 곳에서 한다 — 매핑을 두 곳에 두지 않는다.
# 원본 type 을 label 에 남기는 이유는 결과가 이상할 때 어느 클래스에서 왔는지
# 되짚을 수 있어야 하기 때문이다.
DLA_TYPES = (
    "Title", "List-item", "Equation", "Figure", "Table",
    "PageHF", "Index", "Text", "Unknown",
)

# 줄이 오지 않는 클래스. 규격상 `lines` 는 "table·image·equation 외 클래스" 에만 온다.
# 표의 정본 줄이 없어지는 문제(전환 계획 G11)가 여기서 비롯한다.
TYPES_WITHOUT_LINES = frozenset({"table", "figure", "equation"})


def _cell_grid(paragraph: dict, width: int, height: int) -> dict | None:
    """`cells` + `cell_info` → `RegionTable` 이 받는 격자 dict.

    PaddleX 경로(`_build_table_grid`)는 `pred_html` 을 파싱해 행·열을 **추론**하고
    셀 좌표 개수가 안 맞으면 좌표를 버렸다. ETL 은 `cell_info` 로 행·열 범위를 직접
    주므로 그 추론과 폴백이 필요 없다.

    `cell_info` 는 `[row_start, row_end, col_start, col_end]` 이고 **양끝 포함**으로
    해석한다 — 원문 p.56 예시의 3x3 표 첫 셀이 `[0, 0, 0, 0]` 이다(span 1). 실물
    응답에 병합셀이 나오면 이 해석을 먼저 확인할 것.
    """
    raw_cells = paragraph.get("cells")
    if not raw_cells:
        return None

    cells: list[dict] = []
    missing_info = 0
    for raw in raw_cells:
        if not isinstance(raw, dict):
            continue
        info = raw.get("cell_info")
        if not (isinstance(info, (list, tuple)) and len(info) == 4):
            missing_info += 1
            continue
        try:
            r0, r1, c0, c1 = (int(v) for v in info)
        except (TypeError, ValueError):
            missing_info += 1
            continue
        cells.append({
            "row": r0,
            "col": c0,
            "row_span": max(1, r1 - r0 + 1),
            "col_span": max(1, c1 - c0 + 1),
            "bbox": _norm_bbox(raw.get("bbox"), width, height),
            # 셀 텍스트. PaddleX 경로와 같은 자리이며 **정본이 아니다**
            # (`ir.py::TableCell` 주석). 표에 정본 줄이 없는 문제는 G11 로 따로 다룬다.
            "text": str(raw.get("contents") or ""),
        })

    if not cells:
        return None

    # 행·열 수는 ETL 이 직접 준다. 없으면 셀에서 되짚는다.
    n_rows = _positive_int(paragraph.get("rows")) or max(
        (c["row"] + c["row_span"] for c in cells), default=0
    )
    n_cols = _positive_int(paragraph.get("cols")) or max(
        (c["col"] + c["col_span"] for c in cells), default=0
    )

    note = None
    if missing_info:
        note = f"cell_info 가 없거나 형식이 달라 셀 {missing_info}개를 격자에 넣지 못했다"

    return {
        "n_rows": n_rows,
        "n_cols": n_cols,
        # 우리 격자 해석이 틀렸을 때 대조할 원본. `table_to_struct: "html"` 기준.
        "html": str(paragraph.get("contents") or ""),
        "cells": cells,
        "note": note,
    }


def _positive_int(value) -> int | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def page_result_from_dla(page: dict) -> PaddleXPageResult:
    """Default JSON 의 `pages[]` 한 장 → `PaddleXPageResult`.

    좌표는 손대지 않는다. 페이지가 선언한 `width`/`height` 를 그대로
    `source_width`/`source_height` 에 담아 호출측이 스케일을 결정하게 한다.
    """
    width = _positive_int(page.get("width")) or 0
    height = _positive_int(page.get("height")) or 0
    result = PaddleXPageResult(source_width=width, source_height=height)

    for paragraph in page.get("paragraphs") or []:
        if not isinstance(paragraph, dict):
            continue
        bbox = _norm_bbox(paragraph.get("bbox"), width, height)
        if not bbox:
            continue

        dla_type = str(paragraph.get("type") or "Unknown")
        label = dla_type.lower()

        score = paragraph.get("confidence")
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError):
            score = None

        result.blocks.append(
            LayoutBlock(
                label=label,
                bbox=bbox,
                score=score,
                content=paragraph.get("contents"),
                # PaddleX 의 `parsing_res_list`/`layout_det_res` 와 구분한다.
                source="dla_paragraphs",
                table=_cell_grid(paragraph, width, height) if label == "table" else None,
            )
        )

        for raw_line in paragraph.get("lines") or []:
            if not isinstance(raw_line, dict):
                continue
            line_bbox = _norm_bbox(raw_line.get("bbox"), width, height)
            if not line_bbox:
                continue
            result.ocr_lines.append(
                Line(
                    text=str(raw_line.get("contents") or ""),
                    bbox=line_bbox,
                    # ETL 은 줄 단위 인식 신뢰도를 주지 않는다(전환 계획 G1).
                    # paragraph 의 DLA confidence 는 다른 물리량이라 여기 넣지 않는다.
                    confidence=None,
                    source="ocr",
                )
            )

    return result


def page_results_from_dla(doc: dict) -> list[PaddleXPageResult]:
    """Default JSON 문서 전체 → 페이지 순서대로의 결과 목록.

    `pages[].pageId` 는 1부터 오지만 순서를 신뢰하지 않고 `pageId` 로 정렬한다 —
    비동기 처리라 순서가 뒤바뀔 여지를 남기지 않는다.
    """
    pages = [p for p in (doc.get("pages") or []) if isinstance(p, dict)]
    pages.sort(key=lambda p: _positive_int(p.get("pageId")) or 0)
    return [page_result_from_dla(p) for p in pages]


def rescale(result: PaddleXPageResult, width: int, height: int) -> PaddleXPageResult:
    """결과 좌표를 목표 캔버스 크기로 옮긴다 (전환 계획 G5).

    x·y 배율을 따로 계산한다 — 같은 페이지를 렌더한 것이라면 종횡비가 같아 두 배율이
    같겠지만, 다르면 좌표를 통째로 어긋나게 하는 쪽보다 축별로 맞추는 편이 안전하다.
    `source_width`/`source_height` 가 비어 있으면 배율을 알 수 없어 **그대로 돌려준다**
    (조용히 0 으로 나누거나 1 로 가정하지 않는다).
    """
    if result.source_width <= 0 or result.source_height <= 0:
        return result
    sx = width / result.source_width
    sy = height / result.source_height
    if sx == 1.0 and sy == 1.0:
        return result

    def box(b: list[int] | None) -> list[int] | None:
        if not b:
            return None
        return [round(b[0] * sx), round(b[1] * sy), round(b[2] * sx), round(b[3] * sy)]

    scaled = PaddleXPageResult(source_width=width, source_height=height)
    scaled.ocr_lines = [
        line.model_copy(update={"bbox": box(line.bbox)}) for line in result.ocr_lines
    ]
    for block in result.blocks:
        table = None
        if block.table:
            table = dict(block.table)
            table["cells"] = [
                {**cell, "bbox": box(cell.get("bbox"))} for cell in block.table["cells"]
            ]
        scaled.blocks.append(
            LayoutBlock(
                label=block.label,
                bbox=box(block.bbox) or block.bbox,
                score=block.score,
                content=block.content,
                source=block.source,
                table=table,
                tile_indices=block.tile_indices,
                tile_spans=block.tile_spans,
            )
        )
    return scaled
