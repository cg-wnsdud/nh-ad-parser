"""영역 판독 — VLM 전사와 OCR 정본의 대조 규칙을 검증한다.

정본을 바꾸는 경우는 OCR 이 아무것도 못 읽었을 때 하나뿐이다.
"""
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PARSER_V2 = ROOT / "experiments" / "parser-v2"
sys.path.insert(0, str(PARSER_V2))
sys.path.insert(0, str(ROOT / "experiments" / "ocr-lab"))
sys.path.insert(0, str(ROOT / "src"))


def _module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PARSER_V2 / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reading = _module("parser_v2_reading", "reading.py")


def test_matching_reading_keeps_the_ocr_text():
    region = {"bbox": [0, 0, 10, 10], "text": "가입금액 100만원 이상",
              "text_source": "digital_ocr_lines"}

    status = reading.apply_reading(
        region, {"text": "가입금액 100만원 이상", "confidence": 0.95})

    assert status == "agree"
    assert region["text"] == "가입금액 100만원 이상"
    assert region["text_source"] == "digital_ocr_lines"
    assert region.get("needs_review") is not True
    # 대조 근거는 남긴다.
    assert region["text_candidates"]["vlm_reading"] == "가입금액 100만원 이상"


def test_disagreement_keeps_ocr_but_flags_review():
    """좌표가 있는 쪽이 OCR 이므로 정본은 바꾸지 않는다."""
    region = {"bbox": [0, 0, 10, 10],
              "text": "※상환능력에비해신용카드사용액이과도할경우,귀하의개인신용평점이그을V",
              "text_source": "paddlex_block_content"}

    status = reading.apply_reading(region, {
        "text": "※ 상환능력에 비해 신용카드 사용액이 과도할 경우, "
                "귀하의 개인신용평점이 하락할 수 있습니다.",
        "confidence": 0.9,
    })

    assert status == "disagree"
    assert region["text"].startswith("※상환능력에")
    assert region["text_source"] == "paddlex_block_content"
    assert region["needs_review"] is True
    assert region["vlm_reading"]["agreement"] < reading.AGREE


def test_vlm_only_text_becomes_canonical_with_a_coarser_bbox():
    """OCR 이 못 읽은 디자인 문구는 VLM 판독만이 유일한 텍스트다."""
    region = {"bbox": [0, 0, 10, 10], "text": "", "text_source": "empty",
              "bbox_quality": "exact"}

    status = reading.apply_reading(region, {"text": "NH농협은행", "confidence": 0.9})

    assert status == "vlm_only"
    assert region["text"] == "NH농협은행"
    assert region["text_source"] == "vlm_only"
    # 줄 단위 좌표가 없으므로 품질을 낮춰 표시한다.
    assert region["bbox_quality"] == "region"
    assert region["needs_review"] is True


def test_blank_reading_never_erases_the_ocr_text():
    region = {"bbox": [0, 0, 10, 10], "text": "기본금리 연 2.25%",
              "text_source": "digital_ocr_lines"}

    status = reading.apply_reading(region, {"text": "", "confidence": 0.0})

    assert status == "vlm_blank"
    assert region["text"] == "기본금리 연 2.25%"
    assert region["text_source"] == "digital_ocr_lines"


def test_table_regions_are_never_re_read():
    """셀 배치 단계에서 같은 crop 을 이미 봤다. 평문으로 다시 읽으면 무조건 불일치다."""
    table = {"bbox": [0, 0, 10, 10], "kind": "table", "text": "| 가 | 나 |",
             "table": {"grid": {"rows": 1, "cols": 2}}}

    assert reading.should_read(table, "all") is False
    assert reading.should_read(table, "targeted") is False


def test_targeted_scope_picks_only_suspicious_regions():
    broken = {"bbox": [0, 0, 10, 10], "text": ")", "lines": [{"text": ")"}]}
    conflict = {"bbox": [0, 0, 10, 10], "text": "긴 본문입니다 " * 3,
                "text_selection_status": "conflict_pending_vlm", "lines": [{}]}
    healthy = {"bbox": [0, 0, 10, 10], "text": "가입금액 100만원 이상",
               "text_selection_status": "sources_agree", "lines": [{}]}

    assert reading.should_read(broken, "targeted") is True
    assert reading.should_read(conflict, "targeted") is True
    assert reading.should_read(healthy, "targeted") is False
    # all 은 전부 본다.
    assert reading.should_read(healthy, "all") is True
    assert reading.should_read(healthy, "off") is False


def test_a_failed_reading_does_not_stop_the_page():
    class Boom:
        width = height = 100
        def crop(self, box):
            raise RuntimeError("crop 실패")

    page = {"regions": [
        {"region_id": "r1", "bbox": [0, 0, 50, 50], "text": "원본 유지"},
    ]}

    stats = reading.read_page(page, Boom(), scope="all")

    assert stats["failed"] == 1
    assert page["regions"][0]["text"] == "원본 유지"
    assert page["regions"][0]["needs_review"] is True


def test_whitespace_only_differences_never_count_as_disagreement():
    """공백을 지우고 비교하므로 줄바꿈·띄어쓰기 차이는 잡음이 되지 않는다."""
    assert reading.agreement("가입금액100만원이상(원단위)",
                             "가입금액 100만원 이상 (원 단위)") == 1.0
    assert reading.agreement("대출한도\n최대 3억원 이내",
                             "대출한도 최대 3억원 이내") == 1.0


def test_a_single_digit_difference_is_flagged():
    """금리 숫자 한 자 차이는 광고 심의에서 가장 크게 문제 되는 종류다."""
    region = {"bbox": [0, 0, 10, 10], "text": "기본금리 연 2.25%",
              "text_source": "digital_ocr_lines"}

    status = reading.apply_reading(region, {"text": "기본금리 연 2.26%", "confidence": 0.9})

    assert status == "disagree"
    assert region["needs_review"] is True
    # 정본은 여전히 OCR 이고 VLM 판독은 후보로만 남는다.
    assert region["text"] == "기본금리 연 2.25%"
    assert region["text_candidates"]["vlm_reading"] == "기본금리 연 2.26%"
