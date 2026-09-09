"""KL 광고 API 작업을 현재 ``nh_parser`` 파이프라인에 연결한다.

이 모듈은 OCR/VLM 모델을 직접 실행하지 않는다. ``process_file()``을 같은 Python
프로세스에서 호출하고, 현재 파이프라인이 설정된 PaddleX/Gemma 서비스에 HTTP 요청한다.
농협 ETLwithLLM을 호출하는 코드는 호출 경계가 확정될 때까지 포함하지 않는다.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

from ..review import build_evidence, build_review_input, label_regions, resolve_template
from .status import DONE, ERROR, PARSING, write_status


PARSER_BACKEND = "dgx-paddlex-gemma"


def run_ad_job(
    work_dir: str | Path,
    image_dir: str | Path,
    file_path: str | Path,
    option: dict[str, Any] | None = None,
) -> None:
    """광고 파일 하나를 처리하고 상태 및 반환 대상 파일을 기록한다."""
    work = Path(work_dir)
    images = Path(image_dir)
    source = Path(file_path)
    try:
        write_status(work, PARSING)
        document = _process_current_pipeline(source, images)
        _write_ad_outputs(work, source, document, option=option)
        write_status(work, DONE)
    except Exception as exc:  # 백그라운드 작업 실패를 폴링 응답으로 전달한다.
        traceback.print_exc()
        write_status(work, ERROR, f"{exc.__class__.__name__}: {exc}")


def _process_current_pipeline(source: Path, image_dir: Path):
    """현재 DGX-Spark PaddleX/Gemma 기준선을 실행한다."""
    from ..pipeline import process_file

    image_dir.mkdir(parents=True, exist_ok=True)
    return process_file(source, preview_dir=image_dir)


def _write_ad_outputs(
    work: Path,
    source: Path,
    document: Any,
    *,
    option: dict[str, Any] | None,
) -> None:
    """현재 P1/P3를 기존 보고의 파일명으로 내보낸다."""
    parsed = document.model_dump(mode="json")
    resolution = resolve_template(parsed)
    labeling = label_regions(parsed, resolution)
    evidence = build_evidence(parsed, resolution, labeling)
    review_input = build_review_input(evidence)

    _write_json(work / f"{source.name}_parsed.json", evidence)
    _write_json(work / f"{source.name}_review_input.json", review_input)
    _write_json(
        work / "ad_summary.json",
        {
            "doc_id": evidence.get("doc_id"),
            "file_type": evidence.get("file_type"),
            "classification": evidence.get("classification"),
            "template": {
                key: (evidence.get("template") or {}).get(key)
                for key in ("template_id", "status", "source", "confidence", "reason")
            },
            "page_count": len(evidence.get("pages") or []),
            "region_count": (evidence.get("summary") or {}).get("region_count"),
            "line_count": (evidence.get("summary") or {}).get("line_count"),
            "review_input_summary": review_input.get("summary"),
            "parser_backend": PARSER_BACKEND,
            "request_option": option,
            "notes": evidence.get("notes") or [],
        },
    )


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    temporary.replace(path)
