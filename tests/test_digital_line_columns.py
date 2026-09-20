"""PDF 디지털 텍스트가 세로 구분선을 넘어 붙지 않는지 검증한다.

`_lines_from_runs` 는 소속 런을 모르는 글자가 하나라도 있으면 통째로 폴백했다.
pdfium 이 홀로 놓인 공백 글리프의 TEXT 객체를 돌려주지 않는 일이 잦아 실제로는
거의 모든 PDF 가 폴백으로 떨어졌고, 폴백인 글자 군집에는 구분선 인식이 없어
2단 편집물의 좌우 칸이 한 줄로 붙었다.
"""
from __future__ import annotations

import ctypes
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest

from nh_parser.ingest import triage

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "spark1118-sample"
TWO_COLUMN = SAMPLES / "1. 예금성상품(적립식).pdf"


def _chars(textpage) -> list:
    out = []
    for index in range(textpage.count_chars()):
        char = textpage.get_text_range(index, 1)
        if not char or char in ("\r", "\n"):
            continue
        try:
            tight = textpage.get_charbox(index)
        except Exception:  # noqa: BLE001
            continue
        try:
            owner = pdfium_c.FPDFText_GetTextObject(textpage.raw, index)
            key = ctypes.cast(owner, ctypes.c_void_p).value if owner else None
        except Exception:  # noqa: BLE001
            key = None
        out.append((char, tight, tight, None, key))
    return out


@pytest.mark.skipif(not TWO_COLUMN.exists(), reason="고객 자료는 저장소에 없다")
def test_two_column_pdf_uses_the_structured_path_and_keeps_every_char():
    page = pdfium.PdfDocument(str(TWO_COLUMN))[0]
    chars = _chars(page.get_textpage())
    runs = triage._text_runs(page)
    dividers = triage._vertical_dividers(page, chars)

    lines = triage._lines_from_runs(chars, runs, dividers)

    assert lines is not None, "공백 하나 때문에 구조 경로를 포기하면 안 된다"
    assert sum(len(line) for line in lines) == len(chars), "글자를 잃지 않아야 한다"


@pytest.mark.skipif(not TWO_COLUMN.exists(), reason="고객 자료는 저장소에 없다")
def test_no_line_crosses_a_drawn_vertical_divider():
    page = pdfium.PdfDocument(str(TWO_COLUMN))[0]
    chars = _chars(page.get_textpage())
    dividers = triage._vertical_dividers(page, chars)
    assert dividers, "이 샘플에는 세로 구분선이 그려져 있다"

    lines = triage._lines_from_runs(chars, triage._text_runs(page), dividers)

    crossing = []
    for line in lines:
        x0 = min(c[1][0] for c in line)
        x1 = max(c[1][2] for c in line)
        y0 = min(c[1][1] for c in line)
        y1 = max(c[1][3] for c in line)
        if any(x0 < x < x1 and low < y1 and high > y0 for x, low, high in dividers):
            crossing.append("".join(c[0] for c in line))
    assert crossing == [], f"구분선을 넘은 줄: {crossing[:3]}"


def test_non_space_orphan_still_falls_back():
    """공백이 아닌 글자의 소속을 모르면 종전대로 폴백한다."""
    runs = [{"key": 1, "box": (0.0, 0.0, 10.0, 10.0), "baseline": 2.0, "size": 8.0}]
    chars = [
        ("가", (0.0, 0.0, 5.0, 8.0), (0.0, 0.0, 5.0, 8.0), None, 1),
        ("나", (5.0, 0.0, 9.0, 8.0), (5.0, 0.0, 9.0, 8.0), None, 999),
    ]

    assert triage._lines_from_runs(chars, runs, []) is None


def test_orphan_space_joins_the_run_on_its_baseline():
    runs = [
        {"key": 1, "box": (0.0, 0.0, 10.0, 10.0), "baseline": 2.0, "size": 8.0},
        {"key": 2, "box": (0.0, 40.0, 10.0, 50.0), "baseline": 42.0, "size": 8.0},
    ]
    space = (" ", (5.0, 1.0, 6.0, 7.0), (5.0, 1.0, 6.0, 7.0), None, None)
    chars = [
        ("가", (0.0, 0.0, 5.0, 8.0), (0.0, 0.0, 5.0, 8.0), None, 1),
        space,
        ("다", (0.0, 40.0, 5.0, 48.0), (0.0, 40.0, 5.0, 48.0), None, 2),
    ]

    lines = triage._lines_from_runs(chars, runs, [])

    assert lines is not None
    assert sum(len(line) for line in lines) == 3
    joined = ["".join(c[0] for c in line) for line in lines]
    assert "가 " in joined or " 가" in joined
