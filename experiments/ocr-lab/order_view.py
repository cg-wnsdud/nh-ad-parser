# -*- coding: utf-8 -*-
"""읽기순서(`block_order`)를 눈으로 보게 만든다.

Label Studio 는 박스 위에 **라벨 이름만** 그린다. 순서를 캔버스에 직접 찍을 방법이 없어서
두 가지를 같이 만든다.

    ① order-overlay/*.jpg   박스마다 순서 번호를 그려 넣은 이미지 (확실히 보인다)
    ② tasks-order.json      Label Studio 용. 결과를 **읽기순서대로 정렬**해서 넣으므로
                            오른쪽 Regions 패널의 1,2,3... 이 곧 읽기순서다.
                            각 영역에는 `#순서` 를 meta 로 붙인다.

`block_order` 가 없는 블록(header·image·table·footer 등)은 `-` 로 표시하고 뒤로 보낸다.
PaddleX 가 본문 읽기 흐름에 넣지 않은 블록이라는 뜻이다.

사용법::

    uv run python experiments/ocr-lab/order_view.py --matrix layout-20260918-v1 `
      --configs merge-union merge-large
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
MEDIA = ROOT.parent / "layout-labeling" / ".local" / "media"
sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(ROOT))
from lab import view  # noqa: E402

OVERLAY_MAX_SIDE = 2000
FONT_CANDIDATES = ("C:/Windows/Fonts/malgunbd.ttf", "C:/Windows/Fonts/arialbd.ttf",
                   "C:/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/arial.ttf")


def _font(size: int):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def order_key(block: dict) -> tuple:
    """읽기순서 우선, 없으면 좌표 순으로 뒤에 붙인다."""
    order = block.get("order")
    return (order is None, order if order is not None else 0,
            block["bbox"][1], block["bbox"][0])


def draw(image: Image.Image, blocks: list[dict], labels: list[str], out: Path) -> None:
    canvas = image.convert("RGB").copy()
    scale = min(1.0, OVERLAY_MAX_SIDE / max(canvas.size))
    if scale < 1.0:
        canvas = canvas.resize(
            (max(1, round(canvas.width * scale)), max(1, round(canvas.height * scale))),
            Image.LANCZOS,
        )
    drawer = ImageDraw.Draw(canvas)
    stroke = max(2, round(max(canvas.size) / 600))
    size = max(16, round(max(canvas.size) / 55))
    font = _font(size)

    for seat, block in enumerate(sorted(blocks, key=order_key), start=1):
        color = view.color_for(block["label"], labels)
        x0, y0, x1, y1 = (round(v * scale) for v in block["bbox"])
        drawer.rectangle([x0, y0, x1, y1], outline=color, width=stroke)
        order = block.get("order")
        tag = f"{seat}" if order is None else f"{order}"
        mark = f"{tag}" if order is not None else f"{tag}-"
        tw = drawer.textlength(mark, font=font)
        bx1, by1 = x0 + tw + size * 0.5, y0 + size * 1.25
        drawer.rectangle([x0, y0, bx1, by1], fill=color)
        drawer.text((x0 + size * 0.25, y0 + size * 0.1), mark, fill="white", font=font)

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, format="JPEG", quality=88)


def main() -> None:
    parser = argparse.ArgumentParser(description="읽기순서를 이미지와 LS task 로 본다")
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--configs", nargs="+", required=True)
    parser.add_argument("--file", default="", help="파일명 일부로 걸러내기")
    args = parser.parse_args()

    out_dir = ROOT / "matrices" / args.matrix
    if not out_dir.is_dir():
        raise SystemExit(f"{out_dir} 가 없습니다")

    pages: dict[str, dict[str, dict]] = {}
    labels: list[str] = []
    for config in args.configs:
        boxes_dir = ROOT / "runs" / f"{args.matrix}--{config}" / "boxes"
        if not boxes_dir.is_dir():
            raise SystemExit(f"{boxes_dir} 가 없습니다")
        for path in sorted(boxes_dir.glob("*.json")):
            if args.file and args.file not in path.stem:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            pages.setdefault(path.stem, {})[config] = data
            for block in data["parsing"]:
                if block["label"] not in labels:
                    labels.append(block["label"])
    if not pages:
        raise SystemExit("대상 페이지가 없습니다")

    tasks = []
    drawn = 0
    for key, per_config in sorted(pages.items()):
        media = MEDIA / f"lab_{args.matrix}__{key}.png"
        predictions = []
        for config in args.configs:
            data = per_config.get(config)
            if not data:
                continue
            width, height = data["canvas"]
            results = []
            for seat, block in enumerate(sorted(data["parsing"], key=order_key), start=1):
                rect = view.rectangle(block, width, height,
                                      box_id=f"{key}_{config}_{seat:03d}",
                                      from_name="layout")
                if not rect:
                    continue
                order = block.get("order")
                rect["meta"] = {"text": [f"#{order}" if order is not None else "#- (읽기순서 없음)"]}
                results.append(rect)
            predictions.append({"model_version": f"{config} · order", "result": results})

            if media.is_file():
                draw(Image.open(media), data["parsing"], labels,
                     out_dir / "order-overlay" / f"{key}__{config}.jpg")
                drawn += 1

        first = per_config[args.configs[0]]
        tasks.append({
            "data": {
                "image": f"/data/local-files/?d=pages/{media.name}",
                "source_file": key,
                "canvas_width": first["canvas"][0],
                "canvas_height": first["canvas"][1],
            },
            "predictions": predictions,
        })

    task_path = out_dir / "tasks-order.json"
    task_path.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "labeling-config-order.xml").write_text(
        view.labeling_config(labels), encoding="utf-8"
    )
    print(f"페이지 {len(tasks)}개 / 오버레이 {drawn}장")
    print(f"  Label Studio -> {task_path}")
    print(f"  순서 그림     -> {out_dir / 'order-overlay'}")


if __name__ == "__main__":
    main()
