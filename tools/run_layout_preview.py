# -*- coding: utf-8 -*-
"""원본 입력을 NH 전처리와 PaddleX에 통과시켜 Label Studio 예측 task를 만든다.

PowerShell 예시::

    uv run python tools/run_layout_preview.py `
      --input "samples/[예금성상품-적금] 올원e적금.png"

기본 PaddleX URL은 spark-1118 SSH tunnel의 로컬 끝점(127.0.0.1:18081)이다.
VLM은 호출하지 않는다. 산출물과 렌더 이미지는 모두 Git ignore 경로에 둔다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_stem(path: Path, source_hash: str) -> str:
    clean = "".join(
        char if char.isalnum() or char in "-_." else "_" for char in path.stem
    ).strip("._")
    return f"{clean[:80]}__{source_hash[:12]}"


def _render_source(path: Path):
    """Label Studio 캔버스를 실제 parser와 같은 PDF 렌더 분기로 만든다."""
    import pypdfium2 as pdfium

    from nh_parser.ingest.canvas import (
        load_image_canvas, native_image_dpi, render_pdf_page,
    )
    from nh_parser.ingest.triage import triage_page

    if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        yield load_image_canvas(path), "image"
        return

    if path.suffix.lower() != ".pdf":
        raise ValueError("Label Studio 영역 미리보기는 PDF/PNG/JPG만 지원합니다")

    pdf = pdfium.PdfDocument(str(path))
    for index, pdf_page in enumerate(pdf):
        verdict = triage_page(pdf_page)
        dpi = (
            native_image_dpi(pdf_page)
            if verdict.verdict in {"scan_like", "hybrid"}
            else None
        )
        yield render_pdf_page(pdf_page, index + 1, dpi=dpi), verdict.verdict


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--paddlex-url",
        default="http://127.0.0.1:18081/layout-parsing",
        help="기본값: spark-1118 SSH tunnel의 로컬 끝점",
    )
    parser.add_argument(
        "--merge-mode", choices=("server", "small", "large", "union"), default="server",
        help="server면 HTTP key를 생략해 spark-1118 YAML의 클래스별 값을 사용",
    )
    parser.add_argument("--threshold", default="server")
    parser.add_argument("--unclip", default="server")
    parser.add_argument(
        "--root", type=Path, default=Path("experiments/layout-labeling"),
    )
    parser.add_argument("--run-name", default="spark-1118-base")
    args = parser.parse_args()

    source = args.input.resolve()
    if not source.is_file():
        raise SystemExit(f"입력 파일이 없습니다: {source}")

    # config 모듈 import 전에 지정해야 Settings에 반영된다. .env의 fc87 값보다 process
    # 환경변수가 우선하므로 파일을 수정하지 않고 이번 실행만 spark-1118로 분기한다.
    os.environ["PADDLEX_URL"] = args.paddlex_url
    os.environ["PADDLEX_LAYOUT_MERGE_BBOXES_MODE"] = args.merge_mode
    os.environ["PADDLEX_LAYOUT_THRESHOLD"] = args.threshold
    os.environ["PADDLEX_LAYOUT_UNCLIP_RATIO"] = args.unclip
    os.environ["REGION_READING_MODE"] = "off"

    from nh_parser.label_studio import region_to_rectangle
    from nh_parser.pipeline import process_file

    run_dir = args.root / "tasks" / "runs" / args.run_name
    preview_dir = run_dir / "preview"
    media_dir = args.root / ".local" / "media"
    source_hash = _sha256(source)
    source_key = _safe_stem(source, source_hash)

    started = time.time()
    document = process_file(source, preview_dir=preview_dir, use_vlm=False)
    payload = json.loads(document.model_dump_json())
    _write_json(run_dir / "parse.json", payload)

    canvases = list(_render_source(source))
    pages = payload.get("pages") or []
    if len(canvases) != len(pages):
        raise RuntimeError(
            f"렌더 페이지 {len(canvases)}개와 파싱 페이지 {len(pages)}개가 다릅니다"
        )

    tasks = []
    skipped_labels: dict[str, int] = {}
    for index, ((canvas, route), page) in enumerate(zip(canvases, pages), start=1):
        width, height = canvas.image.size
        if (page.get("canvas_w"), page.get("canvas_h")) != (width, height):
            raise RuntimeError(
                f"p{index}: 파싱 좌표계와 Label Studio 캔버스가 다릅니다: "
                f"{page.get('canvas_w')}x{page.get('canvas_h')} != {width}x{height}"
            )
        filename = f"{source_key}__p{index:03d}.png"
        media_dir.mkdir(parents=True, exist_ok=True)
        canvas.image.save(media_dir / filename, format="PNG", optimize=False)

        rectangles = []
        for region in page.get("regions") or []:
            result = region_to_rectangle(region, width, height)
            if result is None:
                label = str(region.get("label") or "unknown")
                skipped_labels[label] = skipped_labels.get(label, 0) + 1
            else:
                rectangles.append(result)

        scores = [item["score"] for item in rectangles if item.get("score") is not None]
        prediction = {
            "model_version": args.run_name,
            "result": rectangles,
        }
        if scores:
            prediction["score"] = sum(scores) / len(scores)
        tasks.append({
            "data": {
                "image": f"/data/local-files/?d=pages/{filename}",
                "source_file": source.name,
                "source_sha256": source_hash,
                "page_no": index,
                "canvas_width": width,
                "canvas_height": height,
                "parse_route": route,
            },
            "predictions": [prediction],
        })

    import_path = run_dir / "label-studio-import.json"
    _write_json(import_path, tasks)
    _write_json(run_dir / "run.json", {
        "input": str(source),
        "source_sha256": source_hash,
        "paddlex_url": args.paddlex_url,
        "merge_mode": args.merge_mode,
        "threshold": args.threshold,
        "unclip": args.unclip,
        "use_vlm": False,
        "seconds": round(time.time() - started, 3),
        "pages": len(pages),
        "regions": sum(len(page.get("regions") or []) for page in pages),
        "skipped_prediction_labels": skipped_labels,
    })

    region_count = sum(len(page.get("regions") or []) for page in pages)
    print(f"완료: {len(pages)}쪽 / 영역 {region_count}개 / VLM 호출 없음")
    print(f"Label Studio import -> {import_path}")
    print(f"파싱 JSON           -> {run_dir / 'parse.json'}")
    print(f"영역 overlay        -> {preview_dir}")
    if skipped_labels:
        print(f"PP 20종 밖이라 prediction에서 제외: {skipped_labels}")


if __name__ == "__main__":
    main()
