"""구분값이 섞인 Region 을 줄 라벨로 쪼개는 경로를 검증한다.

줄 소유권이 정확히 한 곳에만 남아야 P3 계약이 유지된다.
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


full_pipeline = _module("parser_v2_full_pipeline_split", "full_pipeline.py")
export_v2 = _module("parser_v2_export_split", "export_v2.py")


def _mixed_region():
    """`1. 예금성상품(거치식)` p1_r001 — 가입대상과 가입금액이 한 영역에 있다."""
    return {
        "region_id": "p1_r001",
        "bbox": [254, 1357, 641, 1531],
        "product_id": "product_1",
        "origin": "paddlex",
        "text": "가입대상개인\n가입금액100만원 이상\n(원 단위)",
        "semantic_label": "가입대상",
        "needs_split": True,
        "lines": [
            {"line_ref": "p1/p1_r001/L000", "bbox": [254, 1357, 500, 1400],
             "text": "가입대상개인"},
            {"line_ref": "p1/p1_r001/L001", "bbox": [254, 1410, 641, 1470],
             "text": "가입금액100만원 이상"},
            {"line_ref": "p1/p1_r001/L002", "bbox": [254, 1480, 420, 1531],
             "text": "(원 단위)"},
        ],
    }


def test_split_keeps_each_line_in_exactly_one_child():
    region = _mixed_region()
    labels = [
        {"line_ref": "p1/p1_r001/L000", "label": "가입대상", "confidence": 0.9},
        {"line_ref": "p1/p1_r001/L001", "label": "가입금액", "confidence": 0.9},
        # 앞줄에 딸린 단위는 같은 라벨이라 같은 자식에 들어간다.
        {"line_ref": "p1/p1_r001/L002", "label": "가입금액", "confidence": 0.9},
    ]

    children = full_pipeline._children_from_line_labels(region, labels)

    assert [c["region_id"] for c in children] == ["p1_r001s01", "p1_r001s02"]
    assert [c["semantic_label"] for c in children] == ["가입대상", "가입금액"]
    assert children[0]["text"] == "가입대상개인"
    assert children[1]["text"] == "가입금액100만원 이상\n(원 단위)"
    # 줄은 정확히 한 자식에만 있다.
    refs = [r for c in children for line in c["lines"] for r in [line["line_ref"]]]
    assert sorted(refs) == [f"p1/p1_r001/L{i:03d}" for i in range(3)]
    assert len(refs) == len(set(refs))
    # 부모를 가리키는 연결은 남긴다.
    assert all(c["parent_id"] == "p1_r001" for c in children)


def test_child_bbox_is_the_union_of_its_own_lines():
    region = _mixed_region()
    labels = [
        {"line_ref": "p1/p1_r001/L000", "label": "가입대상", "confidence": 0.9},
        {"line_ref": "p1/p1_r001/L001", "label": "가입금액", "confidence": 0.9},
        {"line_ref": "p1/p1_r001/L002", "label": "가입금액", "confidence": 0.9},
    ]

    children = full_pipeline._children_from_line_labels(region, labels)

    assert children[0]["bbox"] == [254, 1357, 500, 1400]
    assert children[1]["bbox"] == [254, 1410, 641, 1531]
    assert all(c["bbox_source"] == "ocr_pdf_lines" for c in children)


def test_same_label_on_every_line_is_not_split():
    """실제로는 한 구분값뿐이면 쪼개지 않고 그 라벨만 반영한다."""
    region = _mixed_region()
    labels = [
        {"line_ref": f"p1/p1_r001/L{i:03d}", "label": "가입금액", "confidence": 0.9}
        for i in range(3)
    ]

    children = full_pipeline._children_from_line_labels(region, labels)

    assert len(children) == 1
    assert children[0]["region_id"] == "p1_r001"
    assert children[0]["semantic_label"] == "가입금액"
    assert children[0]["needs_split"] is False


def test_repeated_label_is_not_merged_across_a_gap():
    """떨어져 있는 같은 라벨을 합치면 사이의 다른 항목까지 bbox 에 들어간다."""
    region = _mixed_region()
    labels = [
        {"line_ref": "p1/p1_r001/L000", "label": "가입금액", "confidence": 0.9},
        {"line_ref": "p1/p1_r001/L001", "label": "가입대상", "confidence": 0.9},
        {"line_ref": "p1/p1_r001/L002", "label": "가입금액", "confidence": 0.9},
    ]

    children = full_pipeline._children_from_line_labels(region, labels)

    assert [c["semantic_label"] for c in children] == ["가입금액", "가입대상", "가입금액"]
    assert children[0]["bbox"] != children[2]["bbox"]


def test_unlabelled_child_is_marked_for_review():
    region = _mixed_region()
    labels = [
        {"line_ref": "p1/p1_r001/L000", "label": "가입대상", "confidence": 0.9},
        {"line_ref": "p1/p1_r001/L001", "label": None, "confidence": 0.0},
        {"line_ref": "p1/p1_r001/L002", "label": None, "confidence": 0.0},
    ]

    children = full_pipeline._children_from_line_labels(region, labels)

    assert children[0]["needs_review"] is False
    assert children[1]["needs_review"] is True


def test_p3_still_owns_every_line_exactly_once_after_a_split():
    region = _mixed_region()
    labels = [
        {"line_ref": "p1/p1_r001/L000", "label": "가입대상", "confidence": 0.9},
        {"line_ref": "p1/p1_r001/L001", "label": "가입금액", "confidence": 0.9},
        {"line_ref": "p1/p1_r001/L002", "label": "가입금액", "confidence": 0.9},
    ]
    children = full_pipeline._children_from_line_labels(region, labels)
    document = {
        "doc_id": "d", "source_file": "s.pdf", "file_type": "pdf",
        "classification": {}, "template": {},
        "pages": [{"page_no": 1, "canvas": [1654, 2339],
                   "regions": children, "unassigned_lines": []}],
    }

    # 중복 소유가 있으면 build_p3 가 ValueError 로 막는다.
    p3 = export_v2.build_p3(export_v2.build_p1(document))

    assert p3["summary"]["parser_primary_line_total"] == 3
    assert {r["region_id"] for r in p3["pages"][0]["regions"]} == {
        "p1_r001s01", "p1_r001s02"}
    assert p3["location_index"]["p1_r001s02"]["bbox"] == [254, 1410, 641, 1531]
