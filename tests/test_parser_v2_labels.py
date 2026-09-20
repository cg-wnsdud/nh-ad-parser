"""한 영역에 구분값이 여럿일 때 라벨을 여러 개 붙이는 경로를 검증한다.

영역을 쪼개지 않는다. 구간 bbox 는 그 구간이 가진 OCR 줄의 합집합이라, 쪼갰을
때와 같은 좌표를 얻으면서 원본 영역과 ID 를 잃지 않는다.
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


full_pipeline = _module("parser_v2_full_pipeline_labels", "full_pipeline.py")
export_v2 = _module("parser_v2_export_labels", "export_v2.py")


def _mixed_region():
    """`1. 예금성상품(거치식)` p1_r001 — 가입대상과 가입금액이 한 영역에 있다."""
    return {
        "region_id": "p1_r001",
        "bbox": [254, 1357, 641, 1531],
        "bbox_source": "paddlex_layout",
        "product_id": "product_1",
        "origin": "paddlex",
        "text": "가입대상 개인\n가입금액 100만원 이상\n(원 단위)",
        "semantic_label": "가입대상",
        "mixed_gubun": True,
        "lines": [
            {"line_ref": "p1/p1_r001/L000", "bbox": [263, 1365, 501, 1399],
             "text": "가입대상 개인"},
            {"line_ref": "p1/p1_r001/L001", "bbox": [263, 1448, 646, 1485],
             "text": "가입금액 100만원 이상"},
            {"line_ref": "p1/p1_r001/L002", "bbox": [429, 1491, 563, 1529],
             "text": "(원 단위)"},
        ],
    }


def _document(region):
    return {
        "doc_id": "d", "source_file": "s.pdf", "file_type": "pdf",
        "classification": {}, "template": {},
        "pages": [{"page_no": 1, "canvas": [1654, 2339],
                   "regions": [region], "unassigned_lines": []}],
    }


def _labels(*pairs):
    return [
        {"line_ref": f"p1/p1_r001/L{index:03d}", "label": label, "confidence": 0.9}
        for index, label in enumerate(pairs)
    ]


def test_spans_split_the_lines_without_touching_the_region():
    region = _mixed_region()

    # `(원 단위)` 는 앞줄에 딸린 단위라 같은 구간에 들어간다.
    spans = full_pipeline._label_spans_from_lines(
        region, _labels("가입대상", "가입금액", "가입금액"))

    assert [span["label"] for span in spans] == ["가입대상", "가입금액"]
    assert [span["span_id"] for span in spans] == ["p1_r001#01", "p1_r001#02"]
    assert spans[0]["line_refs"] == ["p1/p1_r001/L000"]
    assert spans[1]["line_refs"] == ["p1/p1_r001/L001", "p1/p1_r001/L002"]
    # 영역은 그대로다 — 이것이 쪼개기와의 차이다.
    assert region["region_id"] == "p1_r001"
    assert region["bbox"] == [254, 1357, 641, 1531]


def test_span_bbox_matches_what_splitting_would_have_produced():
    """쪼개서 얻는 정밀도가 없다는 것이 이 방식을 고른 이유다."""
    region = _mixed_region()

    spans = full_pipeline._label_spans_from_lines(
        region, _labels("가입대상", "가입금액", "가입금액"))

    assert spans[0]["bbox"] == [263, 1365, 501, 1399]
    assert spans[1]["bbox"] == [263, 1448, 646, 1529]
    assert all(span["bbox_source"] == "ocr_pdf_lines" for span in spans)


def test_repeated_label_is_not_merged_across_a_gap():
    """떨어져 있는 같은 라벨을 합치면 사이의 다른 항목까지 bbox 에 들어간다."""
    region = _mixed_region()

    spans = full_pipeline._label_spans_from_lines(
        region, _labels("가입금액", "가입대상", "가입금액"))

    assert [span["label"] for span in spans] == ["가입금액", "가입대상", "가입금액"]
    assert spans[0]["bbox"] != spans[2]["bbox"]


def test_unlabelled_run_is_kept_as_a_span():
    """어디가 미분류인지 보여야 광고 문구인지 빠뜨린 항목인지 판단할 수 있다."""
    region = _mixed_region()

    spans = full_pipeline._label_spans_from_lines(
        region, _labels("가입대상", None, None))

    assert [span["label"] for span in spans] == ["가입대상", None]
    assert spans[1]["line_refs"] == ["p1/p1_r001/L001", "p1/p1_r001/L002"]
    assert spans[1]["bbox"] == [263, 1448, 646, 1529]


def test_p3_exposes_every_label_and_keeps_the_region_addressable():
    region = _mixed_region()
    region["label_spans"] = full_pipeline._label_spans_from_lines(
        region, _labels("가입대상", "가입금액", "가입금액"))
    region["semantic_label"] = "가입금액"
    p3 = export_v2.build_p3(export_v2.build_p1(_document(region)))

    out = p3["pages"][0]["regions"][0]
    assert out["region_id"] == "p1_r001"
    assert [entry["label"] for entry in out["labels"]] == ["가입대상", "가입금액"]
    # 영역 전체와 구분값 하나를 모두 짚을 수 있다.
    index = p3["location_index"]
    assert index["p1_r001"]["bbox"] == [254, 1357, 641, 1531]
    assert index["p1_r001#01"]["bbox"] == [263, 1365, 501, 1399]
    assert index["p1_r001#02"]["bbox"] == [263, 1448, 646, 1529]
    assert index["p1_r001#02"]["region_id"] == "p1_r001"
    # 줄은 영역이 전부 소유한다 — 중복 없음.
    assert p3["summary"]["parser_primary_line_total"] == 3


def test_label_index_lists_every_span_not_just_the_representative():
    region = _mixed_region()
    region["label_spans"] = full_pipeline._label_spans_from_lines(
        region, _labels("가입대상", "가입금액", "가입금액"))
    region["semantic_label"] = "가입금액"
    p3 = export_v2.build_p3(export_v2.build_p1(_document(region)))

    found = {row["label"]: row["references"][0] for row in p3["label_index"]}
    assert set(found) == {"가입대상", "가입금액"}
    assert found["가입대상"]["span_id"] == "p1_r001#01"
    assert found["가입대상"]["bbox"] == [263, 1365, 501, 1399]


def test_region_without_line_labels_still_reports_one_entry():
    """줄 단위 라벨링을 거치지 않은 영역도 같은 모양으로 나온다."""
    region = _mixed_region()
    region.pop("mixed_gubun")
    region["semantic_label"] = "가입대상"
    p3 = export_v2.build_p3(export_v2.build_p1(_document(region)))

    entries = p3["pages"][0]["regions"][0]["labels"]
    assert len(entries) == 1
    assert entries[0]["span_id"] == "p1_r001#01"
    assert entries[0]["bbox"] == [254, 1357, 641, 1531]
    assert entries[0]["line_refs"] == [f"p1/p1_r001/L{i:03d}" for i in range(3)]


def test_multiple_gubun_alone_is_not_a_review_item():
    """구분값이 여럿인 것은 정상이다. 그러라고 라벨을 여러 개 붙인다.

    예전에는 `needs_split`(쪼개야 한다는 신호)이 그대로 검수로 이어져, 라벨을
    잘 붙인 영역이 전부 검수 목록에 들어갔다.
    """
    region = _mixed_region()
    region["label_spans"] = full_pipeline._label_spans_from_lines(
        region, _labels("가입대상", "가입금액", "가입금액"))
    region["needs_review"] = False

    p3 = export_v2.build_p3(export_v2.build_p1(_document(region)))

    out = p3["pages"][0]["regions"][0]
    assert len(out["labels"]) == 2
    assert out["needs_review"] is False
    assert out["selection_status"] == "parser_v2_selected"
    # 쪼개기 시절의 필드는 P3 에 더 이상 없다.
    assert "needs_split" not in out


def test_an_unlabelled_span_does_raise_review():
    region = _mixed_region()
    region["label_spans"] = full_pipeline._label_spans_from_lines(
        region, _labels("가입대상", None, None))
    region["needs_review"] = True   # _label_region_lines 가 켜는 조건

    p3 = export_v2.build_p3(export_v2.build_p1(_document(region)))

    out = p3["pages"][0]["regions"][0]
    assert out["needs_review"] is True
    assert [entry["label"] for entry in out["labels"]] == ["가입대상", None]
