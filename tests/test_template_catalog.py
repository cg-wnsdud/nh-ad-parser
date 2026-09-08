from nh_parser.review.catalog import load_catalog, template_item_map


def test_확정된_hwpx_19개_템플릿을_싣는다() -> None:
    catalog = load_catalog()

    assert catalog["version"] == "nh-ad-template-catalog-v1"
    assert len(catalog["templates"]) == 19
    assert "투자성상품-펀드" in catalog["templates"]
    assert "대출금리" in template_item_map("대출성상품-상품명 노출", catalog)


def test_한_템플릿의_구분값은_중복되지_않는다() -> None:
    for template in load_catalog()["templates"].values():
        labels = [item["gubun"] for item in template["items"]]
        assert len(labels) == len(set(labels))
