# -*- coding: utf-8 -*-
"""농협 광고 템플릿 HWPX의 표 셀을 런타임 JSON 카탈로그로 변환한다.

    uv run python tools/build_template_catalog.py \
      --input "nh-data/template/광고 템플릿(근거규정x, 필수 여부).hwpx"

``document-processor``는 이 빌드 도구에만 필요하다. 생성된 JSON은 패키지에 포함되므로
일반 광고 파싱 환경에서는 HWPX 원본이나 사내 패키지가 없어도 된다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "nh-data" / "template" / "광고 템플릿(근거규정x, 필수 여부).hwpx"
DEFAULT_OUTPUT = ROOT / "src" / "nh_parser" / "templates" / "ad_templates.json"
TITLE_RE = re.compile(r"^\[(.+)]$")

sys.stdout.reconfigure(encoding="utf-8")


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def _product_group(template_id: str) -> str:
    prefix = template_id.split("상품-", 1)[0]
    if prefix not in {"대출성", "예금성", "카드", "투자성"}:
        raise ValueError(f"템플릿명에서 상품군을 알 수 없습니다: {template_id!r}")
    return prefix


def _entry_from_row(row: list[object]) -> tuple[str, dict[str, str]] | None:
    cells = [_text(getattr(cell, "text", "")) for cell in row]
    if not cells or cells[0].replace(" ", "") == "구분":
        return None
    gubun = cells[0]
    if not gubun:
        return None
    required_index = next(
        (index for index, value in enumerate(cells[1:], start=1) if value in {"O", "△"}),
        None,
    )
    if required_index is None:
        raise ValueError(f"필수여부 O/△를 찾을 수 없는 행: {cells!r}")
    examples = list(dict.fromkeys(value for value in cells[1:required_index] if value))
    instructions = " / ".join(value for value in cells[required_index + 1:] if value)
    return gubun, {
        "example": "\n".join(examples),
        "required": cells[required_index],
        "instructions": instructions,
    }


def build_catalog(source: Path) -> dict[str, Any]:
    try:
        from document_processor import DocIR
    except ImportError as exc:
        raise SystemExit(
            "document-processor가 필요합니다. README의 HWP 설치 안내를 확인하세요."
        ) from exc

    doc = DocIR.from_file(str(source))
    templates: OrderedDict[str, dict[str, Any]] = OrderedDict()
    pending_title: str | None = None
    for paragraph in doc.paragraphs:
        paragraph_text = _text(getattr(paragraph, "text", ""))
        match = TITLE_RE.fullmatch(paragraph_text)
        if match:
            pending_title = match.group(1)
        for node in getattr(paragraph, "content", None) or []:
            if type(node).__name__ != "TableIR":
                continue
            if not pending_title:
                raise ValueError("제목 앞에 나타난 템플릿 표가 있습니다")
            grouped: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
            for row in getattr(node, "cells", None) or []:
                parsed = _entry_from_row(row)
                if parsed is None:
                    continue
                gubun, entry = parsed
                grouped.setdefault(gubun, []).append(entry)
            templates[pending_title] = {
                "product_group": _product_group(pending_title),
                "items": [
                    {"gubun": gubun, "entries": entries}
                    for gubun, entries in grouped.items()
                ],
            }
            pending_title = None

    if len(templates) != 19:
        raise ValueError(f"광고 템플릿은 19개여야 합니다: 실제 {len(templates)}개")
    return {
        "version": "nh-ad-template-catalog-v1",
        "source": {
            "file_name": source.name,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
        "templates": templates,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    catalog = build_catalog(args.input.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(f"템플릿 {len(catalog['templates'])}개 → {args.output}")


if __name__ == "__main__":
    main()
