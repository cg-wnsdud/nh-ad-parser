# -*- coding: utf-8 -*-
"""영역 분할 설정을 눈으로 대조한다 — 같은 문서를 여러 설정으로 파싱해 HTML로 겹쳐 본다.

    uv run python tools/compare_regions.py --inputs samples/*.pdf --out out/영역비교

설정마다 parse JSON 을 남기고, 문서마다 HTML 한 장을 만든다. HTML 에는 원본 쪽
이미지 위에 영역 박스를 올려 설정들을 나란히 놓고, 후보 설정에서 **합쳐진 자리**를
따로 뽑아 준다.

서버 설정은 건드리지 않는다. 레이아웃 옵션 세 개(merge/threshold/unclip)는 모두
config 의 `"server"` 센티널 규약을 쓴다 — 그 값이면 요청에서 키를 빼 서버
PP-StructureV3.yml 기본값을 그대로 쓴다. 바뀌는 것은 우리 POST 본문뿐이다.

**케이스는 baseline 에서 한 축만 바꾼다**(8번만 상호작용). 이전 4케이스는 후보 A 가
merge 와 타일 겹침을 동시에 바꿔 둘 중 무엇의 효과인지 분리할 수 없었다. 겹침 축을
뺀 이유는 실측이다 — 캔버스 높이 4000px 이하면 단일 타일이라 겹침이 쓰일 자리가 없고
(tiling.make_tiles), 2026-09-08 결과의 단일 타일 문서 3건에서 실제로 `small400` 이
`baseline` 과 정확히 같았다. 새 샘플은 11쪽 중 10쪽이 단일 타일이다.

`server`(키 생략) 케이스도 뺐다. 프로브 fingerprint 가 `large` 와 완전히 같고
(6ecbfbab5f), 2026-09-08 단일 타일 문서 3건에서도 영역 수가 일치했다 — 서버 기본값이
`large` 라는 뜻이므로 따로 돌리면 같은 계산을 두 번 한다.

**모든 케이스는 VLM을 호출하지 않는다.** `process_file(use_vlm=False)`가 실제 입력 적재·렌더링·
타일링·PaddleX·좌표 복원·중복 제거·영역 조립과 결정론 후처리는 유지하고 카드 분할·누락문구
sweep·문서 분류·Reader/Judge만 건너뛴다. 따라서 케이스 간 차이는 PaddleX 설정과 결정론 client
처리에서만 생긴다.
"""
from __future__ import annotations

import argparse
import dataclasses
import html
import json
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("REGION_READING_MODE", "off")   # 대조에는 Reader/Judge 불필요

from PIL import Image                                  # noqa: E402
import pypdfium2 as pdfium                             # noqa: E402

from nh_parser import config as CFG                    # noqa: E402

# (키, 화면표기, 설정 오버라이드) — baseline 에서 **한 축만** 바꾼다
#
# 미지정(=서버 기본)을 그대로 두는 항목은 오버라이드에 넣지 않는다. 문서화된 기본값은
# threshold 0.5 / unclip 1.0 인데 서버 응답이 이 둘을 돌려주지 않아 실값은 미확인이다 —
# 그래서 "현행"을 0.5/1.0 으로 적지 않고 미지정으로 남긴다(config 주석 참조).
CASES = [
    ("baseline",    "현행 small · 나머지 미지정", {}),
    ("large",       "merge=large (=서버기본)",
     {"paddlex_layout_merge_bboxes_mode": "large"}),
    ("union",       "merge=union",
     {"paddlex_layout_merge_bboxes_mode": "union"}),
    ("thr03",       "threshold=0.3 (후보 늘림)",
     {"paddlex_layout_threshold": 0.3}),
    ("thr07",       "threshold=0.7 (후보 줄임)",
     {"paddlex_layout_threshold": 0.7}),
    ("unclip08",    "unclip=0.8 (박스 축소)",
     {"paddlex_layout_unclip_ratio": 0.8}),
    ("unclip15",    "unclip=1.5 (박스 확장)",
     {"paddlex_layout_unclip_ratio": 1.5}),
    ("large_thr03", "large + threshold=0.3 (상호작용)",
     {"paddlex_layout_merge_bboxes_mode": "large", "paddlex_layout_threshold": 0.3}),
]
COLORS = {"baseline": "#d92b2b", "large": "#1a73e8", "union": "#0f9d58",
          "thr03": "#8e24aa", "thr07": "#e8710a", "unclip08": "#00838f",
          "unclip15": "#c2185b", "large_thr03": "#5d4037"}
IMG_EXTS = {".png", ".jpg", ".jpeg"}


# ── 실제로 나간 요청 본문 잡아두기 ─────────────────────────────────────────
def _payload_spy(sink: dict):
    """PaddleX 로 처음 나가는 POST 본문을 그대로 기록한다 (run_manifest 근거).

    설정값을 보고 payload 를 다시 조립하면 paddlex.py 와 로직이 갈라질 수 있다. 실제로
    나간 본문을 잡는 편이 "이 결과가 어떤 요청에서 나왔나"의 근거로 정확하다.
    """
    import requests

    original = requests.post

    def spy(url, **kwargs):
        body = kwargs.get("json")
        if not sink and body and url == CFG.SETTINGS.paddlex_url:
            sink.update({k: ("<base64 생략>" if k == "file" else v)
                         for k, v in body.items()})
        return original(url, **kwargs)

    requests.post = spy
    return original


# ── 파싱 ────────────────────────────────────────────────────────────────
def run_case(path: Path, overrides: dict, base: dict, *,
             payload_sink: dict | None = None) -> dict:
    for key, value in base.items():
        object.__setattr__(CFG.SETTINGS, key, value)   # 매번 기준값으로 되돌린다
    for key, value in overrides.items():
        object.__setattr__(CFG.SETTINGS, key, value)   # frozen dataclass 우회 (실험용)
    from nh_parser.pipeline import process_file

    import requests
    original_post = _payload_spy(payload_sink) if payload_sink is not None else None
    try:
        return json.loads(process_file(path, use_vlm=False).model_dump_json())
    finally:
        if original_post is not None:
            requests.post = original_post


# ── 쪽 이미지 ───────────────────────────────────────────────────────────
def page_images(path: Path, sizes: list[tuple[int, int]]) -> list[Image.Image]:
    """parse JSON 의 캔버스 크기에 정확히 맞춘 쪽 이미지. 박스 좌표와 어긋나지 않게."""
    if path.suffix.lower() in IMG_EXTS:
        raw = [Image.open(path).convert("RGB")]
    else:
        pdf = pdfium.PdfDocument(str(path))
        raw = [pdf[i].render(scale=200 / 72.0).to_pil().convert("RGB")
               for i in range(len(pdf))]
    out = []
    for index, (width, height) in enumerate(sizes):
        img = raw[index] if index < len(raw) else Image.new("RGB", (width, height), "white")
        if img.size != (width, height):
            img = img.resize((width, height))
        out.append(img)
    return out


# ── 합쳐진 자리 찾기 ─────────────────────────────────────────────────────
def _covered(inner: list[int], outer: list[int]) -> float:
    ix = max(0, min(inner[2], outer[2]) - max(inner[0], outer[0]))
    iy = max(0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    area = max(1, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return ix * iy / area


def merge_groups(a_page: dict, b_page: dict, min_cover: float = 0.7) -> list[dict]:
    """B 의 영역 하나가 A 의 영역 둘 이상을 삼킨 자리. (A=기준, B=대조)"""
    groups = []
    for b in b_page.get("regions") or []:
        if not b.get("bbox"):
            continue
        inside = [a for a in a_page.get("regions") or []
                  if a.get("bbox") and _covered(a["bbox"], b["bbox"]) >= min_cover]
        if len(inside) >= 2:
            groups.append({"target": b, "sources": inside})
    return groups


def region_text(region: dict) -> str:
    return " ".join((line.get("text") or "") for line in region.get("lines") or []).strip()


# ── HTML ────────────────────────────────────────────────────────────────
CSS = """
body{font:13px/1.6 'Malgun Gothic',sans-serif;margin:0;background:#f6f7f9;color:#1a1a1a}
header{position:sticky;top:0;z-index:20;background:#fff;border-bottom:1px solid #ddd;padding:10px 16px}
h1{font-size:16px;margin:0 0 6px}
.sum{font-size:12px;color:#555}
.cols{display:flex;gap:12px;padding:12px;align-items:flex-start}
.col{flex:1;min-width:0;background:#fff;border:1px solid #ddd;border-radius:6px;overflow:hidden}
.col h2{font-size:13px;margin:0;padding:8px 10px;border-bottom:1px solid #eee;background:#fafafa}
.col h2 .n{float:right;color:#666;font-weight:400}
.canvas{position:relative;line-height:0}
.canvas img{width:100%;height:auto;display:block}
.box{position:absolute;border:2px solid;box-sizing:border-box;cursor:pointer}
.box:hover,.box.on{background:rgba(255,214,0,.35);border-width:3px}
.box .id{position:absolute;top:-1px;left:-1px;font-size:9px;line-height:1;padding:1px 3px;color:#fff}
.pane{position:fixed;right:0;bottom:0;left:0;max-height:34vh;overflow:auto;background:#fff;
  border-top:2px solid #333;padding:10px 16px;font-size:13px;display:none;z-index:30}
.pane.on{display:block}
.k{color:#666;font-size:11px}
.merged{padding:0 12px 12px}
.merged table{border-collapse:collapse;width:100%;background:#fff;font-size:12px}
.merged td,.merged th{border:1px solid #e0e0e0;padding:5px 7px;vertical-align:top;text-align:left}
.merged th{background:#fafafa}
details>summary{cursor:pointer;padding:8px 12px;font-weight:600}
.txtcols{display:flex;gap:12px;padding:0 12px 40px}
.txtcol{flex:1;min-width:0;background:#fff;border:1px solid #ddd;border-radius:6px;
  max-height:60vh;overflow:auto;font-size:12px}
.txtcol .row{padding:4px 8px;border-bottom:1px solid #f0f0f0;word-break:break-all}
.txtcol .rid{color:#888;font-size:10px;margin-right:5px}
"""

JS = """
document.addEventListener('click', function (e) {
  var b = e.target.closest('.box');
  var pane = document.getElementById('pane');
  document.querySelectorAll('.box.on').forEach(function (x) { x.classList.remove('on'); });
  if (!b) { pane.classList.remove('on'); return; }
  b.classList.add('on');
  pane.innerHTML = '<div class="k">' + b.dataset.case + ' / ' + b.dataset.rid +
    ' / label=' + b.dataset.label + ' / bbox=' + b.dataset.bbox + '</div>' +
    '<div>' + (b.dataset.text || '<i>(글자 없음)</i>') + '</div>';
  pane.classList.add('on');
});
"""


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _summary(doc: dict) -> dict:
    """설정 하나의 결과를 지표 묶음으로 요약한다.

    영역 수만 보면 안 된다. 같은 문서에서 영역이 33→28 로 줄었는데 줄 수가 그대로면
    "읽은 글자를 어떻게 묶었나"의 문제이고, 줄·글자까지 줄었으면 그 설정은 글자를
    잃은 것이다 — 후자는 영역 실험이 아니라 OCR 손실 실험이 된다. unclip 이 실제로
    그렇다(프로브: 1.5 에서 줄 119→132).

    **총 줄·총 글자(= 영역에 담긴 것 + 미배정)가 이 실험의 핵심 지표다.** 실측
    (2026-09-09, `1. 예금성상품(입출식·적립식 통합).pdf`): 영역이 8~28 로 3.5배
    흔들리는데 총 줄은 8케이스 중 6케이스에서 66 으로 같았다(나머지 둘은 65). 글자를
    못 읽은 게 아니라 **읽은 줄을 어떻게 묶었는지만 달라진다**는 뜻이다.

    담긴 것만 세면 안 되는 이유가 실측으로 나왔다 — `2. 예금성상품(거치식)` 의 `large`
    는 담긴 글자가 1084→1006 으로 줄었지만 총 줄은 63 그대로였다. 글자를 잃은 게 아니라
    5줄이 미배정으로 빠진 것이다(미배정 5→10). 담긴 값만 보면 "글자 유실"로 오독한다.

    미배정 줄은 담을 영역을 못 찾은 줄이라 낮을수록 좋다. 방향이 분명한 유일한 지표다 —
    영역 수는 많은 쪽이 옳은지 적은 쪽이 옳은지 문서마다 다르다
    (docs/영역분할-모델설정-종합진단.md §7.4).
    """
    pages = doc.get("pages") or []
    regions = sum(len(p.get("regions") or []) for p in pages)
    lines = sum(len(r.get("lines") or []) for p in pages for r in p.get("regions") or [])
    chars = sum(len(l.get("text") or "")
                for p in pages for r in p.get("regions") or [] for l in r.get("lines") or [])
    stray = sum(len(p.get("unassigned_lines") or []) for p in pages)
    stray_chars = sum(len(l.get("text") or "")
                      for p in pages for l in p.get("unassigned_lines") or [])
    return {
        "total_lines": lines + stray,
        "total_chars": chars + stray_chars,
        "regions": regions,
        "unassigned_lines": stray,
        "lines_in_regions": lines,
        "chars_in_regions": chars,
    }


def build_html(doc_name: str, cases: list[tuple[str, str, dict]], images: dict) -> str:
    parts = [f"<!doctype html><meta charset='utf-8'><title>{esc(doc_name)} 영역 대조</title>",
             f"<style>{CSS}</style>", "<header>",
             f"<h1>{esc(doc_name)} — 영역 분할 대조</h1><div class='sum'>"]
    for key, name, doc in cases:
        s = _summary(doc)
        parts.append(f"<span style='color:{COLORS[key]}'>&#9632;</span> <b>{esc(name)}</b> "
                     f"영역 {s['regions']} / 총 줄 {s['total_lines']} / "
                     f"총 글자 {s['total_chars']}"
                     f"{f' / 미배정 {s['unassigned_lines']}' if s['unassigned_lines'] else ''}"
                     " &nbsp;&nbsp;")
    parts.append("</div></header>")

    # 현행에서는 따로였는데 다른 설정에서 하나가 된 자리
    by_key = {k: d for k, _, d in cases}
    for key, name, doc in cases:
        if key == "baseline":
            continue
        rows = []
        other_pages = doc.get("pages") or []
        for page_index, page in enumerate(by_key["baseline"].get("pages") or []):
            if page_index >= len(other_pages):
                continue
            for group in merge_groups(page, other_pages[page_index]):
                src = "<br>".join(f"- {esc(region_text(a)[:110])}" for a in group["sources"])
                rows.append(f"<tr><td>{page_index + 1}</td>"
                            f"<td>{esc(group['target'].get('region_id'))} "
                            f"<span class='k'>{esc(group['target'].get('label'))}</span><br>"
                            f"<span class='k'>{esc(group['target'].get('bbox'))}</span></td>"
                            f"<td>{src}</td></tr>")
        parts.append(f"<div class='merged'><table><tr><th colspan='3' "
                     f"style='background:{COLORS[key]};color:#fff'>{esc(name)} — "
                     f"현행에서 따로였다가 하나가 된 자리 {len(rows)}곳</th></tr>"
                     "<tr><th>쪽</th><th>합쳐진 영역</th><th>삼켜진 현행 영역</th></tr>"
                     + ("".join(rows) or "<tr><td colspan='3'>없음</td></tr>")
                     + "</table></div>")

    # 쪽마다 3열 이미지
    n_pages = max(len(d.get("pages") or []) for _, _, d in cases)
    for page_index in range(n_pages):
        parts.append("<div class='cols'>")
        for key, name, doc in cases:
            pages = doc.get("pages") or []
            page = pages[page_index] if page_index < len(pages) else {}
            regions = page.get("regions") or []
            canvas_w = page.get("canvas_w") or 1
            canvas_h = page.get("canvas_h") or 1
            src = images.get(page_index, "")
            parts.append(f"<div class='col'><h2>{esc(name)}"
                         f"<span class='n'>p{page_index + 1} / 영역 {len(regions)}</span></h2>"
                         f"<div class='canvas'><img src='{esc(src)}' loading='lazy'>")
            for region in regions:
                box = region.get("bbox")
                if not box:
                    continue
                style = (f"left:{box[0] / canvas_w * 100:.3f}%;"
                         f"top:{box[1] / canvas_h * 100:.3f}%;"
                         f"width:{(box[2] - box[0]) / canvas_w * 100:.3f}%;"
                         f"height:{(box[3] - box[1]) / canvas_h * 100:.3f}%;"
                         f"border-color:{COLORS[key]}")
                short = (region.get("region_id") or "").split("_")[-1]
                parts.append(
                    f"<div class='box' style='{style}' data-case=\"{esc(name)}\" "
                    f"data-rid='{esc(region.get('region_id'))}' "
                    f"data-label='{esc(region.get('label'))}' "
                    f"data-bbox='{esc(box)}' "
                    f"data-text=\"{esc(region_text(region))}\" "
                    f"title=\"{esc(region_text(region)[:180])}\">"
                    f"<span class='id' style='background:{COLORS[key]}'>{esc(short)}</span>"
                    "</div>")
            parts.append("</div></div>")
        parts.append("</div>")

    # 영역별 텍스트 3열
    parts.append("<details open><summary>영역별 텍스트 (읽기 순서)</summary>"
                 "<div class='txtcols'>")
    for key, name, doc in cases:
        parts.append(f"<div class='txtcol'><div class='row' style='position:sticky;top:0;"
                     f"background:{COLORS[key]};color:#fff'>{esc(name)}</div>")
        for page in doc.get("pages") or []:
            for region in page.get("regions") or []:
                text = esc(region_text(region)) or "<i>(글자 없음)</i>"
                parts.append(f"<div class='row'><span class='rid'>"
                             f"{esc(region.get('region_id'))}</span>{text}</div>")
        parts.append("</div>")
    parts.append("</div></details>")

    parts.append("<div class='pane' id='pane'></div>")
    parts.append(f"<script>{JS}</script>")
    return "".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--force", action="store_true", help="저장된 JSON 무시하고 다시 파싱")
    args = ap.parse_args()

    json_dir = args.out / "json"
    (args.out / "img").mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)
    base = {f.name: getattr(CFG.SETTINGS, f.name) for f in dataclasses.fields(CFG.SETTINGS)}

    index_rows = []
    manifest: dict = {"cases": {}, "documents": {}}
    for path in args.inputs:
        print(f"[{path.name}]", flush=True)
        docs = []
        for key, name, overrides in CASES:
            dest = json_dir / f"{path.stem}__{key}.json"
            if dest.exists() and not args.force:
                saved_doc = json.loads(dest.read_text(encoding="utf-8"))
                docs.append((key, name, saved_doc))
                # 재사용분도 지표를 채운다. 안 채우면 HTML 만 다시 만들려고 재실행했을 때
                # manifest 가 빈 껍데기로 덮여 원래 실행의 요청 근거가 사라진다.
                manifest["documents"].setdefault(path.name, {})[key] = {
                    **_summary(saved_doc), "reused_from_saved_json": True,
                }
                print(f"   {name:30} 저장된 결과 재사용", flush=True)
                continue
            started = time.time()
            payload_sink: dict = {}
            try:
                doc = run_case(path, overrides, base, payload_sink=payload_sink)
            except Exception as exc:
                print(f"   {name:30} 실패 — {type(exc).__name__}: {exc}", flush=True)
                continue
            dest.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            s = _summary(doc)
            manifest["cases"].setdefault(key, {"name": name, "overrides": overrides,
                                               "paddlex_payload": payload_sink})
            manifest["documents"].setdefault(path.name, {})[key] = {
                **s, "seconds": round(time.time() - started, 1),
                "vlm_called": False,
            }
            stray = s["unassigned_lines"]
            print(f"   {name:30} 영역 {s['regions']:>3} / 총 줄 {s['total_lines']:>4}"
                  f" / 총 글자 {s['total_chars']:>5}"
                  f"{f' / 미배정 {stray}' if stray else ''} / "
                  f"{time.time() - started:.0f}초", flush=True)
            docs.append((key, name, doc))
        if len(docs) < len(CASES):
            print("   건너뜀 — 설정 일부 실패", flush=True)
            continue

        sizes = [(p["canvas_w"], p["canvas_h"]) for p in docs[0][2]["pages"]]
        images = {}
        for page_index, img in enumerate(page_images(path, sizes)):
            if img.width > 900:
                img = img.resize((900, int(img.height * 900 / img.width)))
            rel = f"img/{path.stem}_p{page_index + 1}.jpg"
            img.save(args.out / rel, quality=72)
            images[page_index] = rel

        page_path = args.out / f"{path.stem}.html"
        page_path.write_text(build_html(path.stem, docs, images), encoding="utf-8")
        index_rows.append((path.stem, {k: _summary(d) for k, _, d in docs}, page_path.name))
        print(f"   -> {page_path}", flush=True)

    # 이전 실행의 요청 근거를 지우지 않는다. JSON 을 재사용해 HTML 만 다시 만드는 실행은
    # payload 를 관측할 기회가 없으므로(호출 자체를 안 한다), 있던 기록 위에 덮어쓴다.
    manifest_path = args.out / "run_manifest.json"
    merged = {"cases": {}, "documents": {}}
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            merged["cases"].update(previous.get("cases") or {})
            for doc_name, cases in (previous.get("documents") or {}).items():
                merged["documents"].setdefault(doc_name, {}).update(cases)
        except Exception as exc:
            print(f"기존 manifest 를 읽지 못해 새로 쓴다 — {type(exc).__name__}: {exc}")
    merged["cases"].update(manifest["cases"])
    for doc_name, cases in manifest["documents"].items():
        into = merged["documents"].setdefault(doc_name, {})
        for case_key, entry in cases.items():
            # 저장된 JSON 을 재사용한 기록은 지표만 있고 payload·소요시간이 없다. 이미
            # 실제 호출로 남은 기록이 있으면 그쪽이 근거로 더 낫다 — 지표는 같은 JSON
            # 에서 나오므로 동일하다.
            if entry.get("reused_from_saved_json") and case_key in into:
                continue
            into[case_key] = entry
    manifest_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    # 지표를 네 벌로 나눠 낸다. 영역 수만 놓고 어느 설정이 "낫다"고 말할 수 없다 —
    # 대출 PDF 에서는 large 가, 카드 콜라주에서는 small 이 나은 반대 사례가 이미 있다
    # (docs/영역분할-모델설정-종합진단.md §7.4). 줄·글자가 함께 줄었는지를 같이 봐야
    # "묶기가 달라진 것"과 "글자를 잃은 것"이 구분된다.
    METRICS = [
        ("total_lines", "총 줄 (담김+미배정) — 설정이 글자를 잃었는지"),
        ("total_chars", "총 글자 (담김+미배정) — 설정이 글자를 잃었는지"),
        ("regions", "영역 수 — 읽은 줄을 어떻게 묶었는지"),
        ("unassigned_lines", "미배정 줄 — 담을 영역을 못 찾은 줄 (낮을수록 좋다)"),
        ("lines_in_regions", "영역에 담긴 줄"),
    ]
    head = "".join(f"<th>{esc(n)}</th>" for _, n, _ in CASES) + "".join(
        f"<th>{esc(n)} - 현행</th>" for k, n, _ in CASES if k != "baseline")
    tables = []
    for field, label in METRICS:
        rows = "".join(
            f"<tr><td><a href='{esc(fn)}'>{esc(name)}</a></td>"
            + "".join(f"<td>{c[k][field]}</td>" for k, _, _ in CASES)
            + "".join(f"<td>{c[k][field] - c['baseline'][field]:+d}</td>"
                      for k, _, _ in CASES if k != "baseline") + "</tr>"
            for name, c, fn in index_rows)
        tables.append(f"<div class='merged'><table>"
                      f"<tr><th colspan='{2 * len(CASES)}'>{esc(label)}</th></tr>"
                      f"<tr><th>문서</th>{head}</tr>{rows}</table></div>")
    (args.out / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>영역 분할 대조</title>"
        f"<style>{CSS}</style><header><h1>영역 분할 설정 대조</h1>"
        "<div class='sum'>케이스마다 baseline 에서 한 축만 바꿨다(마지막 1건만 상호작용). "
        "요청 본문은 run_manifest.json 에 그대로 기록된다 — 서버 설정은 변경하지 않는다."
        "</div></header>" + "".join(tables),
        encoding="utf-8")
    print(f"\n목차 -> {args.out / 'index.html'}")
    print(f"요청 근거 -> {args.out / 'run_manifest.json'}")


if __name__ == "__main__":
    main()
