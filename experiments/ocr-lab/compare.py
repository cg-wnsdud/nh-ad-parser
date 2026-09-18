# -*- coding: utf-8 -*-
"""두 실행을 **영역 커버리지**로 대조한다 — 박스 개수 대신 쓰는 지표.

**왜 개수를 쓰면 안 되나.** 2026-09-17 에 `4. 카드상품` 을 박스 개수(56 vs 37)로
판정했다가 틀렸다. 실제로는 같은 영역을 더 잘게 쪼갠 것(과분할)이 대부분이었고,
진짜 누락은 1곳뿐이었다. **개수가 많다고 더 많이 잡은 것이 아니다.**

**이 지표의 정의.**

    A 의 박스 하나를 놓고, B 의 박스들을 **전부 합친 것**이 그 면적의 몇 %를 덮는지 잰다.
    50% 미만이면 "B 가 놓친 영역" 으로 센다. 반대 방향도 같은 방법으로 잰다.

**합집합으로 재는 이유.** 한쪽이 큰 덩어리 하나로 잡고 다른 쪽이 낱줄 여럿으로 나눠
잡았을 때, "가장 잘 덮는 단일 박스"로 재면 후자를 누락으로 센다. 실제로 그렇게 세서
누락 3건이라는 틀린 값이 나왔고, 합집합으로 다시 재니 1건이었다.

**이 지표가 재지 못하는 것** (반드시 눈으로 확인해야 하는 것):

    · 경계가 맞는가        — 글자를 반쯤 자른 박스도 "덮었다"로 센다
    · 과병합              — 서로 다른 상품을 한 박스에 묶어도 커버리지는 만점이다
    · 라벨이 맞는가        — 라벨을 보지 않는다
    · 읽기 순서

즉 이것은 **"무엇을 놓쳤나"만 재는 지표**다. 품질 판정은 정답 박스가 있어야 한다.

사용법::

    uv run python experiments/ocr-lab/compare.py --a asis --b tiled
    uv run python experiments/ocr-lab/compare.py --a tiled --b span1600 --layer parsing
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COVER_THRESHOLD = 0.5
MAX_SAMPLES = 40_000


def union_coverage(box: list[int], others: list[list[int]]) -> float:
    """others 를 전부 합친 것이 box 면적의 몇 %를 덮는가 (격자 표본)."""
    x0, y0, x1, y1 = box
    width, height = max(1, x1 - x0), max(1, y1 - y0)
    step = max(2, min(width, height) // 15)
    while (width // step + 1) * (height // step + 1) > MAX_SAMPLES:
        step *= 2
    near = [
        b for b in others
        if b[0] < x1 and b[2] > x0 and b[1] < y1 and b[3] > y0
    ]
    if not near:
        return 0.0
    hit = total = 0
    for x in range(x0, x1, step):
        for y in range(y0, y1, step):
            total += 1
            for b in near:
                if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                    hit += 1
                    break
    return hit / max(1, total)


def load(run: str, layer: str) -> dict[str, dict]:
    directory = ROOT / "runs" / run / "boxes"
    if not directory.is_dir():
        raise SystemExit(
            f"{directory} 가 없습니다 — 페이지 좌표 박스는 boxes/ 추가 이후 실행부터 생깁니다"
        )
    pages: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        pages[path.stem] = {
            "canvas": data["canvas"],
            "pieces": data.get("pieces", 1),
            "boxes": [b["bbox"] for b in data.get(layer) or []],
        }
    return pages


def main() -> None:
    parser = argparse.ArgumentParser(description="두 실행의 영역 커버리지 대조")
    parser.add_argument("--a", required=True, help="기준 실행 이름")
    parser.add_argument("--b", required=True, help="비교 실행 이름")
    parser.add_argument("--layer", choices=("det", "parsing"), default="det")
    parser.add_argument("--list", type=int, default=3, help="놓친 영역을 몇 개까지 출력할지")
    args = parser.parse_args()

    a_pages = load(args.a, args.layer)
    b_pages = load(args.b, args.layer)
    shared = sorted(set(a_pages) & set(b_pages))
    if not shared:
        raise SystemExit("두 실행에 공통 페이지가 없습니다")

    print(f"지표: 상대 박스들의 합집합이 면적의 {COVER_THRESHOLD:.0%} 미만만 덮으면 '놓침'")
    print(f"레이어: {args.layer}   A={args.a}   B={args.b}\n")
    header = f"{'페이지':34} {'A박스':>5} {'B박스':>5} {'B가놓침':>7} {'A가놓침':>7}"
    print(header)
    print("-" * len(header))

    totals = [0, 0, 0, 0]
    for key in shared:
        a, b = a_pages[key], b_pages[key]
        if a["canvas"] != b["canvas"]:
            print(f"{key[:32]:34} 캔버스가 달라 비교 불가 {a['canvas']} vs {b['canvas']}")
            continue
        miss_b = [x for x in a["boxes"] if union_coverage(x, b["boxes"]) < COVER_THRESHOLD]
        miss_a = [x for x in b["boxes"] if union_coverage(x, a["boxes"]) < COVER_THRESHOLD]
        print(f"{key[:32]:34} {len(a['boxes']):>5} {len(b['boxes']):>5} "
              f"{len(miss_b):>7} {len(miss_a):>7}")
        for box in miss_b[: args.list]:
            print(f"      B가 놓침 {box}  {box[2]-box[0]}x{box[3]-box[1]}")
        for box in miss_a[: args.list]:
            print(f"      A가 놓침 {box}  {box[2]-box[0]}x{box[3]-box[1]}")
        totals[0] += len(a["boxes"])
        totals[1] += len(b["boxes"])
        totals[2] += len(miss_b)
        totals[3] += len(miss_a)

    print("-" * len(header))
    print(f"{'합계':34} {totals[0]:>5} {totals[1]:>5} {totals[2]:>7} {totals[3]:>7}")
    print(f"\nB({args.b})가 {totals[2]}곳 놓치고 {totals[3]}곳 더 잡았다 "
          f"(순증 {totals[3] - totals[2]:+d}곳)")
    print("주의: 이 지표는 경계 정확도, 과병합, 라벨, 읽기순서를 재지 못한다.")


if __name__ == "__main__":
    main()
