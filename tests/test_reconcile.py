import pytest
from builders import item, options, variant

from reconciler.models import DiscrepancyType, ErpItem
from reconciler.reconcile import (
    check_missing,
    check_sku_quality,
    index_erp_items,
    index_shopify_variants,
    is_in_scope,
    match_products,
    normalize_sku,
)


def _types(discrepancies: list) -> list[tuple[DiscrepancyType, str]]:
    return [(d.type, d.key) for d in discrepancies]


# --- SKU matching and scope ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  ABC-1 ", "ABC-1"), ("abc-1", "abc-1"), ("\tA B\n", "A B"), (None, ""), ("   ", "")],
)
def test_normalize_sku_trims_but_keeps_case(raw: str | None, expected: str) -> None:
    assert normalize_sku(raw) == expected


@pytest.mark.parametrize(
    "out_of_scope",
    [
        item("X", disabled=True),
        item("X", is_sales_item=False),
        item("X", is_stock_item=False),  # service
        item("X", has_variants=True),  # variant template
        item("X", item_group="Raw Material"),
    ],
)
def test_items_outside_scope_are_excluded(out_of_scope: ErpItem) -> None:
    assert not is_in_scope(out_of_scope, options())


def test_enabled_sales_stock_item_in_configured_group_is_in_scope() -> None:
    assert is_in_scope(item("X"), options())


def test_matching_is_exact_after_trim_and_case_sensitive() -> None:
    shopify = index_shopify_variants([variant(" SKU-1 ", 1), variant("sku-2", 2)])
    erp = index_erp_items([item("SKU-1"), item("SKU-2")], options())

    assert list(match_products(shopify, erp)) == ["SKU-1"]
    assert _types(check_missing(shopify, erp)) == [
        (DiscrepancyType.MISSING_IN_SHOPIFY, "SKU-2"),
        (DiscrepancyType.MISSING_IN_ERP, "sku-2"),
    ]


# --- Data quality: empty and duplicate SKUs ------------------------------------------------


def test_clean_skus_have_no_quality_findings() -> None:
    shopify = index_shopify_variants([variant("SKU-1", 1), variant("SKU-2", 2)])
    erp = index_erp_items([item("SKU-1"), item("SKU-2")], options())

    assert check_sku_quality(shopify, erp) == []


def test_empty_shopify_sku_is_reported_by_variant_id() -> None:
    shopify = index_shopify_variants([variant(None, 7), variant("  ", 8)])
    erp = index_erp_items([], options())

    assert _types(check_sku_quality(shopify, erp)) == [
        (DiscrepancyType.SKU_EMPTY, "gid://shopify/ProductVariant/7"),
        (DiscrepancyType.SKU_EMPTY, "gid://shopify/ProductVariant/8"),
    ]


def test_duplicate_skus_are_reported_per_system() -> None:
    shopify = index_shopify_variants([variant("SKU-1", 1), variant("SKU-1 ", 2)])
    erp = index_erp_items([item("SKU-2"), item(" SKU-2")], options())

    findings = check_sku_quality(shopify, erp)

    assert _types(findings) == [
        (DiscrepancyType.SKU_DUPLICATE, "SKU-1"),
        (DiscrepancyType.SKU_DUPLICATE, "SKU-2"),
    ]
    assert findings[0].detail == "appears 2 times in Shopify"
    assert findings[1].detail == "appears 2 times in ERPNext"


def test_duplicated_sku_gets_no_match_and_no_missing_finding() -> None:
    shopify = index_shopify_variants([variant("SKU-1", 1), variant("SKU-1", 2)])
    erp = index_erp_items([item("SKU-1")], options())

    assert match_products(shopify, erp) == {}
    assert check_missing(shopify, erp) == []


# --- MISSING_IN_SHOPIFY / MISSING_IN_ERP ---------------------------------------------------


def test_products_on_both_sides_are_not_missing() -> None:
    shopify = index_shopify_variants([variant("SKU-1")])
    erp = index_erp_items([item("SKU-1")], options())

    assert check_missing(shopify, erp) == []


def test_missing_on_each_side_is_reported_sorted() -> None:
    shopify = index_shopify_variants([variant("SKU-3", 1), variant("SKU-1", 2)])
    erp = index_erp_items([item("SKU-2"), item("SKU-1"), item("SKU-0")], options())

    assert _types(check_missing(shopify, erp)) == [
        (DiscrepancyType.MISSING_IN_SHOPIFY, "SKU-0"),
        (DiscrepancyType.MISSING_IN_SHOPIFY, "SKU-2"),
        (DiscrepancyType.MISSING_IN_ERP, "SKU-3"),
    ]


def test_out_of_scope_erp_item_is_never_missing_in_shopify() -> None:
    shopify = index_shopify_variants([])
    erp = index_erp_items([item("RAW-1", item_group="Raw Material")], options())

    assert check_missing(shopify, erp) == []


def test_shopify_sku_whose_erp_item_is_out_of_scope_is_missing_in_erp() -> None:
    shopify = index_shopify_variants([variant("SKU-1")])
    erp = index_erp_items([item("SKU-1", disabled=True)], options())

    assert _types(check_missing(shopify, erp)) == [(DiscrepancyType.MISSING_IN_ERP, "SKU-1")]
