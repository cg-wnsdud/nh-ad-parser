# -*- coding: utf-8 -*-
"""PaddleX 가 최종으로 내놓는 것이 정확히 무엇인지 글로 펼쳐 본다.

박스 그림만 보면 "이 영역에 어떤 글자가 담겼는지"와 "어디에도 안 담긴 글자가 있는지"를
알 수 없다. 이 도구는 한 페이지를 세 덩어리로 나눠 보여준다.

    ① parsing_res_list   PaddleX 최종 블록 — 읽기순서 · 라벨 · **본문 텍스트**
    ② 내용이 빈 블록      table/seal/formula 는 block_content 가 비어 있다.
                         그 안에 들어 있는 OCR 줄을 대신 보여준다.
    ③ 미배정 OCR 줄       어느 블록에도 안 들어간 줄. 여기 있는 글자는
                         block_content 만 쓰면 **통째로 사라진다.**

사용법::

    uv run python experiments/ocr-lab/show.py --run layout-20260918-v1--table-off
    uv run python experiments/ocr-lab/show.py --run <실행> --file "25. 대출성" --out tmp/x.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")

# OCR 줄이 블록에 담겼다고 볼 최소 겹침 (줄 면적 대비). 기존 파이프라인의 규칙과 같다.
LINE_IN_BLOCK = 0.5


def overlap(line: list[int], box: list[int]) -> float:
    ix = max(0, min(line[2], box[2]) - max(line[0], box[0]))
    iy = max(0, min(line[3], box[3]) - max(line[1], box[1]))
    return ix * iy / max(1, (line[2] - line[0]) * (line[3] - line[1]))


def render_page(name: str, data: dict, max_chars: int) -> list[str]:
    blocks = data.get("parsing") or []
    lines = data.get("ocr_lines") or []
    width, height = data.get("canvas", [0, 0])

    # 각 OCR 줄을 가장 많이 겹치는 블록에 붙인다 (표시용 — PaddleX 의 배정이 아니다).
    holder: dict[int, list[dict]] = {}
    orphans: list[dict] = []
    for line in lines:
        best, ratio = -1, 0.0
        for index, block in enumerate(blocks):
            value = overlap(line["bbox"], block["bbox"])
            if value > ratio:
                best, ratio = index, value
        if best >= 0 and ratio >= LINE_IN_BLOCK:
            holder.setdefault(best, []).append(line)
        else:
            orphans.append(line)

    out = [f"\n{'=' * 78}", f"{name}   캔버스 {width}x{height}   조각 {data.get('pieces', 1)}개",
           f"{'=' * 78}",
           f"parsing 블록 {len(blocks)}개 / OCR 줄 {len(lines)}개 / 미배정 {len(orphans)}개", ""]

    ordered = sorted(
        range(len(blocks)),
        key=lambda i: (blocks[i].get("order") is None, blocks[i].get("order") or 0,
                       blocks[i]["bbox"][1], blocks[i]["bbox"][0]),
    )
    for index in ordered:
        block = blocks[index]
        order = block.get("order")
        tag = f"[{order:>3}]" if order is not None else "[  -]"
        content = (block.get("content") or "").replace("\n", " ").strip()
        held = holder.get(index, [])
        if content:
            shown = content if len(content) <= max_chars else content[:max_chars] + " …"
            out.append(f"{tag} {block['label']:<16} {block['bbox']}  {shown}")
        else:
            out.append(f"{tag} {block['label']:<16} {block['bbox']}  "
                       f"⟨block_content 없음 — 안에 OCR 줄 {len(held)}개⟩")
            for line in held[:12]:
                out.append(f"        · {line['text']}")
            if len(held) > 12:
                out.append(f"        · … 외 {len(held) - 12}줄")

    if orphans:
        out.append("")
        out.append(f"── 어느 블록에도 안 들어간 OCR 줄 {len(orphans)}개 "
                   "(block_content 만 쓰면 사라지는 글자) ──")
        for line in orphans[:40]:
            out.append(f"    · {line['bbox']}  {line['text']}")
        if len(orphans) > 40:
            out.append(f"    · … 외 {len(orphans) - 40}줄")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="PaddleX 최종 결과를 글로 펼쳐 본다")
    parser.add_argument("--run", required=True, help="runs/ 아래 실행 이름")
    parser.add_argument("--file", default="", help="파일명 일부로 걸러내기")
    parser.add_argument("--max-chars", type=int, default=90)
    parser.add_argument("--out", type=Path, default=None, help="저장할 경로(.md/.txt)")
    args = parser.parse_args()

    directory = ROOT / "runs" / args.run / "boxes"
    if not directory.is_dir():
        raise SystemExit(f"{directory} 가 없습니다")

    lines: list[str] = []
    hit = 0
    for path in sorted(directory.glob("*.json")):
        if args.file and args.file not in path.stem:
            continue
        hit += 1
        lines += render_page(path.stem, json.loads(path.read_text(encoding="utf-8")),
                             args.max_chars)
    if not hit:
        raise SystemExit(f"'{args.file}' 에 맞는 페이지가 없습니다")

    text = "\n".join(lines)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"{hit}쪽 저장 -> {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
