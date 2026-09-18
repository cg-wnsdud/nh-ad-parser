# -*- coding: utf-8 -*-
"""실험용 손잡이: 기본값에서는 요청이 한 글자도 안 달라져야 한다.

`"server"` 센티널의 요점은 **키를 안 보내는 것**이다. 스칼라를 하나라도 보내면 서버
YAML 의 클래스별 dict 가 통째로 무력화되므로, 기본 경로에 새 키가 새어 나가지 않는지를
회귀로 못박는다.
"""
from __future__ import annotations

import dataclasses

import pytest
from PIL import Image

from nh_parser import pipeline, trace
from nh_parser.config import SETTINGS
from nh_parser.ir import Line
from nh_parser.label_studio import block_to_rectangle
from nh_parser.ocr import paddlex as paddlex_mod
from nh_parser.ocr.paddlex import LayoutBlock, PaddleXPageResult

_IMAGE = Image.new("RGB", (8, 8), "white")

# 실험 필드가 추가되기 전(2026-09-16 fc87 동등성 검증 시점)의 요청 본문.
_BASELINE_KEYS = {
    "fileType",
    "useDocOrientationClassify",
    "useDocUnwarping",
    "textDetLimitSideLen",
    "textDetLimitType",
    "useFormulaRecognition",
    "useTextlineOrientation",
    "layoutMergeBboxesMode",
}


def _with(monkeypatch, **overrides):
    monkeypatch.setattr(paddlex_mod, "SETTINGS", dataclasses.replace(SETTINGS, **overrides))


def test_default_payload_has_no_new_keys(monkeypatch):
    _with(monkeypatch)  # 저장소 기본값 그대로
    payload = paddlex_mod.build_request_payload(_IMAGE)

    assert set(payload) == _BASELINE_KEYS
    assert payload["textDetLimitSideLen"] == 2500
    assert payload["layoutMergeBboxesMode"] == "small"


def test_server_sentinel_drops_the_key(monkeypatch):
    _with(monkeypatch, paddlex_layout_merge_bboxes_mode="server",
          paddlex_text_det_limit_side_len="server", paddlex_text_det_limit_type="server")
    payload = paddlex_mod.build_request_payload(_IMAGE)

    assert "layoutMergeBboxesMode" not in payload
    assert "textDetLimitSideLen" not in payload
    assert "textDetLimitType" not in payload


def test_experiment_knobs_reach_the_payload(monkeypatch):
    _with(
        monkeypatch,
        paddlex_layout_nms="false",
        paddlex_use_region_detection="true",
        paddlex_use_table_recognition="false",
        paddlex_text_det_thresh="0.25",
        paddlex_text_det_box_thresh="0.5",
        paddlex_text_det_unclip_ratio="1.8",
        paddlex_text_rec_score_thresh="0.3",
        paddlex_layout_threshold="0.35",
    )
    payload = paddlex_mod.build_request_payload(_IMAGE)

    assert payload["layoutNms"] is False
    assert payload["useRegionDetection"] is True
    assert payload["useTableRecognition"] is False
    assert payload["textDetThresh"] == 0.25
    assert payload["textDetBoxThresh"] == 0.5
    assert payload["textDetUnclipRatio"] == 1.8
    assert payload["textRecScoreThresh"] == 0.3
    assert payload["layoutThreshold"] == 0.35


def test_unreadable_bool_fails_before_the_request(monkeypatch):
    _with(monkeypatch, paddlex_layout_nms="아마도")
    with pytest.raises(ValueError):
        paddlex_mod.build_request_payload(_IMAGE)


def _fake_result(_image) -> PaddleXPageResult:
    return PaddleXPageResult(
        ocr_lines=[Line(text="NH", bbox=[20, 20, 70, 40], confidence=0.99, source="ocr")],
        blocks=[
            LayoutBlock(label="text", bbox=[10, 10, 190, 90]),
            LayoutBlock(label="text", bbox=[10, 10, 190, 90], score=0.88,
                        source="layout_det_res"),
        ],
    )


def test_trace_keeps_both_layers(tmp_path, monkeypatch):
    path = tmp_path / "sample.png"
    Image.new("RGB", (200, 100), "white").save(path)
    monkeypatch.setattr(pipeline, "request_layout_parsing", _fake_result)

    with trace.capture() as layers:
        doc = pipeline.process_file(path, use_vlm=False)

    assert len(layers) == 1
    page = layers[0]
    assert page["canvas_w"] == 200 and page["canvas_h"] == 100
    assert [tile["y_offset"] for tile in page["tiles"]] == [0]
    # 모델의 답 — 확신도가 붙어 오는 목록만 골라도 박스가 남아야 한다
    det = [b for b in page["blocks"] if b["source"] == "layout_det_res"]
    assert det == [{"label": "text", "bbox": [10, 10, 190, 90], "score": 0.88,
                    "source": "layout_det_res", "tile_indices": [0]}]
    # 우리 로직의 답
    assert doc.pages[0].regions[0].label == "text"


def test_trace_is_off_by_default(tmp_path, monkeypatch):
    path = tmp_path / "sample.png"
    Image.new("RGB", (200, 100), "white").save(path)
    monkeypatch.setattr(pipeline, "request_layout_parsing", _fake_result)

    pipeline.process_file(path, use_vlm=False)

    assert trace.active() is False


def test_block_rectangle_matches_region_rectangle_geometry():
    rect = block_to_rectangle(
        {"label": "text", "bbox": [10, 20, 60, 70], "score": 0.9},
        width=200, height=100, box_id="p1_x000",
    )

    assert rect["id"] == "p1_x000"
    assert rect["score"] == 0.9
    assert rect["value"] == {
        "x": 5.0, "y": 20.0, "width": 25.0, "height": 50.0,
        "rotation": 0, "rectanglelabels": ["text"],
    }
