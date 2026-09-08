# -*- coding: utf-8 -*-
"""광고물 하나(또는 폴더)를 파싱해 JSON 으로 떨군다.

    uv run python tools/parse.py --input <파일 또는 폴더> --out out/

산출:
    <out>/parse/<파일명>.json         OCR·좌표·VLM 후보 원본
    <out>/evidence/<파일명>.json      P1 전체 근거 + 템플릿/구분/Judge
    <out>/review-input/<파일명>.json  P3 영역 중심 다음 심의 단계 입력
    <out>/pages/<파일명>_p1.jpg       쪽 이미지 축소본 (--preview 를 준 경우)

PaddleX·Gemma 엔드포인트가 있어야 한다(.env 참고). 사내 VPN 이 필요하다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".hwp", ".hwpx"}


def _write_json(path: Path, value: dict) -> None:
    """중단 시 반쪽 JSON이 남지 않도록 같은 폴더에서 원자적으로 교체한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    temporary.replace(path)


def parse_one(
    path: Path, out_dir: Path, preview: bool, *, build_review: bool = True,
) -> tuple[dict, bool]:
    # main()이 REGION_READING_MODE를 정한 뒤 import해야 Settings에 반영된다.
    from nh_parser.pipeline import process_file

    started = time.time()
    preview_dir = (out_dir / "pages") if preview else None
    if preview_dir:
        preview_dir.mkdir(parents=True, exist_ok=True)

    doc = process_file(path, preview_dir=preview_dir)
    payload = json.loads(doc.model_dump_json())

    dest = out_dir / "parse" / f"{path.stem}.json"
    _write_json(dest, payload)

    evidence_dest = review_dest = None
    review_payload = None
    if build_review:
        from nh_parser.review import (
            build_evidence, build_review_input, label_regions, resolve_template,
        )

        resolution = resolve_template(payload)
        labeling = label_regions(payload, resolution)
        evidence = build_evidence(payload, resolution, labeling)
        review_payload = build_review_input(evidence)
        evidence_dest = out_dir / "evidence" / f"{path.stem}.json"
        review_dest = out_dir / "review-input" / f"{path.stem}.json"
        _write_json(evidence_dest, evidence)
        _write_json(review_dest, review_payload)

    pages = payload.get("pages") or []
    regions = sum(len(p.get("regions") or []) for p in pages)
    lines = sum(len(r.get("lines") or []) for p in pages for r in (p.get("regions") or []))
    stray = sum(len(p.get("unassigned_lines") or []) for p in pages)
    statuses = [str(p.get("parse_status") or "") for p in pages]
    readable = bool(pages) and any(
        status != "unreadable"
        and (
            any(region.get("lines") for region in page.get("regions") or [])
            or page.get("unassigned_lines")
        )
        for page, status in zip(pages, statuses)
    )
    print(
        f"  쪽 {len(pages)} / 영역 {regions} / 줄 {lines}"
        f"{f' / 미배정줄 {stray}' if stray else ''}"
        f" / {time.time() - started:.0f}초  → {dest}"
    )
    if review_payload is not None:
        template = review_payload["document"]["template"]
        summary = review_payload["summary"]
        print(
            f"  템플릿 {template.get('template_id') or '판단불가'} "
            f"({template.get('status')}) / 라벨 {summary['label_count']} / "
            f"검수영역 {summary['needs_review_region_count']}"
        )
        print(f"  P1 → {evidence_dest}")
        print(f"  P3 → {review_dest}")
    if any(status != "ok" for status in statuses):
        print(f"  경고 — parse_status={','.join(statuses)}")
    return payload, readable


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True, help="파일 또는 폴더")
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--only", default=None, help="폴더일 때 파일명 부분 일치 필터")
    ap.add_argument("--preview", action="store_true", help="쪽 이미지 축소본도 저장")
    ap.add_argument(
        "--parse-only", action="store_true",
        help="기존 parse JSON만 만들고 템플릿 판정/P1/P3 생성을 생략",
    )
    ap.add_argument(
        "--region-reading", choices=("off", "shadow"), default=None,
        help="영역 Reader/Judge 실행(기본: 전체 출력은 shadow, --parse-only는 off)",
    )
    args = ap.parse_args()

    reading_mode = args.region_reading or ("off" if args.parse_only else "shadow")
    os.environ["REGION_READING_MODE"] = reading_mode

    if args.input.is_dir():
        targets = sorted(p for p in args.input.iterdir() if p.suffix.lower() in EXTS)
        if args.only:
            targets = [p for p in targets if args.only in p.name]
    else:
        targets = [args.input]
    if not targets:
        sys.exit(f"처리할 파일이 없습니다: {args.input}")

    failed = 0
    for path in targets:
        print(f"▶ {path.name}")
        try:
            _, readable = parse_one(
                path, args.out, args.preview, build_review=not args.parse_only,
            )
            if not readable:
                failed += 1
                print("  실패 — 읽을 수 있는 파서 정본 줄이 없습니다")
        except Exception as exc:            # 한 건 실패가 나머지를 막지 않게 한다
            failed += 1
            print(f"  실패 — {type(exc).__name__}: {exc}")

    print(f"\n{len(targets) - failed}/{len(targets)} 건 성공")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
