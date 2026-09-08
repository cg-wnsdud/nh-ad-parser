"""농협 DLA 결과를 기존 광고 파싱 후반부에 연결한다."""

from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium

from ...ingest.canvas import CanvasPage, load_image_canvas, render_pdf_page
from ...ir import AdDocument
from ...ocr.etlwithllm import normalize_default_document, page_results_from_dla, rescale
from ...pipeline import _apply_vlm_judgments, _assemble_page, _classify_into, _save_preview


_IMAGE_EXTS = {".png", ".jpg", ".jpeg"}


def process_file_from_dla(
    input_path: Path,
    dla_payload: dict,
    *,
    preview_dir: Path | None = None,
) -> AdDocument:
    """이미 완료된 ETL Default JSON을 받아 PaddleX 호출 없이 P1 전 단계를 수행한다.

    이 함수는 외부 API 방식과 ``transform()`` 방식이 공유한다. ETL이 이미 렌더·DLA·
    STR·TSR을 수행했으므로 기존 ``pipeline._ocr_canvas_to_page``의 타일링과 PaddleX
    호출은 사용하지 않는다. 페이지 이미지는 좌표 스케일과 VLM crop을 위해서만 렌더한다.
    """
    source = Path(input_path)
    default_doc = normalize_default_document(dla_payload)
    results = page_results_from_dla(default_doc)
    canvases = _load_canvases(source)
    declared = default_doc.get("pageLen")
    if declared is not None and int(declared) != len(results):
        raise ValueError(
            f"Default JSON pageLen과 pages 개수가 다릅니다: {declared} != {len(results)}"
        )
    if len(canvases) != len(results):
        raise ValueError(
            "원본 페이지와 ETL 결과 페이지 개수가 다릅니다: "
            f"source={len(canvases)}, etl={len(results)}. "
            "start_page/end_page 부분 분석은 아직 지원하지 않습니다."
        )

    file_type = "image" if source.suffix.lower() in _IMAGE_EXTS else "pdf"
    doc = AdDocument(doc_id=source.stem, source_file=source.name, file_type=file_type)
    for page_no, (canvas, result) in enumerate(zip(canvases, results), start=1):
        scaled = rescale(result, *canvas.image.size)
        page = _assemble_page(
            canvas,
            scaled.ocr_lines,
            scaled.blocks,
            route="ocr",
            page_no=page_no,
        )
        page.notes.append("OCR provider: AgileSoDA ETLwithLLM Default JSON")
        _apply_vlm_judgments(page, canvas.image)
        doc.pages.append(page)
        if preview_dir:
            _save_preview(canvas, page, preview_dir, doc.doc_id)

    if canvases:
        _classify_into(doc, canvases[0].image)
    return doc


def _load_canvases(path: Path) -> list[CanvasPage]:
    ext = path.suffix.lower()
    if ext in _IMAGE_EXTS:
        return [load_image_canvas(path)]
    if ext == ".pdf":
        try:
            pdf = pdfium.PdfDocument(path)
        except Exception as exc:
            raise ValueError(f"PDF를 열 수 없습니다: {path}: {exc}") from exc
        try:
            return [render_pdf_page(page, i + 1) for i, page in enumerate(pdf)]
        finally:
            pdf.close()
    raise ValueError(
        f"ETL DLA 결합 프로토타입은 PDF/PNG/JPG만 지원합니다: {path.suffix or '(확장자 없음)'}"
    )
