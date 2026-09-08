# -*- coding: utf-8 -*-
"""PP-StructureV3 서빙이 받는 요청 파라미터를 하나씩 바꿔 보며 실제 효과를 잰다.

    uv run python tools/probe_paddlex_params.py --image <파일> --out tmp/probe.json

읽기·추론 호출만 한다. 서버 설정은 건드리지 않는다 — 요청 본문만 달리 보낸다.
같은 이미지에 기준 호출 1회 + 변형 1회씩을 보내고, 응답에서
블록 수 / OCR 줄 수 / 글자 수 / 블록(라벨+좌표) 지문을 비교한다.

지문이 기준과 같으면 그 파라미터는 **이 입력에서 아무 효과가 없었다**는 뜻이다.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path

import requests
from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")

BASE = {
    "fileType": 1,
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "textDetLimitSideLen": 2500,
    "textDetLimitType": "max",
    "layoutMergeBboxesMode": "small",
    "useFormulaRecognition": False,
    "useTextlineOrientation": False,
}

# (이름, 바꿀 것) — 기준 호출 위에 이것만 얹는다
CASES: list[tuple[str, dict]] = [
    ("기준", {}),
    # ── 전처리 ──
    ("useDocOrientationClassify=True", {"useDocOrientationClassify": True}),
    ("useDocUnwarping=True", {"useDocUnwarping": True}),
    ("useTextlineOrientation=True", {"useTextlineOrientation": True}),
    # ── 서브파이프라인 on/off ──
    ("useSealRecognition=True", {"useSealRecognition": True}),
    ("useSealRecognition=False", {"useSealRecognition": False}),
    ("useTableRecognition=False", {"useTableRecognition": False}),
    ("useFormulaRecognition=True", {"useFormulaRecognition": True}),
    ("useChartRecognition=True", {"useChartRecognition": True}),
    ("useRegionDetection=False", {"useRegionDetection": False}),
    ("useRegionDetection=True", {"useRegionDetection": True}),
    ("formatBlockContent=True", {"formatBlockContent": True}),
    # ── 레이아웃 검출 ──
    ("layoutThreshold=0.3", {"layoutThreshold": 0.3}),
    ("layoutThreshold=0.7", {"layoutThreshold": 0.7}),
    ("layoutThreshold={'2':0.3} 딕셔너리", {"layoutThreshold": {"2": 0.3}}),
    ("layoutThreshold={'0':0.3,'2':0.3}", {"layoutThreshold": {"0": 0.3, "2": 0.3}}),
    ("layoutThreshold={'999':0.3} 없는키", {"layoutThreshold": {"999": 0.3}}),
    ("layoutNms=False", {"layoutNms": False}),
    ("layoutNms=True", {"layoutNms": True}),
    ("layoutUnclipRatio=1.5", {"layoutUnclipRatio": 1.5}),
    ("layoutUnclipRatio=[1.5,1.0] 배열", {"layoutUnclipRatio": [1.5, 1.0]}),
    ("layoutUnclipRatio={'2':1.5} 딕셔너리", {"layoutUnclipRatio": {"2": 1.5}}),
    ("layoutMergeBboxesMode=large", {"layoutMergeBboxesMode": "large"}),
    ("layoutMergeBboxesMode=union", {"layoutMergeBboxesMode": "union"}),
    ("layoutMergeBboxesMode 미지정(서버기본)", {"layoutMergeBboxesMode": None}),
    ("mode={'2':'large'} 딕셔너리", {"layoutMergeBboxesMode": {"2": "large"}}),
    ("mode={'1':'large','2':'large'}", {"layoutMergeBboxesMode": {"1": "large", "2": "large"}}),
    ("mode={'999':'large'} 없는키", {"layoutMergeBboxesMode": {"999": "large"}}),
    ("mode={'2':'헛소리'} 잘못된값", {"layoutMergeBboxesMode": {"2": "nonsense"}}),
    ("mode='헛소리' 잘못된값", {"layoutMergeBboxesMode": "nonsense"}),
    # ── 텍스트 검출/인식 ──
    ("textDetLimitSideLen=960", {"textDetLimitSideLen": 960}),
    ("textDetLimitType=min", {"textDetLimitType": "min"}),
    ("textDetThresh=0.1", {"textDetThresh": 0.1}),
    ("textDetThresh=0.5", {"textDetThresh": 0.5}),
    ("textDetBoxThresh=0.3", {"textDetBoxThresh": 0.3}),
    ("textDetBoxThresh=0.8", {"textDetBoxThresh": 0.8}),
    ("textDetUnclipRatio=1.0", {"textDetUnclipRatio": 1.0}),
    ("textDetUnclipRatio=3.0", {"textDetUnclipRatio": 3.0}),
    ("textRecScoreThresh=0.5", {"textRecScoreThresh": 0.5}),
    # ── 표 ──
    ("useWiredTableCellsTransToHtml=True", {"useWiredTableCellsTransToHtml": True}),
    ("useWirelessTableCellsTransToHtml=True", {"useWirelessTableCellsTransToHtml": True}),
    ("useTableOrientationClassify=False", {"useTableOrientationClassify": False}),
    ("useOcrResultsWithTableCells=False", {"useOcrResultsWithTableCells": False}),
    ("useE2eWiredTableRecModel=True", {"useE2eWiredTableRecModel": True}),
    ("useE2eWirelessTableRecModel=False", {"useE2eWirelessTableRecModel": False}),
    # ── 출력 ──
    ("outputFormats=['markdown']", {"outputFormats": ["markdown"]}),
    ("markdownIgnoreLabels=['footer']", {"markdownIgnoreLabels": ["footer"]}),
    ("prettifyMarkdown=False", {"prettifyMarkdown": False}),
    ("showFormulaNumber=True", {"showFormulaNumber": True}),
    ("visualize=True", {"visualize": True}),
]


def encode(image: Image.Image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def digest(pruned: dict) -> dict:
    """응답에서 비교에 쓸 지문만 뽑는다."""
    blocks = pruned.get("parsing_res_list") or []
    det = (pruned.get("layout_det_res") or {}).get("boxes") or []
    ocr = pruned.get("overall_ocr_res") or {}
    texts = ocr.get("rec_texts") or []
    fp = hashlib.sha1(
        json.dumps([[b.get("block_label"), [int(v) for v in (b.get("block_bbox") or [])]]
                    for b in blocks], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:10]
    return {
        "blocks": len(blocks),
        "det_boxes": len(det),
        "lines": len(texts),
        "chars": sum(len(str(t)) for t in texts),
        "labels": sorted({str(b.get("block_label")) for b in blocks}),
        "classes": sorted({(int(b.get("cls_id")), str(b.get("label")))
                           for b in det if b.get("cls_id") is not None}),
        "fingerprint": fp,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", type=Path, required=True)
    ap.add_argument("--crop", default=None, help="x0,y0,x1,y1")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    url = os.environ["PADDLEX_URL"]

    img = Image.open(args.image).convert("RGB")
    if args.crop:
        img = img.crop(tuple(int(v) for v in args.crop.split(",")))
    file_b64 = encode(img)
    print(f"입력 {args.image.name} {img.width}x{img.height}\n")

    results = {}
    for name, extra in CASES:
        payload = {**BASE, "file": file_b64}
        for k, v in extra.items():
            if v is None:
                payload.pop(k, None)      # None = 그 키를 안 보낸다
            else:
                payload[k] = v
        started = time.time()
        try:
            resp = requests.post(url, json=payload, timeout=600)
        except Exception as exc:
            results[name] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"{name:40} 호출실패 {exc}")
            continue
        elapsed = time.time() - started
        if resp.status_code != 200:
            body = resp.text[:200].replace("\n", " ")
            results[name] = {"http": resp.status_code, "error": body, "sec": elapsed}
            print(f"{name:40} HTTP {resp.status_code}  {body[:90]}")
            continue
        body = resp.json()
        if body.get("errorCode") != 0:
            results[name] = {"errorCode": body.get("errorCode"),
                             "error": str(body.get("errorMsg"))[:200], "sec": elapsed}
            print(f"{name:40} errorCode={body.get('errorCode')} {body.get('errorMsg')}")
            continue
        page = body["result"]["layoutParsingResults"][0]
        info = digest(page.get("prunedResult") or {})
        info["sec"] = round(elapsed, 1)
        info["resp_kb"] = round(len(resp.content) / 1024)
        results[name] = info
        same = "" if name == "기준" else (
            " = 기준과 동일" if info["fingerprint"] == results["기준"]["fingerprint"] else " ★변화")
        print(f"{name:40} 블록{info['blocks']:>3} det{info['det_boxes']:>3} "
              f"줄{info['lines']:>4} 글자{info['chars']:>5} {info['sec']:>5.1f}s{same}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장 -> {args.out}")


if __name__ == "__main__":
    main()
