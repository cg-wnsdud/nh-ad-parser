# -*- coding: utf-8 -*-
"""review-input(P3) JSON + pages 미리보기를 하나의 검수용 HTML로 묶는다.

    uv run python tools/build_html_review.py --out out/<날짜>/<라벨>

<out>/review-input/*.json 전부를 다음 심의 단계가 실제로 받는 형태 그대로 보여준다.
영역 bbox·좌표는 <out>/parse/*.json 의 canvas_w/canvas_h 만 가져온다(P3에는 없음).
이미지는 <out>/pages/*_p<N>.jpg 를 base64로 파일 안에 그대로 넣으므로 별도 서버 없이
더블클릭으로 열어볼 수 있다.

새 OCR/VLM 재실행 없이 review.html만 다시 만들고 싶으면 이 스크립트만 다시 돌리면
된다(review-input/parse/pages를 다시 만들지 않는다).
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any


def _b64_image(path: Path) -> str:
    data = path.read_bytes()
    return f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}"


def _region_payload(region: dict[str, Any]) -> dict[str, Any]:
    layout = region.get("layout") or {}
    return {
        "region_id": region.get("region_id"),
        "bbox": region.get("bbox"),
        "layout_label": layout.get("label"),
        "layout_score": layout.get("score"),
        "card_no": (region.get("scope") or {}).get("card_no"),
        "selected_text": region.get("selected_text"),
        "selected_source": region.get("selected_source"),
        "selection_status": region.get("selection_status"),
        "needs_review": bool(region.get("needs_review")),
        "confidence": region.get("confidence"),
        "reason": region.get("reason"),
        "line_refs": region.get("line_refs") or [],
        "labels": region.get("labels") or [],
        "table": region.get("table"),
        "empty": False,
    }


def _empty_region_payload(region: dict[str, Any]) -> dict[str, Any]:
    return {
        "region_id": region.get("region_id"),
        "bbox": region.get("bbox"),
        "layout_label": region.get("layout_label"),
        "layout_score": None,
        "card_no": None,
        "selected_text": None,
        "selected_source": None,
        "selection_status": "empty_region",
        "needs_review": False,
        "confidence": None,
        "reason": "텍스트 없는 영역(이미지/장식 등)",
        "line_refs": [],
        "labels": [],
        "table": None,
        "empty": True,
    }


def _canvas_sizes(parse_doc: dict[str, Any]) -> dict[int, tuple[int, int]]:
    return {
        int(page.get("page_no") or 0): (page.get("canvas_w") or 0, page.get("canvas_h") or 0)
        for page in parse_doc.get("pages") or []
    }


def _page_payload(
    page: dict[str, Any],
    empty_regions: list[dict[str, Any]],
    recovery: list[dict[str, Any]],
    canvas_sizes: dict[int, tuple[int, int]],
    pages_dir: Path,
    doc_id: str,
) -> dict[str, Any]:
    page_no = page.get("page_no")
    canvas_w, canvas_h = canvas_sizes.get(int(page_no or 0), (0, 0))
    image_path = pages_dir / f"{doc_id}_p{page_no}.jpg"
    regions = [_region_payload(region) for region in page.get("regions") or []]
    regions.extend(_empty_region_payload(region) for region in empty_regions)
    return {
        "page_no": page_no,
        "canvas_w": canvas_w,
        "canvas_h": canvas_h,
        "parse_status": page.get("parse_status"),
        "image": _b64_image(image_path) if image_path.exists() else None,
        "image_missing": image_path.name if not image_path.exists() else None,
        "regions": regions,
        "unassigned_text": page.get("unassigned_text") or [],
        "recovery_candidates": recovery,
    }


def _doc_payload(review: dict[str, Any], parse_doc: dict[str, Any], pages_dir: Path) -> dict[str, Any]:
    document = review.get("document") or {}
    doc_id = document.get("doc_id") or ""
    canvas_sizes = _canvas_sizes(parse_doc)
    empty_by_page: dict[int, list[dict[str, Any]]] = {}
    for region in (review.get("diagnostics") or {}).get("empty_regions") or []:
        empty_by_page.setdefault(int(region.get("page_no") or 0), []).append(region)
    recovery_by_page: dict[int, list[dict[str, Any]]] = {}
    for candidate in review.get("unverified_recovery_candidates") or []:
        recovery_by_page.setdefault(int(candidate.get("page_no") or 0), []).append(candidate)

    pages = [
        _page_payload(
            page,
            empty_by_page.get(int(page.get("page_no") or 0), []),
            recovery_by_page.get(int(page.get("page_no") or 0), []),
            canvas_sizes,
            pages_dir,
            doc_id,
        )
        for page in review.get("pages") or []
    ]
    summary = dict(review.get("summary") or {})
    summary["unlabelled_region_count"] = summary.get("region_count", 0) - summary.get(
        "labelled_region_count", 0,
    )
    return {
        "doc_id": doc_id,
        "source_file": document.get("source_file"),
        "file_type": document.get("file_type"),
        "classification": document.get("classification") or {},
        "template": document.get("template") or {},
        "summary": summary,
        "pages": pages,
        "label_index": review.get("label_index") or [],
    }


def build(out_dir: Path) -> Path:
    review_dir = out_dir / "review-input"
    parse_dir = out_dir / "parse"
    pages_dir = out_dir / "pages"
    review_paths = sorted(review_dir.glob("*.json"))
    if not review_paths:
        raise SystemExit(f"review-input JSON을 찾을 수 없습니다: {review_dir}")

    docs = []
    for path in review_paths:
        review = json.loads(path.read_text(encoding="utf-8"))
        parse_path = parse_dir / path.name
        parse_doc = json.loads(parse_path.read_text(encoding="utf-8")) if parse_path.exists() else {}
        docs.append(_doc_payload(review, parse_doc, pages_dir))

    template_path = Path(__file__).with_name("_html_review_template.html")
    html = template_path.read_text(encoding="utf-8")
    html = html.replace(
        "__REVIEW_DATA__", json.dumps(docs, ensure_ascii=False).replace("</", "<\\/"),
    )

    dest = out_dir / "review.html"
    dest.write_text(html, encoding="utf-8")
    return dest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True, help="parse.py --out 와 같은 경로")
    args = ap.parse_args()
    dest = build(args.out)
    print(f"review.html -> {dest}")


if __name__ == "__main__":
    main()
