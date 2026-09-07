# -*- coding: utf-8 -*-
"""광고물 하나(또는 폴더)를 파싱해 JSON 으로 떨군다.

    uv run python tools/parse.py --input <파일 또는 폴더> --out out/

산출:
    <out>/parse/<파일명>.json    글자·좌표·영역·표. 이 저장소의 최종 산출물이다.
    <out>/pages/<파일명>_p1.jpg  쪽 이미지 축소본 (--preview 를 준 경우)

PaddleX·Gemma 엔드포인트가 있어야 한다(.env 참고). 사내 VPN 이 필요하다.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from nh_parser.pipeline import process_file  # noqa: E402

EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".hwp", ".hwpx"}


def parse_one(path: Path, out_dir: Path, preview: bool) -> dict:
    started = time.time()
    preview_dir = (out_dir / "pages") if preview else None
    if preview_dir:
        preview_dir.mkdir(parents=True, exist_ok=True)

    doc = process_file(path, preview_dir=preview_dir)
    payload = json.loads(doc.model_dump_json())

    dest = out_dir / "parse" / f"{path.stem}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    pages = payload.get("pages") or []
    regions = sum(len(p.get("regions") or []) for p in pages)
    lines = sum(len(r.get("lines") or []) for p in pages for r in (p.get("regions") or []))
    stray = sum(len(p.get("unassigned_lines") or []) for p in pages)
    print(
        f"  쪽 {len(pages)} / 영역 {regions} / 줄 {lines}"
        f"{f' / 미배정줄 {stray}' if stray else ''}"
        f" / {time.time() - started:.0f}초  → {dest}"
    )
    return payload


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True, help="파일 또는 폴더")
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--only", default=None, help="폴더일 때 파일명 부분 일치 필터")
    ap.add_argument("--preview", action="store_true", help="쪽 이미지 축소본도 저장")
    args = ap.parse_args()

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
            parse_one(path, args.out, args.preview)
        except Exception as exc:            # 한 건 실패가 나머지를 막지 않게 한다
            failed += 1
            print(f"  실패 — {type(exc).__name__}: {exc}")

    print(f"\n{len(targets) - failed}/{len(targets)} 건 성공")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
