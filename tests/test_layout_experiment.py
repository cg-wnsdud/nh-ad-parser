# -*- coding: utf-8 -*-
"""spark-1118 영역 실험 경로: 실제 전처리를 유지하면서 VLM만 끈다."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from nh_parser import pipeline
from nh_parser.ir import Line
from nh_parser.label_studio import region_to_rectangle
from nh_parser.ocr.paddlex import LayoutBlock, PaddleXPageResult


def _image(tmp_path: Path) -> Path:
    path = tmp_path / "sample.png"
    Image.new("RGB", (200, 100), "white").save(path)
    return path


def _fake_paddlex(_image) -> PaddleXPageResult:
    return PaddleXPageResult(
        ocr_lines=[Line(text="NH", bbox=[20, 20, 70, 40], confidence=0.99, source="ocr")],
        blocks=[LayoutBlock(label="text", bbox=[10, 10, 190, 90], score=0.88)],
    )


def test_layout_mode_keeps_paddlex_and_skips_every_vlm_call(tmp_path, monkeypatch):
    calls = []

    def fake(image):
        calls.append(image.size)
        return _fake_paddlex(image)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("레이아웃 실험에서 VLM 진입점을 호출했다")

    monkeypatch.setattr(pipeline, "request_layout_parsing", fake)
    monkeypatch.setattr(pipeline, "_apply_vlm_judgments", unexpected)
    monkeypatch.setattr(pipeline, "_classify_into", unexpected)

    doc = pipeline.process_file(_image(tmp_path), use_vlm=False)

    assert calls == [(200, 100)], "입력 전처리 뒤 PaddleX 호출은 그대로 실행해야 한다"
    assert doc.pages[0].regions[0].label == "text"
    assert doc.pages[0].regions[0].lines[0].text == "NH"
    assert doc.product_group is None
    assert "VLM 호출 생략" in doc.notes[-1]


def test_default_process_file_still_runs_existing_vlm_path(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(pipeline, "request_layout_parsing", _fake_paddlex)
    monkeypatch.setattr(
        pipeline, "_apply_vlm_judgments", lambda page, image: called.append("page")
    )
    monkeypatch.setattr(
        pipeline, "_classify_into", lambda doc, image: called.append("document")
    )

    pipeline.process_file(_image(tmp_path))

    assert called == ["page", "document"]


def test_label_studio_rectangle_uses_page_percentage_coordinates():
    result = region_to_rectangle(
        {"region_id": "p1_r001", "label": "text", "bbox": [10, 20, 60, 70],
         "layout_score": 0.9},
        width=200,
        height=100,
    )

    assert result is not None
    assert result["value"] == {
        "x": 5.0,
        "y": 20.0,
        "width": 25.0,
        "height": 50.0,
        "rotation": 0,
        "rectanglelabels": ["text"],
    }
    assert result["score"] == 0.9


def test_label_studio_rectangle_skips_non_pp_label():
    assert region_to_rectangle(
        {"region_id": "p1_r001", "label": "product_panel", "bbox": [0, 0, 10, 10]},
        width=100,
        height=100,
    ) is None
