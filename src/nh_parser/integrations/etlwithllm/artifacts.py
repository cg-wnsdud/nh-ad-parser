"""ETL Default JSON에서 현재 저장소의 parse/P1/P3 산출물을 만든다."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ...review import build_evidence, build_review_input, label_regions, resolve_template
from .pipeline import process_file_from_dla


@dataclass(frozen=True)
class ArtifactPaths:
    parse: Path
    evidence: Path
    review_input: Path
    preview_dir: Path | None


def build_artifacts_from_dla(
    input_path: Path,
    dla_payload: dict,
    output_dir: Path,
    *,
    preview: bool = True,
) -> ArtifactPaths:
    """외부 API와 custom extension이 공유하는 공급자 독립 산출물 생성 함수."""
    source = Path(input_path)
    root = Path(output_dir)
    preview_dir = root / "pages" if preview else None
    if preview_dir:
        preview_dir.mkdir(parents=True, exist_ok=True)

    doc = process_file_from_dla(source, dla_payload, preview_dir=preview_dir)
    parsed = doc.model_dump(mode="json")
    resolution = resolve_template(parsed)
    labeling = label_regions(parsed, resolution)
    evidence = build_evidence(parsed, resolution, labeling)
    review_input = build_review_input(evidence)

    parse_path = root / "parse" / f"{source.stem}.json"
    evidence_path = root / "evidence" / f"{source.stem}.json"
    review_path = root / "review-input" / f"{source.stem}.json"
    _write_json(parse_path, parsed)
    _write_json(evidence_path, evidence)
    _write_json(review_path, review_input)
    return ArtifactPaths(parse_path, evidence_path, review_path, preview_dir)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    temporary.replace(path)
