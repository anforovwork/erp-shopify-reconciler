from datetime import timedelta
from decimal import Decimal

import pytest
from builders import AS_OF, day, item, options, order, price, sales_order, stock_bin, variant

from reconciler.models import (
    CheckName,
    DiscrepancyType,
    ErpItem,
    ErpItemPrice,
    ShopifyOrder,
    ShopifyShop,
    System,
)
from reconciler.reconcile import (
    ConfigurationError,
    check_missing,
    check_orders,
    check_prices,
    check_sku_quality,
    check_stock,
    index_erp_items,
    index_shopify_variants,
    is_in_scope,
    match_products,
    normalize_sku,
    validate_currencies,
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


# --- Prices --------------------------------------------------------------------------------


def _price_findings(
    shopify_price: str,
    erp_prices: list,
    taxes_included: bool = False,
    **option_overrides: object,
):
    shopify = index_shopify_variants([variant("SKU-1", price=Decimal(shopify_price))])
    erp = index_erp_items([item("SKU-1")], options())
    shop = ShopifyShop(currency="CAD", taxes_included=taxes_included)
    return check_prices(match_products(shopify, erp), erp_prices, shop, options(**option_overrides))


def test_equal_prices_match() -> None:
    findings = _price_findings("10.00", [price("SKU-1", "10.00")])

    assert findings.discrepancies == []
    assert findings.skipped == []


@pytest.mark.parametrize(
    ("shopify_price", "erp_price", "mismatch"),
    [
        ("10.00", "10.01", False),  # exactly 0.01 is acceptable
        ("10.01", "10.00", False),
        ("10.00", "10.011", True),  # strictly greater than 0.01
        ("10.00", "9.98", True),
        ("19.99", "21.10", True),
    ],
)
def test_price_tolerance_is_strictly_greater_than_one_cent(
    shopify_price: str, erp_price: str, mismatch: bool
) -> None:
    findings = _price_findings(shopify_price, [price("SKU-1", erp_price)])

    assert bool(findings.discrepancies) is mismatch
    if mismatch:
        found = findings.discrepancies[0]
        assert found.type is DiscrepancyType.PRICE_MISMATCH
        assert (found.shopify_value, found.erp_value) == (
            Decimal(shopify_price),
            Decimal(erp_price),
        )
        assert found.source_of_truth is System.ERPNEXT


@pytest.mark.parametrize(
    "not_applicable",
    [
        price("SKU-1", "99.00", price_list="Wholesale"),
        price("SKU-1", "99.00", customer="TEST-CUSTOMER"),
        price("SKU-1", "99.00", batch_no="TEST-BATCH"),
        price("SKU-1", "99.00", uom="Box"),
        price("SKU-1", "99.00", uom=None),
        price("SKU-1", "99.00", valid_from=day(1)),
        price("SKU-1", "99.00", valid_upto=day(-1)),
    ],
)
def test_non_applicable_price_rows_are_ignored(not_applicable: ErpItemPrice) -> None:
    findings = _price_findings("10.00", [price("SKU-1", "10.00"), not_applicable])

    assert findings.discrepancies == []


def test_validity_dates_are_inclusive() -> None:
    findings = _price_findings(
        "10.00", [price("SKU-1", "10.00", valid_from=day(0), valid_upto=day(0))]
    )

    assert findings.discrepancies == []


def test_no_applicable_price_is_price_missing() -> None:
    findings = _price_findings("10.00", [price("SKU-1", "10.00", customer="TEST-CUSTOMER")])

    assert [d.type for d in findings.discrepancies] == [DiscrepancyType.PRICE_MISSING]


def test_several_applicable_prices_are_ambiguous() -> None:
    findings = _price_findings("10.00", [price("SKU-1", "10.00"), price("SKU-1", "12.00")])

    [found] = findings.discrepancies
    assert found.type is DiscrepancyType.PRICE_AMBIGUOUS
    assert found.detail == "2 applicable price rows"


@pytest.mark.parametrize(
    ("erp_prices_include_tax", "taxes_included"),
    [(None, False), (True, False), (False, True)],
)
def test_price_comparison_unsupported_when_tax_treatment_unconfirmed(
    erp_prices_include_tax: bool | None, taxes_included: bool
) -> None:
    findings = _price_findings(
        "10.00",
        [price("SKU-1", "15.00")],
        taxes_included=taxes_included,
        erp_prices_include_tax=erp_prices_include_tax,
    )

    assert findings.discrepancies == []  # no false PRICE_MISMATCH
    [skipped] = findings.skipped
    assert (skipped.check, skipped.key) == (CheckName.PRICE, None)


def test_tax_included_on_both_sides_is_compared() -> None:
    findings = _price_findings(
        "10.00", [price("SKU-1", "15.00")], taxes_included=True, erp_prices_include_tax=True
    )

    assert [d.type for d in findings.discrepancies] == [DiscrepancyType.PRICE_MISMATCH]


def test_missing_price_is_still_reported_when_comparison_unsupported() -> None:
    findings = _price_findings("10.00", [], erp_prices_include_tax=None)

    assert [d.type for d in findings.discrepancies] == [DiscrepancyType.PRICE_MISSING]


def test_matching_currencies_are_valid() -> None:
    validate_currencies("CAD", "CAD", "CAD")


@pytest.mark.parametrize(("shop", "price_list"), [("USD", "CAD"), ("CAD", "USD"), ("cad", "CAD")])
def test_currency_mismatch_is_a_configuration_error(shop: str, price_list: str) -> None:
    with pytest.raises(ConfigurationError, match="Currency mismatch"):
        validate_currencies(shop, price_list, "CAD")


# --- Stock ---------------------------------------------------------------------------------


def _stock_findings(erp_bins: list, erp_item: ErpItem | None = None, **variant_fields: object):
    shopify = index_shopify_variants([variant("SKU-1", **variant_fields)])
    erp = index_erp_items([erp_item or item("SKU-1")], options())
    return check_stock(match_products(shopify, erp), erp_bins, options())


def test_equal_stock_matches() -> None:
    findings = _stock_findings([stock_bin("SKU-1", "5")], on_hand=5)

    assert findings.discrepancies == []
    assert findings.skipped == []


def test_stock_difference_is_a_mismatch() -> None:
    findings = _stock_findings([stock_bin("SKU-1", "3")], on_hand=5)

    [found] = findings.discrepancies
    assert found.type is DiscrepancyType.STOCK_MISMATCH
    assert (found.shopify_value, found.erp_value) == (Decimal(5), Decimal(3))
    assert found.source_of_truth is System.ERPNEXT


def test_whole_erp_quantity_with_decimals_is_not_fractional() -> None:
    findings = _stock_findings([stock_bin("SKU-1", "5.000")], on_hand=5)

    assert findings.discrepancies == []
    assert findings.skipped == []


def test_missing_bin_means_zero_stock() -> None:
    assert _stock_findings([], on_hand=0).discrepancies == []
    assert [d.erp_value for d in _stock_findings([], on_hand=2).discrepancies] == [Decimal(0)]


def test_bins_in_other_warehouses_are_ignored() -> None:
    findings = _stock_findings(
        [stock_bin("SKU-1", "99", warehouse="Other - TC"), stock_bin("SKU-1", "5")], on_hand=5
    )

    assert findings.discrepancies == []


@pytest.mark.parametrize(
    ("erp_bins", "erp_item", "variant_fields", "reason_start"),
    [
        ([stock_bin("SKU-1", "1")], None, {"inventory_tracked": False}, "Inventory is not tracked"),
        ([stock_bin("SKU-1", "1")], None, {"on_hand": None}, "Variant is not stocked"),
        ([stock_bin("SKU-1", "1")], item("SKU-1", stock_uom="Kg"), {}, "ERPNext stock UOM"),
        ([stock_bin("SKU-1", "2.5")], None, {}, "ERPNext quantity is fractional"),
    ],
)
def test_incomparable_stock_is_skipped_with_reason(
    erp_bins: list,
    erp_item: ErpItem | None,
    variant_fields: dict[str, object],
    reason_start: str,
) -> None:
    findings = _stock_findings(erp_bins, erp_item, **variant_fields)

    assert findings.discrepancies == []
    [skipped] = findings.skipped
    assert (skipped.check, skipped.key) == (CheckName.STOCK, "SKU-1")
    assert skipped.reason.startswith(reason_start)


# --- Orders --------------------------------------------------------------------------------


def _order_findings(orders: list, sales_orders: list | None):
    return check_orders(orders, sales_orders, options())


def test_order_with_submitted_sales_order_matches() -> None:
    assert _order_findings([order(1)], [sales_order(1)]).discrepancies == []


def test_order_without_sales_order_is_not_in_erp() -> None:
    findings = _order_findings([order(1), order(2)], [sales_order(1)])

    [found] = findings.discrepancies
    assert (found.type, found.key) == (DiscrepancyType.ORDER_NOT_IN_ERP, "gid://shopify/Order/2")
    assert found.source_of_truth is System.SHOPIFY
    assert found.detail == ""


@pytest.mark.parametrize(("docstatus", "detail"), [(0, "draft"), (2, "cancelled")])
def test_only_submitted_sales_orders_count(docstatus: int, detail: str) -> None:
    findings = _order_findings([order(1)], [sales_order(1, docstatus=docstatus)])

    [found] = findings.discrepancies
    assert found.type is DiscrepancyType.ORDER_NOT_IN_ERP
    assert found.detail == f"only {detail} Sales Order(s) found"


def test_mapping_field_is_trimmed() -> None:
    erp = [sales_order(1, shopify_order_id="  gid://shopify/Order/1 ")]

    assert _order_findings([order(1)], erp).discrepancies == []


@pytest.mark.parametrize(
    "ineligible",
    [
        order(1, financial_status="PENDING"),
        order(1, financial_status="PARTIALLY_REFUNDED"),
        order(1, cancelled=True),
        order(1, test=True),
        order(1, created_at=AS_OF - timedelta(days=30, seconds=1)),
    ],
)
def test_ineligible_orders_are_not_checked(ineligible: ShopifyOrder) -> None:
    assert _order_findings([ineligible], []).discrepancies == []


def test_order_exactly_at_lookback_boundary_is_checked() -> None:
    boundary = order(1, created_at=AS_OF - timedelta(days=30))

    assert len(_order_findings([boundary], []).discrepancies) == 1


def test_missing_mapping_field_skips_orders_with_warning() -> None:
    findings = _order_findings([order(1)], None)

    assert findings.discrepancies == []
    [skipped] = findings.skipped
    assert (skipped.check, skipped.key) == (CheckName.ORDER, None)
    assert findings.warnings == [skipped.reason]
