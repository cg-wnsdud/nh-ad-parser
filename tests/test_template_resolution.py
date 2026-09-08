from nh_parser.review import resolution as tr


def _doc(**values) -> dict:
    return {
        "source_file": values.pop("source_file", "광고.pdf"),
        "product_group": values.pop("product_group", None),
        "product_name_shown": values.pop("product_name_shown", None),
        "pages": [{"regions": [{"lines": [{"text": values.pop("text", "")}]}]}],
        **values,
    }


def test_적금_힌트로_적립식_템플릿을_규칙_확정한다() -> None:
    result = tr.resolve_template(_doc(
        source_file="NH올원e적금.png", product_group="예금성",
        product_name_shown="노출", text="가입기간 12개월",
    ))

    assert result["template_id"] == "예금성상품-적립식"
    assert result["source"] == "rules"
    assert result["status"] == "confirmed"


def test_카드_개인법인_불명확하면_허용후보_안에서_vlm이_선택한다(monkeypatch) -> None:
    def fake_chat(parts, *, schema_name, schema, max_tokens):
        assert schema_name == "ad_template_resolution"
        assert "카드상품-상품명(개인) 노출" in schema["properties"]["template_id"]["enum"]
        return {
            "analysis": "개인 카드 상품",
            "template_id": "카드상품-상품명(개인) 노출",
            "confidence": 0.91,
            "reason": "법인 이용 문구가 없음",
        }

    monkeypatch.setattr(tr.vlm_client, "chat_json", fake_chat)
    result = tr.resolve_template(_doc(
        source_file="새 카드.pdf", product_group="카드",
        product_name_shown="노출", text="연회비와 포인트 적립 혜택",
    ))

    assert result["template_id"] == "카드상품-상품명(개인) 노출"
    assert result["source"] == "vlm"


def test_투자성_펀드가_새_템플릿_범위에_포함된다() -> None:
    result = tr.resolve_template(_doc(
        source_file="투자성 펀드 광고.pdf", product_group="투자성",
        product_name_shown="노출", text="집합투자증권은 예금자보호법에 따라 보호되지 않습니다.",
    ))

    assert result["template_id"] == "투자성상품-펀드"
