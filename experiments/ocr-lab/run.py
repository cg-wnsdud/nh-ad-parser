# -*- coding: utf-8 -*-
"""ocr-lab — 입력 한 장 = PaddleX 호출 한 번. 응답을 가공 없이 본다.

기존 파이프라인(`src/nh_parser/pipeline.py`)의 타일링·중복제거·Region 조립·VLM 을
**하나도 쓰지 않는다.** 재사용하는 것은 입력 처리(PDF 렌더 / 이미지 로드 / HWP 자산
추출)뿐이고, 그마저도 triage 판정은 기록만 하고 크기 결정에는 쓰지 않는다.

PowerShell 예시 (저장소 루트)::

    # 기준선 — 기존과 같은 크기로 보낸다
    uv run python experiments/ocr-lab/run.py --run-name asis `
      --input "samples/spark1118-sample" --sizing asis

    # 긴 변 2500px 상한을 모든 입력에 똑같이 적용
    uv run python experiments/ocr-lab/run.py --run-name max2500 `
      --input "samples/spark1118-sample" --sizing maxside --max-side 2500 `
      --compare-with asis
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from lab import client, tiling, view  # noqa: E402
from lab.ingest import iter_inputs, load_pages  # noqa: E402

ROOT = Path(__file__).resolve().parent
# Label Studio 컨테이너가 이미 이 폴더를 /label-studio/files/pages 로 마운트하고 있다.
# 새 마운트를 추가하려면 컨테이너를 다시 만들어야 해서 같은 자리를 쓴다.
MEDIA_DIR = ROOT.parent / "layout-labeling" / ".local" / "media"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ocr-lab: 한 장 = 한 호출")
    parser.add_argument("--input", type=Path, nargs="+", required=True)
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--run-name", required=True)
    parser.add_argument(
        "--media-key",
        default="",
        help="여러 설정이 같은 렌더 이미지를 공유할 때 쓰는 이름(기본: run-name)",
    )
    parser.add_argument("--note", default="")
    parser.add_argument("--paddlex-url", default="http://127.0.0.1:18081/layout-parsing")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--compare-with", default="")

    size = parser.add_argument_group("크기 정책 (모든 입력에 동일하게 적용)")
    size.add_argument("--sizing", choices=("asis", "maxside"), default="asis")
    size.add_argument("--max-side", type=int, default=2500)
    size.add_argument("--encode", choices=("jpeg", "png"), default="jpeg")

    tile = parser.add_argument_group("자르기 (판단 기준은 픽셀 수가 아니라 가로세로 비율)")
    tile.add_argument("--tiling", choices=("auto", "off"), default="auto")
    tile.add_argument("--aspect-limit", type=float, default=tiling.DEFAULT_ASPECT_LIMIT,
                      help="긴 변÷짧은 변이 이 값을 넘으면 자른다 (기본 2.0)")
    tile.add_argument("--tile-span", type=int, default=tiling.DEFAULT_SPAN,
                      help="목표 조각 크기(px, 기본 1600). 0 을 주면 짧은 변(정사각)")
    tile.add_argument("--tile-overlap", type=int, default=None,
                      help="겹침 픽셀. 생략하면 조각 크기의 15%% (80~200)")
    tile.add_argument("--dedupe", action="store_true",
                      help="조각 경계 중복 박스를 합친다 (기본 끔 — 중복을 그대로 본다)")

    knob = parser.add_argument_group("PaddleX 요청 (미지정 = server = 키를 안 보냄)")
    knob.add_argument("--layout-threshold", default="server")
    knob.add_argument("--layout-unclip-ratio", default="server")
    knob.add_argument("--layout-merge-bboxes-mode", default="server")
    knob.add_argument("--layout-nms", default="server")
    knob.add_argument("--use-region-detection", default="server")
    knob.add_argument("--use-table-recognition", default="server")
    knob.add_argument("--text-det-limit-side-len", default="server")
    knob.add_argument("--text-det-limit-type", default="server")
    knob.add_argument("--text-det-thresh", default="server")
    knob.add_argument("--text-det-box-thresh", default="server")
    knob.add_argument("--text-det-unclip-ratio", default="server")
    knob.add_argument("--text-rec-score-thresh", default="server")
    return parser.parse_args()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_name(text: str) -> str:
    clean = "".join(c if c.isalnum() or c in "-_." else "_" for c in text).strip("._")
    return clean[:70] or "page"


def delta_rows(before: dict, after: dict) -> list[str]:
    rows = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key, 0), after.get(key, 0)
        if old != new:
            rows.append(f"      {key:<20} {old:>4} -> {new:>4}  ({new - old:+d})")
    return rows or ["      (변화 없음)"]


def main() -> None:
    args = parse_args()
    sources = iter_inputs(list(args.input), list(args.exclude))

    payload = client.build_payload(
        layout_threshold=args.layout_threshold,
        layout_unclip_ratio=args.layout_unclip_ratio,
        layout_merge_bboxes_mode=args.layout_merge_bboxes_mode,
        layout_nms=args.layout_nms,
        use_region_detection=args.use_region_detection,
        use_table_recognition=args.use_table_recognition,
        text_det_limit_side_len=args.text_det_limit_side_len,
        text_det_limit_type=args.text_det_limit_type,
        text_det_thresh=args.text_det_thresh,
        text_det_box_thresh=args.text_det_box_thresh,
        text_det_unclip_ratio=args.text_det_unclip_ratio,
        text_rec_score_thresh=args.text_rec_score_thresh,
    )

    run_dir = ROOT / "runs" / args.run_name
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    tasks: list[dict] = []
    documents: list[dict] = []
    det_labels: Counter = Counter()
    parsing_labels: Counter = Counter()
    seen_labels: list[str] = []
    started = time.time()

    for source in sources:
        pages = load_pages(source, sizing=args.sizing, max_side=args.max_side)
        doc_pages = []
        for page in pages:
            pieces, tiling_note = tiling.plan(
                page.image, mode=args.tiling,
                aspect_limit=args.aspect_limit, overlap=args.tile_overlap,
                span_override=args.tile_span,
            )
            key = f"{safe_name(page.doc_id)}__p{page.page_no:03d}"

            det: list[dict] = []
            parsing: list[dict] = []
            lines: list[dict] = []
            seconds = 0.0
            request_bytes = 0
            for piece in pieces:
                result = client.call(piece.image, url=args.paddlex_url, payload=payload,
                                     timeout=args.timeout, fmt=args.encode)
                pruned = result["pruned"]
                seconds += result["seconds"]
                request_bytes += result["request_bytes"]
                # 조각별 원본 응답은 그대로 남긴다 — 이어붙인 그림만 보면
                # "모델이 못 잡은 것"과 "이어붙이며 잃은 것"을 못 가른다.
                suffix = "" if len(pieces) == 1 else f"_t{piece.index:02d}"
                write_json(run_dir / "raw" / f"{key}{suffix}.json", pruned)
                for found, target in ((client.det_boxes(pruned), det),
                                      (client.parsing_boxes(pruned), parsing),
                                      (client.ocr_lines(pruned), lines)):
                    for box in found:
                        moved = tiling.shift(box, piece.x_offset, piece.y_offset)
                        moved["piece"] = piece.index
                        target.append(moved)

            det_before, parsing_before = len(det), len(parsing)
            merged_det = merged_parsing = 0
            if args.dedupe and len(pieces) > 1:
                det, merged_det = tiling.dedupe(det)
                parsing, merged_parsing = tiling.dedupe(parsing)
            result = {"seconds": round(seconds, 3), "request_bytes": request_bytes}
            ocr_boxes = view.ocr_line_boxes(lines)
            unassigned = view.unassigned_ocr_lines(lines, parsing)
            # `raw/` 는 조각 좌표다. 실행끼리 비교하려면 **페이지 좌표**가 따로 있어야 한다
            # (이게 없어서 2026-09-17 분석에서 좌표계를 섞어 틀린 결론을 냈다).
            write_json(run_dir / "boxes" / f"{key}.json", {
                "canvas": list(page.image.size),
                "pieces": tiling_note["pieces"],
                "det": det, "parsing": parsing, "ocr_lines": lines,
                "unassigned_ocr_lines": unassigned,
            })

            for label in ([b["label"] for b in det] + [b["label"] for b in parsing]
                          + ["ocr_line", "unassigned_ocr"]):
                if label not in seen_labels:
                    seen_labels.append(label)
            det_labels.update(b["label"] for b in det)
            parsing_labels.update(b["label"] for b in parsing)

            width, height = page.image.size
            media_key = safe_name(args.media_key or args.run_name)
            media_name = f"lab_{media_key}__{key}.png"
            media_path = MEDIA_DIR / media_name
            # 설정 매트릭스는 같은 페이지를 여러 번 추론한다. 모델 설정만 달라질 뿐
            # 렌더 이미지는 같으므로 한 벌만 저장해 수 GB의 중복 PNG를 만들지 않는다.
            if not media_path.is_file():
                page.image.save(media_path, format="PNG", optimize=False)
            view.draw_overlay(page.image, det, seen_labels,
                              run_dir / "overlay" / f"{key}_1det.jpg")
            view.draw_overlay(page.image, parsing, seen_labels,
                              run_dir / "overlay" / f"{key}_2parsing.jpg")
            view.draw_overlay(page.image, ocr_boxes, seen_labels,
                              run_dir / "overlay" / f"{key}_3ocr.jpg")
            view.draw_overlay(page.image, unassigned, seen_labels,
                              run_dir / "overlay" / f"{key}_4unassigned.jpg")

            tasks.append({
                "data": {
                    "image": f"/data/local-files/?d=pages/{media_name}",
                    "source_file": page.source_file,
                    "page_no": page.page_no,
                    "canvas_width": width,
                    "canvas_height": height,
                    "aspect": page.aspect,
                    "run_name": args.run_name,
                    "sizing": args.sizing,
                    "origin": json.dumps(page.origin, ensure_ascii=False),
                },
                "predictions": [
                    {"model_version": f"{args.run_name} 1-det",
                     "result": [r for r in (
                         view.rectangle(b, width, height, box_id=f"{key}_d{i:03d}")
                         for i, b in enumerate(det)) if r]},
                    {"model_version": f"{args.run_name} 2-parsing",
                     "result": [r for r in (
                         view.rectangle(b, width, height, box_id=f"{key}_p{i:03d}")
                         for i, b in enumerate(parsing)) if r]},
                    {"model_version": f"{args.run_name} 3-ocr",
                     "result": [r for r in (
                         view.rectangle(b, width, height, box_id=f"{key}_o{i:03d}")
                         for i, b in enumerate(ocr_boxes)) if r]},
                    {"model_version": f"{args.run_name} 4-unassigned",
                     "result": [r for r in (
                         view.rectangle(b, width, height, box_id=f"{key}_u{i:03d}")
                         for i, b in enumerate(unassigned)) if r]},
                ],
            })

            doc_pages.append({
                "page_no": page.page_no,
                "origin": page.origin,
                "canvas_px": [width, height],
                "distortion": tiling_note["distortion"],
                "tiling": tiling_note,
                "seconds": result["seconds"],
                "request_kb": round(result["request_bytes"] / 1024),
                "det_boxes_before_dedupe": det_before,
                "det_boxes": len(det),
                "merged_det": merged_det,
                "det_labels": dict(sorted(Counter(b["label"] for b in det).items())),
                "parsing_boxes_before_dedupe": parsing_before,
                "parsing_boxes": len(parsing),
                "merged_parsing": merged_parsing,
                "parsing_labels": dict(sorted(Counter(b["label"] for b in parsing).items())),
                "ocr_lines": len(lines),
                "unassigned_ocr_lines": len(unassigned),
            })
            cut = (f"통짜" if tiling_note["decision"] == "whole"
                   else f"{tiling_note['axis']}축 {tiling_note['pieces']}조각")
            print(f"  · {page.source_file} p{page.page_no}  {width}x{height} "
                  f"(왜곡 {tiling_note['distortion']}) → {cut}  {result['seconds']}s  "
                  f"①{len(det)} ②{len(parsing)} 줄{len(lines)}")

        documents.append({"source_file": source.name, "pages": doc_pages})

    elapsed = round(time.time() - started, 3)
    (run_dir / "labeling-config.xml").write_text(
        view.labeling_config(seen_labels), encoding="utf-8"
    )
    write_json(run_dir / "tasks.json", tasks)
    summary = {
        "run_name": args.run_name,
        "note": args.note,
        "sizing": args.sizing,
        "max_side": args.max_side,
        "encode": args.encode,
        "tiling": args.tiling,
        "aspect_limit": args.aspect_limit,
        "tile_span": args.tile_span,
        "tile_overlap": args.tile_overlap,
        "dedupe": args.dedupe,
        "paddlex_url": args.paddlex_url,
        "request_payload": payload,
        "seconds": elapsed,
        "pages": len(tasks),
        "det_boxes": sum(det_labels.values()),
        "det_labels": dict(sorted(det_labels.items())),
        "parsing_boxes": sum(parsing_labels.values()),
        "parsing_labels": dict(sorted(parsing_labels.items())),
        "labels_seen": seen_labels,
        "documents": documents,
    }
    write_json(run_dir / "run.json", summary)

    split = sum(1 for d in documents for p in d["pages"]
                if p["tiling"]["decision"] == "split")
    print(f"\n[{args.run_name}] {elapsed}s / {len(tasks)}쪽 "
          f"(통짜 {len(tasks) - split}쪽 · 자름 {split}쪽)")
    print(f"  1-det     {summary['det_boxes']}개  {summary['det_labels']}")
    print(f"  2-parsing {summary['parsing_boxes']}개  {summary['parsing_labels']}")
    print("  3-ocr / 4-unassigned 는 페이지별 run.json과 Label Studio에서 확인")
    print(f"  Label Studio tasks -> {run_dir / 'tasks.json'}")
    print(f"  설정 XML           -> {run_dir / 'labeling-config.xml'}")

    if args.compare_with:
        other = ROOT / "runs" / args.compare_with / "run.json"
        if not other.is_file():
            print(f"  ! 비교 대상이 없다: {other}")
            return
        base = json.loads(other.read_text(encoding="utf-8"))
        print(f"\n  -- {args.compare_with} 대비 --")
        print(f"    소요        {base['seconds']}s -> {elapsed}s")
        print(f"    1-det       {base['det_boxes']} -> {summary['det_boxes']}")
        print("\n".join(delta_rows(base.get("det_labels") or {}, summary["det_labels"])))
        print(f"    2-parsing   {base['parsing_boxes']} -> {summary['parsing_boxes']}")
        print("\n".join(delta_rows(base.get("parsing_labels") or {},
                                   summary["parsing_labels"])))


if __name__ == "__main__":
    main()
