# -*- coding: utf-8 -*-
"""영역 분할 설정을 눈으로 대조한다 — 같은 문서를 여러 설정으로 파싱해 HTML로 겹쳐 본다.

    uv run python tools/compare_regions.py --inputs samples/*.pdf --out out/영역비교

설정마다 parse JSON 을 남기고, 문서마다 HTML 한 장을 만든다. HTML 에는 원본 쪽
이미지 위에 영역 박스를 올려 세 설정을 나란히 놓고, 후보 설정에서 **합쳐진 자리**를
따로 뽑아 준다.

서버 설정은 건드리지 않는다. "서버기본" 은 우리 요청에서 layoutMergeBboxesMode 를
빼고 보내는 것뿐이다(config 의 "server" 센티널).
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

# (키, 화면표기, 설정 오버라이드)
CASES = [
    ("baseline", "현행 small · 겹침200", {}),
    ("server", "서버기본 미지정 · 겹침200", {"paddlex_layout_merge_bboxes_mode": "server"}),
    ("cand", "후보A large · 겹침400",
     {"paddlex_layout_merge_bboxes_mode": "large", "tile_overlap_px": 400}),
    ("small400", "후보B small · 겹침400", {"tile_overlap_px": 400}),
]
COLORS = {"baseline": "#d92b2b", "server": "#1a73e8", "cand": "#0f9d58",
          "small400": "#8e24aa"}
IMG_EXTS = {".png", ".jpg", ".jpeg"}


# ── 파싱 ────────────────────────────────────────────────────────────────
def run_case(path: Path, overrides: dict, base: dict) -> dict:
    for key, value in base.items():
        object.__setattr__(CFG.SETTINGS, key, value)   # 매번 기준값으로 되돌린다
    for key, value in overrides.items():
        object.__setattr__(CFG.SETTINGS, key, value)   # frozen dataclass 우회 (실험용)
    from nh_parser.pipeline import process_file
    return json.loads(process_file(path).model_dump_json())


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


def _summary(doc: dict) -> tuple[int, int]:
    pages = doc.get("pages") or []
    regions = sum(len(p.get("regions") or []) for p in pages)
    lines = sum(len(r.get("lines") or []) for p in pages for r in p.get("regions") or [])
    return regions, lines


def build_html(doc_name: str, cases: list[tuple[str, str, dict]], images: dict) -> str:
    parts = [f"<!doctype html><meta charset='utf-8'><title>{esc(doc_name)} 영역 대조</title>",
             f"<style>{CSS}</style>", "<header>",
             f"<h1>{esc(doc_name)} — 영역 분할 대조</h1><div class='sum'>"]
    for key, name, doc in cases:
        regions, lines = _summary(doc)
        parts.append(f"<span style='color:{COLORS[key]}'>&#9632;</span> <b>{esc(name)}</b> "
                     f"영역 {regions} / 줄 {lines} &nbsp;&nbsp;")
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
    for path in args.inputs:
        print(f"[{path.name}]", flush=True)
        docs = []
        for key, name, overrides in CASES:
            dest = json_dir / f"{path.stem}__{key}.json"
            if dest.exists() and not args.force:
                docs.append((key, name, json.loads(dest.read_text(encoding="utf-8"))))
                print(f"   {name:24} 저장된 결과 재사용", flush=True)
                continue
            started = time.time()
            try:
                doc = run_case(path, overrides, base)
            except Exception as exc:
                print(f"   {name:24} 실패 — {type(exc).__name__}: {exc}", flush=True)
                continue
            dest.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            regions, lines = _summary(doc)
            print(f"   {name:24} 영역 {regions:>3} / 줄 {lines:>3} / "
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
        index_rows.append((path.stem, {k: _summary(d)[0] for k, _, d in docs}, page_path.name))
        print(f"   -> {page_path}", flush=True)

    head = "".join(f"<th>{esc(n)}</th>" for _, n, _ in CASES) + "".join(
        f"<th>{esc(n)} - 현행</th>" for k, n, _ in CASES if k != "baseline")
    rows = "".join(
        f"<tr><td><a href='{esc(fn)}'>{esc(name)}</a></td>"
        + "".join(f"<td>{c[k]}</td>" for k, _, _ in CASES)
        + "".join(f"<td>{c[k] - c['baseline']:+d}</td>"
                  for k, _, _ in CASES if k != "baseline") + "</tr>"
        for name, c, fn in index_rows)
    (args.out / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>영역 분할 대조</title>"
        f"<style>{CSS}</style><header><h1>영역 분할 설정 대조 — 영역 수</h1></header>"
        f"<div class='merged'><table><tr><th>문서</th>{head}</tr>{rows}</table></div>",
        encoding="utf-8")
    print(f"\n목차 -> {args.out / 'index.html'}")


if __name__ == "__main__":
    main()
