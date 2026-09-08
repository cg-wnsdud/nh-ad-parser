# -*- coding: utf-8 -*-
"""후보 설정이 다른 문서에서 무너지지 않는지 본다 (골드 없음 — 붕괴 징후만 잰다).

영역 수가 급감하면 과병합, 줄 수가 줄면 텍스트 유실이다.
"""
from __future__ import annotations
import argparse, dataclasses, json, sys, time
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
from nh_parser import config as CFG

CASES = [
    ("기준(small/겹침200)", {}),
    ("large+겹침400", {"paddlex_layout_merge_bboxes_mode": "large", "tile_overlap_px": 400}),
]

def run(path, ov, base):
    for k, v in base.items(): object.__setattr__(CFG.SETTINGS, k, v)
    for k, v in ov.items():   object.__setattr__(CFG.SETTINGS, k, v)
    from nh_parser.pipeline import process_file
    d = json.loads(process_file(path).model_dump_json())
    nr = sum(len(p["regions"]) for p in d["pages"])
    nl = sum(len(r.get("lines") or []) for p in d["pages"] for r in p["regions"])
    ch = sum(len(l["text"]) for p in d["pages"] for r in p["regions"] for l in r.get("lines") or [])
    return nr, nl, ch

ap = argparse.ArgumentParser(); ap.add_argument("--inputs", nargs="+", type=Path)
args = ap.parse_args()
base = {f.name: getattr(CFG.SETTINGS, f.name) for f in dataclasses.fields(CFG.SETTINGS)}
print(f"{'문서':30}{'설정':18}{'영역':>6}{'줄':>6}{'글자':>7}{'초':>5}")
for path in args.inputs:
    row = {}
    for name, ov in CASES:
        t = time.time()
        try: row[name] = run(path, ov, base)
        except Exception as e: print(f"{path.stem[:28]:30}{name:18} 실패 {e}"); continue
        nr, nl, ch = row[name]
        print(f"{path.stem[:28]:30}{name:18}{nr:>6}{nl:>6}{ch:>7}{time.time()-t:>5.0f}")
    if len(row) == 2:
        a, b = row["기준(small/겹침200)"], row["large+겹침400"]
        print(f"{'':30}{'차이':18}{b[0]-a[0]:>+6}{b[1]-a[1]:>+6}{b[2]-a[2]:>+7}")
