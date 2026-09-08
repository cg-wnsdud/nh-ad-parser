# -*- coding: utf-8 -*-
"""저장된 parse JSON에서 템플릿 라벨·P1·P3만 다시 생성한다.

OCR·영역 Reader/Judge를 재호출하지 않고 템플릿 선택과 구분값 판정만 반복할 때 쓴다.

    uv run python tools/build_review.py \
      --input out/2026-09-08/p1-p3-card-pdf/parse \
      --out out/2026-09-08/p1-p3-card-pdf
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


sys.stdout.reconfigure(encoding="utf-8")


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    temporary.replace(path)


def build_one(path: Path, out_dir: Path) -> tuple[Path, Path, dict]:
    from nh_parser.review import build_evidence, build_review_input, label_regions, resolve_template

    doc = json.loads(path.read_text(encoding="utf-8"))
    resolution = resolve_template(doc)
    labeling = label_regions(doc, resolution)
    evidence = build_evidence(doc, resolution, labeling)
    review = build_review_input(evidence)
    evidence_path = out_dir / "evidence" / path.name
    review_path = out_dir / "review-input" / path.name
    _write_json(evidence_path, evidence)
    _write_json(review_path, review)
    return evidence_path, review_path, review


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True, help="parse JSON 파일 또는 폴더")
    parser.add_argument("--out", type=Path, required=True, help="실행 결과 루트")
    args = parser.parse_args()
    targets = sorted(args.input.glob("*.json")) if args.input.is_dir() else [args.input]
    if not targets:
        raise SystemExit(f"parse JSON이 없습니다: {args.input}")
    failed = 0
    for target in targets:
        try:
            evidence_path, review_path, review = build_one(target, args.out)
        except Exception as exc:  # 한 파일 실패가 나머지를 막지 않는다
            failed += 1
            print(f"실패 {target.name}: {type(exc).__name__}: {exc}")
            continue
        template = review["document"]["template"]
        summary = review["summary"]
        print(
            f"{target.name}: {template.get('template_id') or '판단불가'} "
            f"/ 라벨 {summary['label_count']} / 검수영역 {summary['needs_review_region_count']}"
        )
        print(f"  P1 → {evidence_path}")
        print(f"  P3 → {review_path}")
    print(f"{len(targets) - failed}/{len(targets)} 건 성공")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
