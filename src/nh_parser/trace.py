# -*- coding: utf-8 -*-
"""영역 실험용 중간 산출물 수집 훅 — **기본 비활성**.

왜 필요한가. 최종 Region 하나만 보면 박스가 틀렸을 때 원인을 못 가른다. 모델이
애초에 못 잡은 것인지, 타일 경계에서 잘린 것인지, 타일 중복 제거가 먹은 것인지,
OCR 줄을 Region 에 붙이는 규칙이 경계를 키운 것인지가 전부 같은 그림으로 보인다.
그래서 `_ocr_canvas_to_page` 가 지나가는 자리에서 **PaddleX 가 돌려준 원본 박스**를
따로 남긴다.

운영 경로에는 영향이 없다. `capture()` 안에서 실행할 때만 수집하고, 그 밖에서는
`active()` 가 False 라 dict 를 만드는 비용조차 들지 않는다.
"""
from __future__ import annotations

from contextlib import contextmanager

_BUCKET: list[dict] | None = None


def active() -> bool:
    return _BUCKET is not None


@contextmanager
def capture():
    """수집을 켠다. 중첩해도 바깥 수집기를 망가뜨리지 않는다."""
    global _BUCKET
    previous = _BUCKET
    bucket: list[dict] = []
    _BUCKET = bucket
    try:
        yield bucket
    finally:
        _BUCKET = previous


def record(page: dict) -> None:
    if _BUCKET is not None:
        _BUCKET.append(page)


def block_record(block) -> dict:
    """LayoutBlock 을 직렬화 가능한 dict 로. 좌표계는 호출한 쪽이 책임진다."""
    return {
        "label": block.label,
        "bbox": [int(v) for v in block.bbox],
        "score": block.score,
        "source": block.source,
        "tile_indices": list(block.tile_indices),
    }
