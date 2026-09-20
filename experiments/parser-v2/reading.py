"""영역 판독 — VLM이 Region을 독립적으로 읽고 OCR 결과와 대조한다.

OCR 은 디자인 문구·스타일 글자에서 무너진다. 실측(2026-09-20, 9개 파일):

    2. 카드상품 p1_r005   `※상환능력에비해신용카드사용액이과도할경우,귀하의개인신용평점이그을V`
    3. 예금성(거치식) p1_r019  `이아융이HN이ㄷ`
    2. 대출성상품 p1_r025  `)`

그런데 깨진 채로 라벨링에 가면 엉뚱한 구분값이 붙는다 — `p1_r025` 는 `)` 한 글자로
`상품명` 라벨을 받았다. 그래서 라벨링 **앞에서** 판독을 끝낸다.

페이지 전체를 훑어 "빠진 문구"를 찾는 방식은 쓰지 않는다. 같은 9개 파일에서 0건
나왔다 — 모델이 목록에 없는 것을 스스로 대조하지 못한다. 영역마다 물어야 한다.

**OCR 정본을 VLM 판독으로 덮어쓰지 않는다.** 좌표가 있는 쪽이 OCR 이므로 정본은
OCR 이고, VLM 은 대조용 후보다. 다만 OCR 이 아무것도 못 읽은 자리는 VLM 판독만이
유일한 텍스트라 그때만 정본이 되고 `bbox_quality` 로 구분한다.
"""
from __future__ import annotations

import os
from difflib import SequenceMatcher
from typing import Any

from PIL import Image

from nh_parser.vlm import client as vlm_client

# 공백을 지우고 비교하므로 띄어쓰기·줄바꿈만 다른 경우는 1.000 이 나온다.
# 임계값은 실측으로 골랐다(2026-09-20).
#
#     띄어쓰기만 다름                                        1.000
#     줄바꿈만 다름                                          1.000
#     오탈자 하나 (`연 2.25%` vs `연 2.26%`)                  0.900
#     꼬리가 깨짐 (`…개인신용평점이그을V`)                      0.842
#     통째로 깨짐 (`이아융이HN이ㄷ`)                            0.176
#
# 0.8 이면 꼬리가 깨진 경우가 통과하고, 0.9 는 오탈자 하나가 경계에 정확히
# 걸터앉는다. 0.95 로 두면 둘 다 잡히면서 형식 차이는 그대로 통과한다 —
# 금리 숫자 한 자 차이는 광고 심의에서 가장 크게 문제 되는 종류라 검수로
# 올리는 쪽이 맞다.
AGREE = 0.95
# crop 여백. 글자가 테두리에 붙어 잘리면 모델이 앞뒤를 못 읽는다.
CROP_PAD = 12
# 너무 작은 영역은 확대해 넣는다. 원본 그대로면 글자가 뭉개진다.
MIN_CROP_SIDE = 320
SCOPES = ("all", "targeted", "off")

_SCHEMA = {
    "type": "object",
    "properties": {
        # 설명할 자리를 **먼저** 준다. 없으면 모델이 JSON 을 닫고 그 뒤에 계속
        # 말해서 그 문장이 text 값에 섞인다 — 실측(2026-09-20):
        # `NH농협카드"} (Note: The user requested to transc…`.
        # 같은 이유로 응답이 5,100자까지 늘어 10건이 파싱 실패했다.
        "analysis": {"type": "string"},
        "text": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["analysis", "text", "confidence"],
    "additionalProperties": False,
}

_PROMPT = """첨부 이미지는 광고 문서에서 잘라낸 영역 하나입니다.

보이는 글자를 **그대로** 옮겨 적으세요.

- 줄바꿈은 보이는 대로 유지하세요.
- 로고·아이콘 안의 글자도 읽을 수 있으면 적으세요.
- 읽을 수 없거나 글자가 없으면 빈 문자열을 반환하세요.
- 없는 내용을 채우거나 요약하지 마세요.
- 하고 싶은 말은 analysis 에 한 문장으로 쓰고, text 에는 **글자만** 담으세요.
"""


def scope_from_env() -> str:
    value = str(os.environ.get("PARSER_V2_READING_SCOPE", "all")).strip().lower()
    return value if value in SCOPES else "all"


def _normalized(value: Any) -> str:
    return "".join(str(value or "").split())


def agreement(left: str, right: str) -> float:
    a, b = _normalized(left), _normalized(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def should_read(region: dict[str, Any], scope: str) -> bool:
    """이 Region 을 VLM 에 보낼지 정한다.

    표 Region 은 제외한다. 셀 배치 단계에서 이미 같은 crop 을 VLM 이 봤고,
    격자를 평문으로 다시 읽으면 무조건 불일치로 나온다.
    """
    if scope == "off" or not region.get("bbox"):
        return False
    if region.get("kind") == "table" or region.get("table"):
        return False
    if scope == "all":
        return True
    # targeted — 의심스러운 곳만.
    text = _normalized(region.get("text"))
    if len(text) <= 3:
        return True
    if region.get("text_selection_status") == "conflict_pending_vlm":
        return True
    return not (region.get("lines") or [])


def _crop(image: Image.Image, box: list[int]) -> Image.Image | None:
    x0 = max(0, int(box[0]) - CROP_PAD)
    y0 = max(0, int(box[1]) - CROP_PAD)
    x1 = min(image.width, int(box[2]) + CROP_PAD)
    y1 = min(image.height, int(box[3]) + CROP_PAD)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    crop = image.crop((x0, y0, x1, y1))
    scale = MIN_CROP_SIDE / max(1, min(crop.size))
    if scale > 1.0:
        scale = min(scale, 4.0)
        crop = crop.resize(
            (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
            Image.LANCZOS,
        )
    return crop


def read_region(image: Image.Image, region: dict[str, Any]) -> dict[str, Any] | None:
    """Region crop 하나를 독립적으로 전사한다. OCR 결과는 보여주지 않는다.

    OCR 텍스트를 프롬프트에 넣으면 모델이 그것을 따라 적어 대조가 무의미해진다.
    """
    crop = _crop(image, region["bbox"])
    if crop is None:
        return None
    # 영역 길이에 맞춰 예산을 잡는다. 실측 최장 Region 텍스트가 697자였다.
    budget = min(6000, 1200 + 3 * len(str(region.get("text") or "")))
    result = vlm_client.chat_json(
        [
            {"type": "text", "text": _PROMPT},
            vlm_client.image_part(crop, box=(1400, 1400), quality=92),
        ],
        schema_name="parser_v2_region_reading",
        schema=_SCHEMA,
        max_tokens=budget,
    )
    return {
        "text": clean_text(result.get("text")),
        "confidence": float(result.get("confidence") or 0.0),
        "analysis": str(result.get("analysis") or "")[:300],
    }


def clean_text(value: Any) -> str:
    """모델이 JSON 을 닫고 이어 쓴 잡담을 잘라낸다.

    analysis 자리를 준 뒤에도 가끔 새어 나온다. 정본 후보로 쓰이는 값이라
    방어적으로 한 번 더 자른다.
    """
    text = str(value or "")
    for marker in ('"}', '"]'):
        index = text.find(marker)
        if index > 0:
            text = text[:index]
    return text.strip()


def apply_reading(region: dict[str, Any], reading: dict[str, Any]) -> str:
    """판독 결과를 Region 에 반영하고 어떤 판정이었는지 돌려준다.

    정본을 바꾸는 경우는 **OCR 이 아무것도 못 읽었을 때 하나뿐**이다. 그 외에는
    OCR 을 유지하고 VLM 판독을 후보로 남긴다 — 줄 단위 좌표가 있는 쪽이 OCR 이다.
    """
    ocr_text = str(region.get("text") or "")
    vlm_text = str(reading.get("text") or "")
    score = agreement(ocr_text, vlm_text)
    region["vlm_reading"] = {
        "text": vlm_text,
        "confidence": reading.get("confidence"),
        "agreement": round(score, 4),
    }
    region.setdefault("text_candidates", {})["vlm_reading"] = vlm_text or None

    if not _normalized(ocr_text) and _normalized(vlm_text):
        # OCR 이 못 읽은 디자인 문구. 줄 단위 좌표가 없으므로 Region bbox 를 쓰고
        # 품질을 낮춰 표시한다.
        region["text"] = vlm_text
        region["text_source"] = "vlm_only"
        region["bbox_quality"] = "region"
        region["needs_review"] = True
        return "vlm_only"
    if not _normalized(vlm_text):
        region["reading_status"] = "vlm_blank"
        return "vlm_blank"
    if score >= AGREE:
        region["reading_status"] = "agree"
        return "agree"
    region["reading_status"] = "disagree"
    region["needs_review"] = True
    return "disagree"


def read_page(
    page: dict[str, Any], image: Image.Image, *, scope: str = "all",
) -> dict[str, int]:
    """페이지의 Region 을 훑어 판독하고 통계를 돌려준다."""
    stats = {"read": 0, "agree": 0, "disagree": 0, "vlm_only": 0, "vlm_blank": 0,
             "skipped": 0, "failed": 0}
    for region in page.get("regions") or []:
        if not should_read(region, scope):
            stats["skipped"] += 1
            continue
        try:
            reading = read_region(image, region)
        except Exception as exc:  # noqa: BLE001
            # 한 영역의 판독 실패가 페이지 전체를 멈추게 하지 않는다. OCR 정본은
            # 그대로 남고 검수 대상으로만 표시된다.
            region["reading_status"] = "failed"
            region["vlm_reading"] = {"error": str(exc)[:200]}
            region["needs_review"] = True
            stats["failed"] += 1
            continue
        if reading is None:
            stats["skipped"] += 1
            continue
        stats["read"] += 1
        stats[apply_reading(region, reading)] += 1
    page["reading_stats"] = stats
    return stats
