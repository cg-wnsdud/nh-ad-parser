"""저장된 ocr-lab boxes 결과로 parser-v2 Region 조립만 다시 검증한다.

SSH 터널이나 GPU 호출 없이 텍스트 후보 선택·줄 소유권 로직을 반복 시험할 때 쓴다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from adapters import build_page_evidence  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="ocr-lab 저장 결과 → parser-v2 재조립")
    parser.add_argument("--lab-run", required=True)
    parser.add_argument("--run-name", required=True)
    args = parser.parse_args()

    root = HERE.parent / "ocr-lab" / "runs" / args.lab_run / "boxes"
    if not root.is_dir():
        raise SystemExit(f"ocr-lab boxes가 없습니다: {root}")
    out = HERE / "outputs" / args.run_name
    totals: Counter[str] = Counter()
    pages = []
    for path in sorted(root.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        found = re.search(r"__p(\d+)$", path.stem)
        page_no = int(found.group(1)) if found else 1
        page = build_page_evidence(
            raw.get("parsing") or [],
            raw.get("ocr_lines") or [],
            page_no=page_no,
            canvas=raw.get("canvas") or [0, 0],
        )
        page["source_boxes"] = path.name
        pages.append(page)
        totals.update(page["diagnostics"])

    out.mkdir(parents=True, exist_ok=True)
    (out / "replayed-pages.json").write_text(
        json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        "source_lab_run": args.lab_run,
        "pages": len(pages),
        "totals": dict(totals),
    }
    (out / "replay-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
