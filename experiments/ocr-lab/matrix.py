# -*- coding: utf-8 -*-
"""모든 샘플에 PaddleX 설정을 하나씩 바꾸어 실행하고 비교 산출물을 만든다.

레이아웃 매트릭스의 원칙은 두 가지다.

1. 입력 처리(렌더링, auto 타일링, 좌표 복원, 중복 제거)는 모든 실행에서 같다.
2. 기준 실행에서 요청 파라미터 하나만 바꾼다. TableRecognition off는 속도용 공통
   기준선이며, 나머지 실행은 모두 이 빠른 기준선과 비교한다.

실행 결과는 ``runs/<run-name>``에, 매트릭스 요약과 Label Studio 통합 작업은
``matrices/<matrix-name>``에 저장된다. 두 폴더 모두 고객 문구가 들어가므로 Git에서
제외한다.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from compare import COVER_THRESHOLD, union_coverage
from lab import view

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
RUN = ROOT / "run.py"
RUNS = ROOT / "runs"
MATRICES = ROOT / "matrices"
DEFAULT_INPUT = ROOT.parents[1] / "samples" / "spark1118-sample"


@dataclass(frozen=True)
class Experiment:
    suffix: str
    purpose: str
    args: tuple[str, ...] = ()
    reference: str = "table-off"


LAYOUT_EXPERIMENTS = (
    Experiment("server", "spark-1118 YAML 그대로(Table/Region 포함)", reference=""),
    Experiment(
        "table-off",
        "TableRecognition만 끔: 영역 유지 여부와 반복 속도 확인",
        ("--use-table-recognition", "false"),
        reference="server",
    ),
    Experiment(
        "region-off",
        "RegionDetection을 끄고 parsing 그룹·순서 변화 확인",
        ("--use-table-recognition", "false", "--use-region-detection", "false"),
    ),
    Experiment(
        "merge-large",
        "포함 박스 중 큰 박스 우선: 과분할 감소 가능성과 과병합 확인",
        ("--use-table-recognition", "false", "--layout-merge-bboxes-mode", "large"),
    ),
    Experiment(
        "merge-small",
        "포함 박스 중 작은 박스 우선: 세밀한 경계와 파편화 확인",
        ("--use-table-recognition", "false", "--layout-merge-bboxes-mode", "small"),
    ),
    Experiment(
        "merge-union",
        "포함 박스를 모두 유지: 현재 클래스별 dict와 스칼라 union 차이 확인",
        ("--use-table-recognition", "false", "--layout-merge-bboxes-mode", "union"),
    ),
    Experiment(
        "threshold-03",
        "낮은 점수 후보를 더 살려 누락과 오검출 변화 확인",
        ("--use-table-recognition", "false", "--layout-threshold", "0.3"),
    ),
    Experiment(
        "threshold-06",
        "낮은 점수 후보를 버려 파편 감소와 유실 변화 확인",
        ("--use-table-recognition", "false", "--layout-threshold", "0.6"),
    ),
    Experiment(
        "unclip-11",
        "레이아웃 박스를 10% 확장: 잘림 개선과 이웃 의미 혼입 확인",
        ("--use-table-recognition", "false", "--layout-unclip-ratio", "1.1"),
    ),
)

OCR_EXPERIMENTS = (
    Experiment(
        "text-server",
        "선택한 레이아웃 기준에서 서버 TextDetection 기본값",
        ("--use-table-recognition", "false"),
        reference="",
    ),
    Experiment(
        "text-2500-max",
        "기존 클라이언트의 글자 검출 크기 2500/max",
        (
            "--use-table-recognition", "false",
            "--text-det-limit-side-len", "2500", "--text-det-limit-type", "max",
        ),
        reference="text-server",
    ),
    Experiment(
        "text-4000-max",
        "잔글씨 보존을 위한 글자 검출 크기 4000/max",
        (
            "--use-table-recognition", "false",
            "--text-det-limit-side-len", "4000", "--text-det-limit-type", "max",
        ),
        reference="text-server",
    ),
    Experiment(
        "text-box-05",
        "글자 박스 평균 점수 문턱 0.5: 누락과 오검출 변화",
        ("--use-table-recognition", "false", "--text-det-box-thresh", "0.5"),
        reference="text-server",
    ),
    Experiment(
        "text-unclip-20",
        "글자 박스 확장 2.0: 잘린 글자와 줄 결합 변화",
        ("--use-table-recognition", "false", "--text-det-unclip-ratio", "2.0"),
        reference="text-server",
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ocr-lab 전체 설정 매트릭스")
    parser.add_argument("--name", required=True, help="예: layout-20260918-v1")
    parser.add_argument("--phase", choices=("layout", "ocr", "all"), default="layout")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--paddlex-url", default="http://127.0.0.1:18081/layout-parsing")
    parser.add_argument("--health-url", default="http://127.0.0.1:18081/health")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--aspect-limit", type=float, default=2.0)
    parser.add_argument("--tile-span", type=int, default=1600)
    parser.add_argument("--force", action="store_true", help="완료된 동일 이름 실행도 다시 수행")
    parser.add_argument(
        "--label-studio-settings", nargs="+", default=("merge-union", "merge-large"),
        help="Label Studio 산출물에 넣을 설정 suffix (기본: merge-union merge-large)",
    )
    parser.add_argument(
        "--artifacts-only", action="store_true",
        help="서버를 호출하지 않고 기존 runs에서 보고서/Label Studio 파일만 다시 만든다",
    )
    return parser.parse_args()


def _experiments(phase: str) -> tuple[Experiment, ...]:
    if phase == "layout":
        return LAYOUT_EXPERIMENTS
    if phase == "ocr":
        return OCR_EXPERIMENTS
    return LAYOUT_EXPERIMENTS + OCR_EXPERIMENTS


def _run_name(matrix_name: str, suffix: str) -> str:
    return f"{matrix_name}--{suffix}"


def check_health(url: str) -> None:
    with urllib.request.urlopen(url, timeout=10) as response:
        body = json.loads(response.read())
    if body.get("errorCode") != 0:
        raise SystemExit(f"PaddleX health 실패: {body}")
    print(f"[health] {url} -> {body.get('errorMsg')}")


def completed(run_name: str) -> bool:
    path = RUNS / run_name / "run.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("pages")) and bool(data.get("documents"))


def run_one(args: argparse.Namespace, experiment: Experiment, log_dir: Path) -> None:
    name = _run_name(args.name, experiment.suffix)
    if completed(name) and not args.force:
        print(f"\n[skip] {name}: run.json이 이미 완성됨")
        return
    command = [
        sys.executable, "-u", str(RUN),
        "--run-name", name,
        "--media-key", args.name,
        "--input", str(args.input),
        "--paddlex-url", args.paddlex_url,
        "--timeout", str(args.timeout),
        "--sizing", "asis",
        "--tiling", "auto",
        "--aspect-limit", str(args.aspect_limit),
        "--tile-span", str(args.tile_span),
        "--dedupe",
        "--note", experiment.purpose,
        *experiment.args,
    ]
    print(f"\n[run] {name}\n  {experiment.purpose}")
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / f"{experiment.suffix}.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT.parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="")
            log.write(line)
        code = process.wait()
    if code:
        raise SystemExit(f"{name} 실패(exit={code}); 로그: {log_dir / f'{experiment.suffix}.log'}")


def _load_run(name: str) -> dict:
    return json.loads((RUNS / name / "run.json").read_text(encoding="utf-8"))


def _load_boxes(name: str) -> dict[str, dict]:
    output = {}
    for path in sorted((RUNS / name / "boxes").glob("*.json")):
        output[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return output


def _median(values: Iterable[float]) -> float:
    values = list(values)
    return round(statistics.median(values), 4) if values else 0.0


def _inside_center(line_box: list[int], region_box: list[int]) -> bool:
    cx = (line_box[0] + line_box[2]) / 2
    cy = (line_box[1] + line_box[3]) / 2
    return region_box[0] <= cx <= region_box[2] and region_box[1] <= cy <= region_box[3]


def metrics(run_name: str) -> tuple[dict, list[dict]]:
    run = _load_run(run_name)
    pages = _load_boxes(run_name)
    det_areas: list[float] = []
    parsing_areas: list[float] = []
    lines_per_region: list[int] = []
    details: list[dict] = []
    for key, page in pages.items():
        width, height = page["canvas"]
        canvas_area = max(1, width * height)
        det = page.get("det") or []
        parsing = page.get("parsing") or []
        lines = page.get("ocr_lines") or []
        for item in det:
            x0, y0, x1, y1 = item["bbox"]
            det_areas.append((x1 - x0) * (y1 - y0) / canvas_area * 100)
        page_lines_per_region = []
        for item in parsing:
            x0, y0, x1, y1 = item["bbox"]
            parsing_areas.append((x1 - x0) * (y1 - y0) / canvas_area * 100)
            count = sum(_inside_center(line["bbox"], item["bbox"]) for line in lines)
            lines_per_region.append(count)
            page_lines_per_region.append(count)
        details.append({
            "run": run_name,
            "page": key,
            "pieces": page.get("pieces"),
            "det_boxes": len(det),
            "parsing_boxes": len(parsing),
            "ocr_lines": len(lines),
            "median_lines_per_region": _median(page_lines_per_region),
        })
    return ({
        "run": run_name,
        "seconds": run.get("seconds"),
        "pages": run.get("pages"),
        "request_payload": run.get("request_payload"),
        "det_boxes": sum(len(p.get("det") or []) for p in pages.values()),
        "parsing_boxes": sum(len(p.get("parsing") or []) for p in pages.values()),
        "ocr_lines": sum(len(p.get("ocr_lines") or []) for p in pages.values()),
        "det_median_area_pct": _median(det_areas),
        "parsing_median_area_pct": _median(parsing_areas),
        "median_lines_per_region": _median(lines_per_region),
        "single_line_region_pct": round(
            sum(value <= 1 for value in lines_per_region) / max(1, len(lines_per_region)) * 100, 1
        ),
    }, details)


def coverage(reference: str, candidate: str, layer: str) -> dict:
    a_pages, b_pages = _load_boxes(reference), _load_boxes(candidate)
    totals = {"reference_boxes": 0, "candidate_boxes": 0,
              "candidate_missed": 0, "candidate_added": 0}
    for key in sorted(set(a_pages) & set(b_pages)):
        a, b = a_pages[key], b_pages[key]
        if a["canvas"] != b["canvas"]:
            continue
        a_boxes = [item["bbox"] for item in a.get(layer) or []]
        b_boxes = [item["bbox"] for item in b.get(layer) or []]
        totals["reference_boxes"] += len(a_boxes)
        totals["candidate_boxes"] += len(b_boxes)
        totals["candidate_missed"] += sum(
            union_coverage(box, b_boxes) < COVER_THRESHOLD for box in a_boxes
        )
        totals["candidate_added"] += sum(
            union_coverage(box, a_boxes) < COVER_THRESHOLD for box in b_boxes
        )
    return totals


def combine_label_studio(args: argparse.Namespace, experiments: tuple[Experiment, ...], out: Path) -> None:
    combined: dict[tuple[str, int], dict] = {}
    review_only: dict[tuple[str, int], dict] = {}
    parsing_only: dict[tuple[str, int], dict] = {}
    det_only: dict[tuple[str, int], dict] = {}
    ocr_only: dict[tuple[str, int], dict] = {}
    unassigned_only: dict[tuple[str, int], dict] = {}
    labels: list[str] = []
    selected = [e for e in experiments if e.suffix in set(args.label_studio_settings)]
    missing = sorted(set(args.label_studio_settings) - {e.suffix for e in selected})
    if missing:
        raise SystemExit(f"알 수 없는 Label Studio 설정: {', '.join(missing)}")
    for experiment in selected:
        name = _run_name(args.name, experiment.suffix)
        tasks = json.loads((RUNS / name / "tasks.json").read_text(encoding="utf-8"))
        boxes_by_key = _load_boxes(name)
        for task in tasks:
            key = (str(task["data"]["source_file"]), int(task["data"]["page_no"]))
            if key not in combined:
                combined[key] = {"data": task["data"], "predictions": []}
                review_only[key] = {"data": task["data"], "predictions": []}
                parsing_only[key] = {"data": task["data"], "predictions": []}
                det_only[key] = {"data": task["data"], "predictions": []}
                ocr_only[key] = {"data": task["data"], "predictions": []}
                unassigned_only[key] = {"data": task["data"], "predictions": []}
            for prediction in task.get("predictions") or []:
                model_version = str(prediction.get("model_version", ""))
                if model_version.endswith("1-det"):
                    layer, layer_no = "det", "1"
                elif model_version.endswith("2-parsing"):
                    layer, layer_no = "parsing", "2"
                else:
                    # 새 run.py가 만든 3/4층도 기존 boxes에서 같은 규칙으로 다시
                    # 계산한다. 과거 run과 미래 run의 통합 결과를 동일하게 유지한다.
                    continue
                copied = json.loads(json.dumps(prediction, ensure_ascii=False))
                copied["model_version"] = f"{experiment.suffix} · {layer_no}-{layer}"
                combined[key]["predictions"].append(copied)
                (parsing_only if layer == "parsing" else det_only)[key]["predictions"].append(copied)
                if layer == "parsing":
                    review_only[key]["predictions"].append(copied)
                for result in prediction.get("result") or []:
                    for label in result.get("value", {}).get("rectanglelabels") or []:
                        if label not in labels:
                            labels.append(label)
            image_name = str(task["data"]["image"]).rsplit("/", 1)[-1]
            page_key = Path(image_name).stem.split("__", 1)[-1]
            page_boxes = boxes_by_key[page_key]
            width, height = page_boxes["canvas"]
            ocr_boxes = view.ocr_line_boxes(page_boxes.get("ocr_lines") or [])
            unassigned = view.unassigned_ocr_lines(
                page_boxes.get("ocr_lines") or [], page_boxes.get("parsing") or [],
            )
            for layer_no, layer, boxes, target in (
                ("3", "ocr", ocr_boxes, ocr_only),
                ("4", "unassigned", unassigned, unassigned_only),
            ):
                prediction = {
                    "model_version": f"{experiment.suffix} · {layer_no}-{layer}",
                    "result": [result for result in (
                        view.rectangle(
                            box, width, height,
                            box_id=f"{page_key}_{experiment.suffix}_{layer_no}{index:03d}",
                        )
                        for index, box in enumerate(boxes)
                    ) if result],
                }
                combined[key]["predictions"].append(prediction)
                review_only[key]["predictions"].append(prediction)
                target[key]["predictions"].append(prediction)
                diagnostic_label = "ocr_line" if layer == "ocr" else "unassigned_ocr"
                if diagnostic_label not in labels:
                    labels.append(diagnostic_label)
    # review 탭은 parsing → OCR → 미배정 순으로 보는 것이 편하므로 다시 정렬한다.
    for task in review_only.values():
        task["predictions"].sort(key=lambda p: str(p.get("model_version", "")))
    (out / "tasks.json").write_text(
        json.dumps(list(combined.values()), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "tasks-parsing.json").write_text(
        json.dumps(list(parsing_only.values()), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "tasks-det.json").write_text(
        json.dumps(list(det_only.values()), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "tasks-review.json").write_text(
        json.dumps(list(review_only.values()), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "tasks-ocr.json").write_text(
        json.dumps(list(ocr_only.values()), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "tasks-unassigned.json").write_text(
        json.dumps(list(unassigned_only.values()), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "labeling-config.xml").write_text(view.labeling_config(labels), encoding="utf-8")


def write_reports(args: argparse.Namespace, experiments: tuple[Experiment, ...], out: Path) -> None:
    rows: list[dict] = []
    details: list[dict] = []
    by_suffix = {experiment.suffix: experiment for experiment in experiments}
    for experiment in experiments:
        name = _run_name(args.name, experiment.suffix)
        row, page_rows = metrics(name)
        row.update(suffix=experiment.suffix, purpose=experiment.purpose)
        if experiment.reference:
            reference = _run_name(args.name, experiment.reference)
            for layer in ("det", "parsing"):
                for key, value in coverage(reference, name, layer).items():
                    row[f"{layer}_{key}"] = value
            row["reference"] = experiment.reference
        rows.append(row)
        details.extend(page_rows)

    (out / "summary.json").write_text(
        json.dumps({"matrix": args.name, "phase": args.phase, "runs": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8"
    )
    fields = sorted({key for row in details for key in row})
    with (out / "pages.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(details)

    lines = [
        f"# OCR 설정 매트릭스 — {args.name}", "",
        f"- phase: `{args.phase}`",
        f"- 입력: `{args.input}`",
        f"- 공통 처리: asis 렌더 → aspect>{args.aspect_limit}만 {args.tile_span}px 밴드 → 좌표 복원 → dedupe",
        f"- PaddleX: `{args.paddlex_url}`", "",
        "## 전체 요약", "",
        "| 실행 | 초 | det | parsing | OCR줄 | parsing 중앙면적% | Region당 줄 중앙값 | 1줄 이하 Region% | 비교 기준 | det 누락/추가 | parsing 누락/추가 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['suffix']} | {row['seconds']} | {row['det_boxes']} | "
            f"{row['parsing_boxes']} | {row['ocr_lines']} | "
            f"{row['parsing_median_area_pct']} | {row['median_lines_per_region']} | "
            f"{row['single_line_region_pct']} | {row.get('reference','-')} | "
            f"{row.get('det_candidate_missed','-')}/{row.get('det_candidate_added','-')} | "
            f"{row.get('parsing_candidate_missed','-')}/{row.get('parsing_candidate_added','-')} |"
        )
    lines += ["", "## 실행 목적", ""]
    for row in rows:
        lines.append(f"- **{row['suffix']}**: {row['purpose']}")
        lines.append(f"  - request: `{json.dumps(row['request_payload'], ensure_ascii=False)}`")
    lines += [
        "", "## 해석 주의", "",
        "- 누락/추가는 상대 박스 합집합 커버리지 50% 기준이다.",
        "- 이 자동 지표는 다른 상품·고지문이 한 박스에 섞인 과병합을 판정하지 못한다.",
        f"- Label Studio에는 `{', '.join(args.label_studio_settings)}` 설정만 넣었다.",
        "- `tasks-review.json`은 parsing/OCR/미배정 진단층을 함께 비교하는 권장 파일이다.",
        "- `tasks-parsing.json`은 parsing 영역만, `tasks-unassigned.json`은 영역 밖 OCR 줄만 보여준다.",
        "- `tasks-det.json`은 검출기 원출력 진단용이고, `tasks.json`은 두 레이어를 모두 포함한다.",
    ]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    experiments = _experiments(args.phase)
    out = MATRICES / args.name
    out.mkdir(parents=True, exist_ok=True)
    if not args.artifacts_only:
        check_health(args.health_url)
    files = [path for path in args.input.iterdir() if path.is_file()]
    print(f"[input] {args.input} — {len(files)} files")
    manifest = {
        "matrix": args.name,
        "phase": args.phase,
        "input": str(args.input.resolve()),
        "common": {
            "sizing": "asis", "tiling": "auto", "aspect_limit": args.aspect_limit,
            "tile_span": args.tile_span, "dedupe": True,
        },
        "experiments": [experiment.__dict__ for experiment in experiments],
    }
    (out / "matrix.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not args.artifacts_only:
        for experiment in experiments:
            run_one(args, experiment, out / "logs")
    write_reports(args, experiments, out)
    combine_label_studio(args, experiments, out)
    print(f"\n[done] {out / 'summary.md'}")
    print(f"[label-studio] {out / 'tasks.json'}")


if __name__ == "__main__":
    main()
