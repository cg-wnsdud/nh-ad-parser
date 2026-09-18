# -*- coding: utf-8 -*-
"""설정 한 축을 바꿔 한 바퀴 돌리고, 두 층을 Label Studio 에서 눈으로 보게 만든다.

    원본 → 렌더 → 타일 → spark-1118 PaddleX ─┬→ ① PaddleX 레이아웃 박스 (모델의 답)
                                              └→ ② 최종 NH Region     (우리 로직의 답)

두 층을 같은 이미지 위에 prediction 두 벌로 올린다. ①이 나쁘면 서버 설정·모델의 문제고,
①은 멀쩡한데 ②가 나쁘면 타일 중복 제거·OCR 줄 귀속·Region 조립의 문제다. 이 구분이
안 되면 "레이아웃 모델을 바꿨더니 좋아졌다"를 증명할 수 없다. VLM 은 부르지 않는다.

PowerShell 예시 (저장소 루트)::

    # 기준선 — 서버 YAML 값을 그대로 쓴다
    uv run python tools/run_layout_experiment.py --run-name base `
      --input "samples/[예금성상품-적금] 올원e적금.png"

    # 한 축만 바꿔 다시 — 직전 실행과의 차이를 표로 찍는다
    uv run python tools/run_layout_experiment.py --run-name nms-off `
      --input "samples/[예금성상품-적금] 올원e적금.png" `
      --layout-nms false --compare-with base

지정하지 않은 항목은 전부 ``server`` 다 — 요청에서 그 키를 빼 서버 YAML 값을 쓴다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_layout_preview import _render_source, _safe_stem, _sha256, _write_json  # noqa: E402

# 환경변수 이름 ← CLI 플래그. 여기 없는 축(모델 교체·클래스별 threshold)은 서버 YAML 을
# 바꿔야 하는 것들이고, 그건 docs/OCR-최적화-테스트-선택지.md 의 층 2·3 에 있다.
ENV_BY_FLAG = {
    "merge_mode": "PADDLEX_LAYOUT_MERGE_BBOXES_MODE",
    "threshold": "PADDLEX_LAYOUT_THRESHOLD",
    "unclip": "PADDLEX_LAYOUT_UNCLIP_RATIO",
    "layout_nms": "PADDLEX_LAYOUT_NMS",
    "region_detection": "PADDLEX_USE_REGION_DETECTION",
    "table_recognition": "PADDLEX_USE_TABLE_RECOGNITION",
    "text_det_limit_side_len": "PADDLEX_TEXT_DET_LIMIT_SIDE_LEN",
    "text_det_limit_type": "PADDLEX_TEXT_DET_LIMIT_TYPE",
    "text_det_thresh": "PADDLEX_TEXT_DET_THRESH",
    "text_det_box_thresh": "PADDLEX_TEXT_DET_BOX_THRESH",
    "text_det_unclip": "PADDLEX_TEXT_DET_UNCLIP_RATIO",
    "text_rec_score_thresh": "PADDLEX_TEXT_REC_SCORE_THRESH",
    "tile_height": "TILE_MAX_HEIGHT_PX",
    "tile_overlap": "TILE_OVERLAP_PX",
    "tile_trigger": "TILE_TRIGGER_HEIGHT_PX",
    "render_dpi": "PDF_RENDER_DPI",
}


SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg"}


def _iter_sources(inputs: list[Path], exclude: list[str]) -> list[Path]:
    """파일과 디렉터리를 섞어 받아 정렬된 입력 목록으로 편다."""
    found: list[Path] = []
    for item in inputs:
        path = item.resolve()
        if path.is_dir():
            found.extend(sorted(
                child for child in path.iterdir()
                if child.is_file() and child.suffix.lower() in SUPPORTED
            ))
        elif path.is_file():
            found.append(path)
        else:
            raise SystemExit(f"입력이 없습니다: {path}")
    kept = [p for p in found if not any(token and token in p.name for token in exclude)]
    if not kept:
        raise SystemExit("처리할 입력이 없습니다 (--exclude 로 전부 걸러졌을 수 있습니다)")
    return kept


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="영역 실험 한 바퀴")
    parser.add_argument("--input", type=Path, required=True, nargs="+",
                        help="파일 여러 개 또는 폴더. 폴더면 pdf/png/jpg 를 모두 처리한다")
    parser.add_argument("--exclude", default=[], action="append",
                        help="파일명에 이 문자열이 들어가면 건너뛴다 (여러 번 지정 가능)")
    parser.add_argument("--run-name", required=True, help="산출물 폴더 이름 = 실험 이름")
    parser.add_argument("--note", default="", help="이번에 무엇을 확인하려는지 한 줄")
    parser.add_argument("--paddlex-url", default="http://127.0.0.1:18081/layout-parsing")
    parser.add_argument("--root", type=Path, default=Path("experiments/layout-labeling"))
    parser.add_argument("--compare-with", default="", help="비교할 이전 run 이름")

    group = parser.add_argument_group("PaddleX 요청 (미지정 = server = 키를 안 보냄)")
    group.add_argument("--merge-mode", default="server",
                       choices=("server", "small", "large", "union"))
    group.add_argument("--threshold", default="server")
    group.add_argument("--unclip", default="server")
    group.add_argument("--layout-nms", default="server", choices=("server", "true", "false"))
    group.add_argument("--region-detection", default="server",
                       choices=("server", "true", "false"))
    group.add_argument("--table-recognition", default="server",
                       choices=("server", "true", "false"))
    group.add_argument("--text-det-limit-side-len", default="server")
    group.add_argument("--text-det-limit-type", default="server")
    group.add_argument("--text-det-thresh", default="server")
    group.add_argument("--text-det-box-thresh", default="server")
    group.add_argument("--text-det-unclip", default="server")
    group.add_argument("--text-rec-score-thresh", default="server")

    client = parser.add_argument_group("클라이언트 전처리 (서버를 안 건드리는 축)")
    client.add_argument("--tile-height", default="1600")
    client.add_argument("--tile-overlap", default="200")
    client.add_argument("--tile-trigger", default="4000")
    client.add_argument("--render-dpi", default="200")
    return parser.parse_args()


def _delta_rows(before: dict, after: dict) -> list[str]:
    rows = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key, 0), after.get(key, 0)
        if old != new:
            rows.append(f"    {key:<18} {old:>4} -> {new:>4}  ({new - old:+d})")
    return rows or ["    (라벨 분포 변화 없음)"]


def main() -> None:
    args = _parse_args()
    sources = _iter_sources(list(args.input), list(args.exclude))

    # config 모듈 import 전에 지정해야 Settings 에 반영된다. .env 파일은 건드리지 않고
    # 이번 process 에만 적용된다.
    os.environ["PADDLEX_URL"] = args.paddlex_url
    os.environ["REGION_READING_MODE"] = "off"
    knobs = {}
    for flag, env_name in ENV_BY_FLAG.items():
        value = str(getattr(args, flag))
        os.environ[env_name] = value
        knobs[env_name] = value

    from PIL import Image

    from nh_parser import trace
    from nh_parser.label_studio import block_to_rectangle, region_to_rectangle
    from nh_parser.ocr.paddlex import build_request_payload
    from nh_parser.pipeline import process_file

    run_dir = args.root / "tasks" / "runs" / args.run_name
    preview_dir = run_dir / "preview"
    media_dir = args.root / ".local" / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    tasks: list[dict] = []
    documents: list[dict] = []
    paddle_labels: Counter = Counter()
    region_labels: Counter = Counter()
    skipped: dict[str, int] = {}
    totals = Counter()
    started = time.time()

    for source in sources:
        source_hash = _sha256(source)
        source_key = _safe_stem(source, source_hash)
        doc_started = time.time()
        with trace.capture() as layers:
            document = process_file(source, preview_dir=preview_dir, use_vlm=False)
        doc_seconds = round(time.time() - doc_started, 3)

        payload = json.loads(document.model_dump_json())
        _write_json(run_dir / "parse" / f"{source_key}.json", payload)
        _write_json(run_dir / "layers" / f"{source_key}.json", layers)

        canvases = list(_render_source(source))
        pages = payload.get("pages") or []
        if len(canvases) != len(pages) or len(layers) != len(pages):
            raise RuntimeError(
                f"{source.name}: 페이지 수 불일치 — 렌더 {len(canvases)} / "
                f"파싱 {len(pages)} / 추적 {len(layers)}"
            )

        doc_paddle: Counter = Counter()
        doc_region: Counter = Counter()
        for index, ((canvas, route), page, layer) in enumerate(
            zip(canvases, pages, layers), start=1
        ):
            width, height = canvas.image.size
            if (page.get("canvas_w"), page.get("canvas_h")) != (width, height):
                raise RuntimeError(
                    f"{source.name} p{index}: 파싱 좌표계와 Label Studio 캔버스가 다릅니다: "
                    f"{page.get('canvas_w')}x{page.get('canvas_h')} != {width}x{height}"
                )
            filename = f"{source_key}__p{index:03d}.png"
            canvas.image.save(media_dir / filename, format="PNG", optimize=False)

            # ① 모델의 답. layout_det_res 는 확신도가 붙어 오는 유일한 목록이다
            #    (parsing_res_list 에는 score 가 없다 — ocr/paddlex.py _attach_det_scores).
            det_blocks = [
                block for block in layer.get("blocks") or []
                if block.get("source") == "layout_det_res"
            ]
            paddle_rects = []
            for order, block in enumerate(det_blocks):
                rect = block_to_rectangle(
                    block, width, height, box_id=f"{source_key}_p{index}_x{order:03d}"
                )
                if rect is not None:
                    paddle_rects.append(rect)
                    doc_paddle[block["label"]] += 1

            # ② 우리 로직의 답.
            region_rects = []
            for region in page.get("regions") or []:
                rect = region_to_rectangle(region, width, height)
                if rect is None:
                    label = str(region.get("label") or "unknown")
                    skipped[label] = skipped.get(label, 0) + 1
                else:
                    region_rects.append(rect)
                    doc_region[str(region.get("label"))] += 1

            tasks.append({
                "data": {
                    "image": f"/data/local-files/?d=pages/{filename}",
                    "source_file": source.name,
                    "source_sha256": source_hash,
                    "page_no": index,
                    "canvas_width": width,
                    "canvas_height": height,
                    "parse_route": route,
                    "run_name": args.run_name,
                },
                "predictions": [
                    {"model_version": f"{args.run_name} 2-nh-region", "result": region_rects},
                    {"model_version": f"{args.run_name} 1-paddlex", "result": paddle_rects},
                ],
            })

        doc_stats = {
            "source_file": source.name,
            "source_sha256": source_hash,
            "seconds": doc_seconds,
            "pages": len(pages),
            "canvas": [[p.get("canvas_w"), p.get("canvas_h")] for p in pages],
            "parse_route": [p.get("parse_route") for p in pages],
            "tiles": sum(len(layer.get("tiles") or []) for layer in layers),
            "paddlex_blocks_before_tile_dedupe": sum(
                len([b for b in (layer.get("raw_blocks") or [])
                     if b.get("source") == "layout_det_res"])
                for layer in layers
            ),
            "paddlex_blocks": sum(doc_paddle.values()),
            "paddlex_labels": dict(sorted(doc_paddle.items())),
            "merged_by_tile_dedupe": sum(layer.get("merged_blocks", 0) for layer in layers),
            "ocr_lines_raw": sum(layer.get("ocr_lines_raw", 0) for layer in layers),
            "ocr_lines_deduped": sum(layer.get("ocr_lines_deduped", 0) for layer in layers),
            "regions": sum(doc_region.values()),
            "region_labels": dict(sorted(doc_region.items())),
        }
        documents.append(doc_stats)
        paddle_labels.update(doc_paddle)
        region_labels.update(doc_region)
        for key in ("pages", "tiles", "paddlex_blocks", "paddlex_blocks_before_tile_dedupe",
                    "merged_by_tile_dedupe", "ocr_lines_raw", "ocr_lines_deduped", "regions"):
            totals[key] += doc_stats[key]
        print(f"  · {source.name}  {doc_seconds}s  {doc_stats['pages']}쪽 "
              f"타일 {doc_stats['tiles']}  ①{doc_stats['paddlex_blocks']} "
              f"②{doc_stats['regions']}")

    elapsed = round(time.time() - started, 3)
    import_path = run_dir / "label-studio-import.json"
    _write_json(import_path, tasks)

    # 요청 본문의 실제 모습을 남긴다 — "이 실행이 무엇을 보냈는지"를 기억에 의존하지
    # 않기 위해서다. 실제 file 바이트는 base64 라 여기서는 넣지 않는다.
    sample_payload = build_request_payload(Image.new("RGB", (8, 8), "white"))
    summary = {
        "run_name": args.run_name,
        "note": args.note,
        "inputs": [str(path) for path in sources],
        "paddlex_url": args.paddlex_url,
        "knobs": knobs,
        "request_payload": sample_payload,
        "use_vlm": False,
        "seconds": elapsed,
        "documents": documents,
        "pages": totals["pages"],
        "tiles": totals["tiles"],
        "paddlex_blocks_before_tile_dedupe": totals["paddlex_blocks_before_tile_dedupe"],
        "paddlex_blocks": totals["paddlex_blocks"],
        "paddlex_labels": dict(sorted(paddle_labels.items())),
        "merged_by_tile_dedupe": totals["merged_by_tile_dedupe"],
        "ocr_lines_raw": totals["ocr_lines_raw"],
        "ocr_lines_deduped": totals["ocr_lines_deduped"],
        "regions": totals["regions"],
        "region_labels": dict(sorted(region_labels.items())),
        "skipped_prediction_labels": skipped,
    }
    _write_json(run_dir / "run.json", summary)

    print(f"[{args.run_name}] {elapsed}s / 문서 {len(sources)}개 / "
          f"{summary['pages']}쪽 / 타일 {summary['tiles']}개")
    print(f"  1) PaddleX 레이아웃 박스 {summary['paddlex_blocks']}개"
          f" (타일 중복 {summary['merged_by_tile_dedupe']}개 병합 전"
          f" {summary['paddlex_blocks_before_tile_dedupe']}개)")
    print(f"     {summary['paddlex_labels']}")
    print(f"  2) 최종 NH Region  {summary['regions']}개")
    print(f"     {summary['region_labels']}")
    print(f"  OCR 줄 {summary['ocr_lines_raw']} -> dedupe {summary['ocr_lines_deduped']}")
    if skipped:
        print(f"  PP 20종 밖이라 prediction 제외: {skipped}")
    print(f"  Label Studio import -> {import_path}")

    if args.compare_with:
        other = args.root / "tasks" / "runs" / args.compare_with / "run.json"
        if not other.is_file():
            print(f"  ! 비교 대상이 없다: {other}")
            return
        base = json.loads(other.read_text(encoding="utf-8"))
        print(f"\n  -- {args.compare_with} 대비 --")
        print(f"    소요            {base['seconds']}s -> {elapsed}s")
        print(f"    1) PaddleX 박스 {base['paddlex_blocks']} -> {summary['paddlex_blocks']}")
        print("\n".join(_delta_rows(base.get("paddlex_labels") or {},
                                    summary["paddlex_labels"])))
        print(f"    2) NH Region    {base['regions']} -> {summary['regions']}")
        print("\n".join(_delta_rows(base.get("region_labels") or {},
                                    summary["region_labels"])))


if __name__ == "__main__":
    main()
