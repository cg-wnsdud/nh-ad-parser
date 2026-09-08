"""농협 ETL with LLM (AgileSoDA) Default JSON → 우리 IR 변환.

규격은 [docs/농협-ocr-연동-규격.md](../../../docs/농협-ocr-연동-규격.md),
전환 배경과 격차는 [docs/농협-ocr-전환-계획.md](../../../docs/농협-ocr-전환-계획.md).

**이 모듈은 변환만 한다.** 공개 API 호출 프로토타입은
``integrations/etlwithllm/client.py``에 분리되어 있다. 로그인·워크스페이스 생성은 농협
준비 범위라 구현하지 않고, 이미 인증된 세션과 전달받은 ``ws_id``를 주입한다.

`paddlex.py` 와 같은 `PaddleXPageResult` 를 낸다. 이름이 PaddleX 지만 하류(영역 조립·
템플릿·검수)가 이미 그 모양을 소비하고 있어서, provider 를 바꿀 때 하류를 건드리지
않으려면 이 모양을 지키는 것이 맞다.

좌표는 **ETL 자체 렌더 기준**으로 그대로 낸다(`source_width`/`source_height` 에 그
크기를 담는다). 우리 캔버스(`pdf_render_dpi=200`)와 다르므로 VLM 크롭처럼 캔버스
좌표를 쓰는 단계 앞에서 `rescale` 을 거쳐야 한다 — 안 맞추면 재판독이 엉뚱한 자리를
본다(전환 계획 G5).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


class EtlDefaultJsonError(ValueError):
    """ETL 응답에서 가이드의 Default JSON 본문을 찾지 못했을 때 발생한다."""


def normalize_default_document(payload: Any) -> dict:
    """실제 응답 변형을 가이드의 Default JSON 한 형태로 정규화한다.

    부록 p.57은 ``doc_result`` 바로 아래에 ``pdfName/pageLen/pages``가 있다고
    설명하지만, 앞쪽 callback 예시는 ``doc_result.default`` 한 단계를 더 둔다.
    결과 조회 API도 플랫폼 공통 ``data`` envelope를 씌울 수 있어 세 형태를 모두
    수용한다. 모르는 키를 재귀적으로 뒤지지는 않는다. 계약 오타가 생겼을 때 우연히
    다른 ``pages``를 집어 드는 것보다 명시적으로 실패하는 편이 안전하다.
    """
    if not isinstance(payload, dict):
        raise EtlDefaultJsonError("ETL 결과는 JSON object여야 합니다")

    candidates: list[tuple[str, Any]] = [("root", payload)]
    seen: set[int] = set()
    while candidates:
        location, value = candidates.pop(0)
        if not isinstance(value, dict) or id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value.get("pages"), list) and (
            "pdfName" in value or "pageLen" in value
        ):
            return value
        for key in ("data", "doc_result", "default"):
            child = value.get(key)
            if isinstance(child, dict):
                candidates.append((f"{location}.{key}", child))

    raise EtlDefaultJsonError(
        "Default JSON을 찾지 못했습니다. 기대 경로: root, data, "
        "doc_result, doc_result.default"
    )


def load_default_document(path: Path, *, source_name: str | None = None) -> dict:
    """``dla_result_path``가 파일/디렉터리 어느 쪽이어도 Default JSON을 찾는다.

    ``transform()``의 인자 이름만 문서에 있고 실제 경로 형태는 공개되지 않았다.
    파일이면 바로 읽고, 디렉터리면 결과형 접미사(``_pages`` 등)를 제외한 JSON을
    검사한다. 여러 후보가 있으면 ``pdfName``이 원본 파일명과 일치하는 것을 고른다.
    그래도 하나로 정해지지 않으면 조용히 임의 선택하지 않는다.
    """
    target = Path(path)
    if target.is_file():
        try:
            return normalize_default_document(json.loads(target.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise EtlDefaultJsonError(f"JSON 파싱 실패: {target}: {exc}") from exc
    if not target.is_dir():
        raise EtlDefaultJsonError(f"DLA 결과 경로가 존재하지 않습니다: {target}")

    excluded = ("_pages.json", "_page_grouped.json", "_chunk_data.json", "_edit.json")
    matches: list[tuple[Path, dict]] = []
    failures: list[str] = []
    for candidate in sorted(target.glob("*.json")):
        if candidate.name.endswith(excluded):
            continue
        try:
            doc = normalize_default_document(json.loads(candidate.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, EtlDefaultJsonError) as exc:
            failures.append(f"{candidate.name}: {exc}")
            continue
        matches.append((candidate, doc))

    if source_name:
        exact = [item for item in matches if str(item[1].get("pdfName") or "") == source_name]
        if len(exact) == 1:
            return exact[0][1]
        if len(exact) > 1:
            names = ", ".join(p.name for p, _ in exact)
            raise EtlDefaultJsonError(f"원본과 일치하는 Default JSON이 여러 개입니다: {names}")
    if len(matches) == 1:
        return matches[0][1]
    if not matches:
        detail = f" ({'; '.join(failures)})" if failures else ""
        raise EtlDefaultJsonError(f"Default JSON 후보가 없습니다: {target}{detail}")
    names = ", ".join(p.name for p, _ in matches)
    raise EtlDefaultJsonError(f"Default JSON 후보가 여러 개입니다: {names}")

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
