"""파서 영역 좌표를 Label Studio rectangle prediction으로 변환한다."""
from __future__ import annotations

PP_LAYOUT_LABELS = {
    "paragraph_title", "image", "text", "number", "abstract", "content",
    "figure_title", "formula", "table", "reference", "doc_title", "footnote",
    "header", "algorithm", "footer", "seal", "chart", "formula_number",
    "aside_text", "reference_content",
}


def region_to_rectangle(region: dict, width: int, height: int) -> dict | None:
    """AdDocument Region 하나를 PP 레이아웃 프로젝트의 percentage bbox로 바꾼다."""
    bbox = region.get("bbox")
    label = str(region.get("label") or "")
    if not bbox or len(bbox) != 4 or label not in PP_LAYOUT_LABELS:
        return None
    x0, y0, x1, y1 = (float(value) for value in bbox)
    if x1 <= x0 or y1 <= y0:
        return None

    def percent(value: float, limit: int) -> float:
        return max(0.0, min(100.0, value / max(1, limit) * 100.0))

    result = {
        "id": str(region.get("region_id") or f"region-{x0}-{y0}"),
        "from_name": "pp_layout",
        "to_name": "image",
        "type": "rectanglelabels",
        "original_width": width,
        "original_height": height,
        "image_rotation": 0,
        "value": {
            "x": percent(x0, width),
            "y": percent(y0, height),
            "width": percent(x1 - x0, width),
            "height": percent(y1 - y0, height),
            "rotation": 0,
            "rectanglelabels": [label],
        },
    }
    if region.get("layout_score") is not None:
        result["score"] = region["layout_score"]
    return result
