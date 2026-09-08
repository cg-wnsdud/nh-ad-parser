from nh_parser.review import labeling


def _document() -> dict:
    return {
        "pages": [{
            "page_no": 1,
            "regions": [{
                "region_id": "p1_r000",
                "label": "text",
                "layout_score": 0.9,
                "role": "본문",
                "bbox": [0, 0, 100, 80],
                "is_illustrative": False,
                "lines": [
                    {"text": "가입기간 : 12개월"},
                    {"text": "매월 30만원 납입"},
                ],
            }],
        }],
    }


def test_vlm_span과_명시표제를_병합하고_잘못된_표제라벨은_제거한다(monkeypatch) -> None:
    def fake_chat(parts, *, schema_name, schema, max_tokens):
        assert schema_name == "ad_template_region_labels"
        allowed = schema["properties"]["verdicts"]["items"]["properties"]["gubun"]["enum"]
        assert "가입금액" in allowed
        assert "대출한도" not in allowed
        return {"verdicts": [{
            "region_id": "p1_r000",
            "line_from": 0,
            "line_to": 1,
            "gubun": "가입금액",
            "confidence": 0.8,
            "reason": "기간과 납입액 영역",
        }]}

    monkeypatch.setattr(labeling.vlm_client, "chat_json", fake_chat)
    result = labeling.label_regions(
        _document(), {"template_id": "예금성상품-적립식"},
    )
    labels = {entry["label"]: entry for entry in result["by_region"]["p1_r000"]}

    assert labels["가입기간"]["spans"][0]["line_from"] == 0
    assert labels["가입기간"]["spans"][0]["sources"] == ["explicit_heading"]
    assert labels["가입금액"]["spans"][0]["line_from"] == 1


def test_템플릿_미확정이면_원문을_건드리지_않고_라벨링을_건너뛴다() -> None:
    result = labeling.label_regions(_document(), {"template_id": None})

    assert result["status"] == "skipped"
    assert result["by_region"] == {}
