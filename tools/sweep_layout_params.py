# -*- coding: utf-8 -*-
"""레이아웃 파라미터를 바꿔가며 '골드 덩어리가 한 영역에 담기는가'를 잰다.

지표는 골드 텍스트가 파싱 영역 하나 안에 통째로 들어갔는지만 본다. VLM 라벨링을
거치지 않으므로 결정론적이고, 같은 설정이면 같은 값이 나온다.

    uv run python tools/sweep_layout_params.py --gold <gold.yaml> --input <이미지>
"""
from __future__ import annotations

import argparse, dataclasses, json, re, sys, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
import yaml
from nh_parser import config as CFG
from nh_parser.truncation import norm   # 평가기와 같은 의미 정규화 (①→1, 「」 제거 등)

_WS = re.compile(r"\s+")
fold = lambda s: _WS.sub("", norm(str(s or "")))


def not_merged(doc: dict, gold: dict) -> tuple[int, int, list[str]]:
    """붙으면 안 되는 두 덩어리가 실제로 다른 영역에 있는가 (과병합 가드)."""
    texts = [fold("".join(l["text"] for l in r.get("lines") or []))
             for p in doc["pages"] for r in p["regions"]]
    by_id = {b["gold_block_id"]: fold(b["text"]) for b in gold["pages"][0]["text_blocks"]}
    ok, total, bad = 0, 0, []
    for rule in gold["pages"][0].get("must_not_merge") or []:
        left, right = (rule if isinstance(rule, list) else rule.get("blocks"))[:2]
        a, b = by_id.get(left), by_id.get(right)
        if not (a and b):
            continue
        total += 1
        if any(a in t and b in t for t in texts):
            bad.append(f"{left}+{right}")
        else:
            ok += 1
    return ok, total, bad


def score(doc: dict, gold: dict) -> tuple[int, int, list[str]]:
    """(한 영역에 담긴 골드 블록 수, 전체, 실패한 블록 id)"""
    blocks = gold["pages"][0]["text_blocks"]
    texts = []
    for page in doc["pages"]:
        for region in page["regions"]:
            texts.append(fold("".join(l["text"] for l in region.get("lines") or [])))
    ok, bad = 0, []
    for b in blocks:
        needle = fold(b["text"])
        if needle and any(needle in t for t in texts):
            ok += 1
        else:
            bad.append(b["gold_block_id"])
    return ok, len(blocks), bad


def run(path: Path, overrides: dict) -> dict:
    for k, v in overrides.items():
        object.__setattr__(CFG.SETTINGS, k, v)      # frozen dataclass 우회 (실험용)
    from nh_parser.pipeline import process_file
    return json.loads(process_file(path).model_dump_json())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", type=Path, required=True)
    ap.add_argument("--input", type=Path, required=True)
    args = ap.parse_args()
    gold = yaml.safe_load(args.gold.read_text(encoding="utf-8"))
    base = {f.name: getattr(CFG.SETTINGS, f.name) for f in dataclasses.fields(CFG.SETTINGS)}

    CASES = [
        ("기준(현행)",                     {}),
        ("merge=large",                   {"paddlex_layout_merge_bboxes_mode": "large"}),
        ("merge=union",                   {"paddlex_layout_merge_bboxes_mode": "union"}),
        ("타일 겹침 400",                  {"tile_overlap_px": 400}),
        ("large + 겹침 400",               {"paddlex_layout_merge_bboxes_mode": "large",
                                           "tile_overlap_px": 400}),
    ]
    print(f"{'설정':24}{'한영역담김':>10}{'미분리':>8}{'영역수':>7}{'초':>5}   놓친 블록 / 과병합")
    for name, ov in CASES:
        for k, v in base.items():
            object.__setattr__(CFG.SETTINGS, k, v)   # 매번 기준값으로 되돌린다
        t = time.time()
        try:
            doc = run(args.input, ov)
        except Exception as exc:
            print(f"{name:24}  실패 {type(exc).__name__}: {exc}"); continue
        ok, total, bad = score(doc, gold)
        mok, mtot, mbad = not_merged(doc, gold)
        nr = sum(len(p["regions"]) for p in doc["pages"])
        nl = sum(len(r.get("lines") or []) for p in doc["pages"] for r in p["regions"])
        print(f"{name:24}{ok:>7}/{total:<3}{mok:>5}/{mtot:<3}{nr:>7}{time.time()-t:>5.0f}   "
              f"{' '.join(bad)}{'  ⚠과병합 '+' '.join(mbad) if mbad else ''}")


if __name__ == "__main__":
    main()
