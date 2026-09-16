# -*- coding: utf-8 -*-
"""NH 입력을 파서와 같은 페이지 좌표계 PNG와 Label Studio task로 변환한다.

    uv run python tools/prepare_layout_labeling.py --input samples

모델 호출은 하지 않는다. 이미지 정규화와 PDF 렌더링만 수행한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pypdfium2 as pdfium

sys.stdout.reconfigure(encoding="utf-8")

from nh_parser.ingest.canvas import load_image_canvas, native_image_dpi, render_pdf_page
from nh_parser.ingest.triage import triage_page


IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
INPUT_EXTS = IMAGE_EXTS | {".pdf"}
DEFAULT_ROOT = Path("experiments/layout-labeling")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def targets(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in INPUT_EXTS)


def safe_stem(path: Path) -> str:
    short_hash = sha256(path)[:12]
    clean = "".join(c if c.isalnum() or c in "-_." else "_" for c in path.stem).strip("._")
    return f"{clean[:80]}__{short_hash}"


def render(path: Path):
    if path.suffix.lower() in IMAGE_EXTS:
        canvas = load_image_canvas(path)
        yield canvas, "image"
        return

    pdf = pdfium.PdfDocument(str(path))
    for index, page in enumerate(pdf):
        verdict = triage_page(page)
        dpi = native_image_dpi(page) if verdict.verdict in {"scan_like", "hybrid"} else None
        yield render_pdf_page(page, index + 1, dpi=dpi), verdict.verdict


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()

    source_paths = targets(args.input)
    if not source_paths:
        raise SystemExit(f"지원하는 PDF/이미지가 없습니다: {args.input}")

    media_dir = args.root / ".local" / "media"
    tasks_dir = args.root / "tasks"
    media_dir.mkdir(parents=True, exist_ok=True)
    tasks_dir.mkdir(parents=True, exist_ok=True)

    task_rows: list[dict] = []
    manifest: dict = {"schema": "nh-layout-labeling-manifest-v1", "documents": []}
    task_id = 1
    seen_source_hashes: dict[str, Path] = {}

    for source in source_paths:
        source_hash = sha256(source)
        if source_hash in seen_source_hashes:
            print(f"{source.name}: 같은 bytes의 입력을 이미 사용함 — {seen_source_hashes[source_hash]}")
            continue
        seen_source_hashes[source_hash] = source
        doc_key = safe_stem(source)
        pages = []
        for canvas, route in render(source):
            filename = f"{doc_key}__p{canvas.page_no:03d}.png"
            output = media_dir / filename
            canvas.image.save(output, format="PNG", optimize=False)
            width, height = canvas.image.size
            task_rows.append({
                "id": task_id,
                "data": {
                    "image": f"/data/local-files/?d=pages/{filename}",
                    "source_file": source.name,
                    "source_sha256": source_hash,
                    "page_no": canvas.page_no,
                    "canvas_width": width,
                    "canvas_height": height,
                    "parse_route": route,
                },
            })
            pages.append({
                "page_no": canvas.page_no,
                "file": filename,
                "width": width,
                "height": height,
                "dpi": canvas.dpi,
                "px_per_pt": canvas.px_per_pt,
                "parse_route": route,
            })
            task_id += 1
        manifest["documents"].append({
            "source": str(source),
            "source_sha256": source_hash,
            "pages": pages,
        })
        print(f"{source.name}: {len(pages)}쪽")

    (tasks_dir / "tasks.json").write_text(
        json.dumps(task_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tasks_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"tasks: {len(task_rows)} -> {tasks_dir / 'tasks.json'}")
    print(f"media -> {media_dir}")


if __name__ == "__main__":
    main()
