from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from builders import AS_OF, day, item, options, order, price, sales_order, stock_bin, variant
from pydantic import ValidationError

from reconciler.models import (
    CheckName,
    DiscrepancyType,
    ErpItem,
    ErpItemPrice,
    ShopifyOrder,
    ShopifyShop,
    ShopifyVariant,
    System,
)
from reconciler.reconcile import (
    ConfigurationError,
    Findings,
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
    out_of_scope_reasons,
    scope_exclusions,
    validate_currencies,
)


def _types(discrepancies: list) -> list[tuple[DiscrepancyType, str]]:
    return [(d.type, d.key) for d in discrepancies]


def _missing(variants: list[ShopifyVariant], items: list[ErpItem]) -> Findings:
    """Run check_missing exactly the way reconcile() wires it up."""
    opts = options()
    return check_missing(
        index_shopify_variants(variants),
        index_erp_items(items, opts),
        out_of_scope_reasons(items, opts),
    )


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
    findings = _missing(
        [variant(" SKU-1 ", 1), variant("sku-2", 2)], [item("SKU-1"), item("SKU-2")]
    )
    assert _types(findings.discrepancies) == [
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
    findings = _missing([variant("SKU-1", 1), variant("SKU-1", 2)], [item("SKU-1")])
    assert findings.discrepancies == []
    assert findings.skipped == []


# --- MISSING_IN_SHOPIFY / MISSING_IN_ERP / out of scope ------------------------------------


def test_products_on_both_sides_are_not_missing() -> None:
    findings = _missing([variant("SKU-1")], [item("SKU-1")])

    assert findings.discrepancies == []
    assert findings.skipped == []


def test_missing_on_each_side_is_reported_sorted() -> None:
    findings = _missing(
        [variant("SKU-3", 1), variant("SKU-1", 2)],
        [item("SKU-2"), item("SKU-1"), item("SKU-0")],
    )

    assert _types(findings.discrepancies) == [
        (DiscrepancyType.MISSING_IN_SHOPIFY, "SKU-0"),
        (DiscrepancyType.MISSING_IN_SHOPIFY, "SKU-2"),
        (DiscrepancyType.MISSING_IN_ERP, "SKU-3"),
    ]


def test_out_of_scope_erp_item_is_never_missing_in_shopify() -> None:
    findings = _missing([], [item("RAW-1", item_group="Raw Material")])

    assert findings.discrepancies == []
    assert findings.skipped == []


def test_shopify_sku_whose_erp_item_is_out_of_scope_is_skipped_with_reason() -> None:
    findings = _missing([variant("SKU-1")], [item("SKU-1", disabled=True, is_sales_item=False)])

    assert findings.discrepancies == []
    [skipped] = findings.skipped
    assert (skipped.check, skipped.key) == (CheckName.SCOPE, "SKU-1")
    assert skipped.reason == (
        "ERPNext item exists but is out of scope: disabled, not a sales item."
    )


def test_out_of_scope_lookup_uses_trimmed_codes() -> None:
    findings = _missing([variant("SKU-1")], [item(" SKU-1 ", item_group="Raw Material")])

    assert [(s.key, s.reason) for s in findings.skipped] == [
        (
            "SKU-1",
            "ERPNext item exists but is out of scope: item group not in ERPNEXT_ITEM_GROUPS.",
        )
    ]


@pytest.mark.parametrize(
    ("excluded", "reason"),
    [
        (item("X", disabled=True), "disabled"),
        (item("X", is_sales_item=False), "not a sales item"),
        (item("X", is_stock_item=False), "not a stock item"),
        (item("X", has_variants=True), "variant template"),
        (item("X", item_group="Raw Material"), "item group not in ERPNEXT_ITEM_GROUPS"),
    ],
)
def test_each_scope_exclusion_has_a_reason(excluded: ErpItem, reason: str) -> None:
    assert scope_exclusions(excluded, options()) == [reason]


# --- Prices --------------------------------------------------------------------------------


def _price_findings(
    shopify_price: str,
    erp_prices: list,
    taxes_included: bool = False,
    erp_item: ErpItem | None = None,
    **option_overrides: object,
) -> Findings:
    shopify = index_shopify_variants([variant("SKU-1", price=Decimal(shopify_price))])
    erp = index_erp_items([erp_item or item("SKU-1")], options())
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
        price("SKU-1", "99.00", supplier="TEST-SUPPLIER"),
        price("SKU-1", "99.00", batch_no="TEST-BATCH"),
        price("SKU-1", "99.00", uom="Box"),
        price("SKU-1", "99.00", packing_unit=6),  # pack price, not for one unit
        price("SKU-1", "99.00", valid_from=day(1)),
        price("SKU-1", "99.00", valid_upto=day(-1)),
    ],
)
def test_non_applicable_price_rows_are_ignored(not_applicable: ErpItemPrice) -> None:
    findings = _price_findings("10.00", [price("SKU-1", "10.00"), not_applicable])

    assert findings.discrepancies == []


@pytest.mark.parametrize(
    ("price_timezone", "applies"), [("America/Edmonton", True), ("UTC", False)]
)
def test_price_validity_uses_the_configured_timezone(price_timezone: str, applies: bool) -> None:
    # 03:00 UTC on Nov 2 is still the evening of Nov 1 in Edmonton (UTC-7).
    late_evening = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    last_day = date(2026, 11, 1)
    shopify = index_shopify_variants([variant("SKU-1")])
    opts = options(as_of=late_evening, price_timezone=price_timezone)
    erp = index_erp_items([item("SKU-1")], opts)
    shop = ShopifyShop(currency="CAD", taxes_included=False)

    findings = check_prices(
        match_products(shopify, erp), [price("SKU-1", valid_upto=last_day)], shop, opts
    )

    assert (findings.discrepancies == []) is applies


@pytest.mark.parametrize("days", [0, 61])
def test_order_lookback_must_be_between_1_and_60_days(days: int) -> None:
    with pytest.raises(ValidationError):
        options(order_lookback_days=days)


def test_validity_dates_are_inclusive() -> None:
    findings = _price_findings(
        "10.00", [price("SKU-1", "10.00", valid_from=day(0), valid_upto=day(0))]
    )

    assert findings.discrepancies == []


def test_no_applicable_price_is_price_missing() -> None:
    findings = _price_findings("10.00", [price("SKU-1", "10.00", customer="TEST-CUSTOMER")])

    assert [d.type for d in findings.discrepancies] == [DiscrepancyType.PRICE_MISSING]


# --- ERPNext price selection (reproduces ERPNext v16.53.0, see reconcile.py) ---------------


def _types_and_erp_values(findings: Findings) -> list[tuple[DiscrepancyType, Decimal | None]]:
    return [(d.type, d.erp_value) for d in findings.discrepancies]


def test_packing_unit_one_applies_to_a_single_unit() -> None:
    assert _price_findings("10.00", [price("SKU-1", "10.00", packing_unit=1)]).discrepancies == []


def test_only_pack_prices_means_price_missing() -> None:
    findings = _price_findings("10.00", [price("SKU-1", "10.00", packing_unit=6)])

    assert [d.type for d in findings.discrepancies] == [DiscrepancyType.PRICE_MISSING]


def test_newest_valid_from_wins() -> None:
    erp_prices = [
        price("SKU-1", "99.00", valid_from=day(-30)),
        price("SKU-1", "10.00", valid_from=day(-1)),
    ]

    assert _price_findings("10.00", erp_prices).discrepancies == []
    assert _types_and_erp_values(_price_findings("99.00", erp_prices)) == [
        (DiscrepancyType.PRICE_MISMATCH, Decimal("10.00"))
    ]


def test_dated_row_beats_row_without_valid_from() -> None:
    # MariaDB sorts NULL last in `valid_from DESC`.
    erp_prices = [price("SKU-1", "99.00", valid_from=None), price("SKU-1", "10.00", day(-400))]

    assert _price_findings("10.00", erp_prices).discrepancies == []


def test_price_valid_from_tomorrow_is_not_used_yet() -> None:
    erp_prices = [price("SKU-1", "10.00", day(-1)), price("SKU-1", "99.00", valid_from=day(1))]

    assert _price_findings("10.00", erp_prices).discrepancies == []


@pytest.mark.parametrize(
    "tied",
    [
        # Same valid_from and UOM; differing packing_unit (0 vs 1) passes ERPNext's duplicate check.
        [price("SKU-1", "10.00", day(-1)), price("SKU-1", "12.00", day(-1), packing_unit=1)],
        # Both without valid_from (legacy/imported data).
        [price("SKU-1", "10.00"), price("SKU-1", "12.00")],
        # valid_upto is not part of ERPNext's ORDER BY, so it does not break the tie.
        [price("SKU-1", "10.00", day(-1)), price("SKU-1", "12.00", day(-1), valid_upto=day(9))],
    ],
)
def test_rows_tied_on_valid_from_and_uom_are_ambiguous(tied: list[ErpItemPrice]) -> None:
    findings = _price_findings("50.00", tied)  # would mismatch either row if compared

    [found] = findings.discrepancies
    assert found.type is DiscrepancyType.PRICE_AMBIGUOUS
    assert found.erp_value is None
    assert found.detail.startswith("2 price rows tie on valid_from and UOM")


def test_identical_rates_still_tie() -> None:
    """Which row ERPNext picks is undefined, so the rule does not peek at the rates."""
    tied = [price("SKU-1", "10.00", day(-1)), price("SKU-1", "10.00", day(-1), packing_unit=1)]

    assert [d.type for d in _price_findings("10.00", tied).discrepancies] == [
        DiscrepancyType.PRICE_AMBIGUOUS
    ]


def test_tie_with_a_row_that_does_not_fit_one_unit_is_not_a_tie() -> None:
    erp_prices = [
        price("SKU-1", "10.00", day(-1)),
        price("SKU-1", "99.00", day(-1), packing_unit=6),
    ]

    assert _price_findings("10.00", erp_prices).discrepancies == []


def test_input_order_does_not_change_the_selection() -> None:
    """There is no creation-time tie-breaker in ERPNext; only the precedence key decides."""
    erp_prices = [price("SKU-1", "99.00", day(-30)), price("SKU-1", "10.00", day(-1))]

    for ordering in (erp_prices, list(reversed(erp_prices))):
        assert _price_findings("10.00", ordering).discrepancies == []


def test_other_uom_rows_are_ignored_even_when_newer() -> None:
    erp_prices = [price("SKU-1", "10.00", day(-10)), price("SKU-1", "99.00", day(-1), uom="Box")]

    assert _price_findings("10.00", erp_prices).discrepancies == []


@pytest.mark.parametrize("unset", [None, "", "  "])
def test_row_without_uom_is_flagged_and_still_used_by_erpnext(unset: str | None) -> None:
    findings = _price_findings("10.00", [price("SKU-1", "15.00", uom=unset)])

    assert _types_and_erp_values(findings) == [
        (DiscrepancyType.PRICE_UOM_MISSING, None),
        (DiscrepancyType.PRICE_MISMATCH, Decimal("15.00")),
    ]
    assert findings.discrepancies[0].detail.startswith("1 applicable price row(s) without UOM")


def test_stock_uom_row_beats_row_without_uom_on_equal_valid_from() -> None:
    # `uom DESC`: 'Nos' > '' > NULL.
    erp_prices = [price("SKU-1", "99.00", day(-1), uom=None), price("SKU-1", "10.00", day(-1))]

    assert _types_and_erp_values(_price_findings("10.00", erp_prices)) == [
        (DiscrepancyType.PRICE_UOM_MISSING, None)
    ]


def test_empty_uom_beats_null_uom_on_equal_valid_from() -> None:
    erp_prices = [
        price("SKU-1", "99.00", day(-1), uom=None),
        price("SKU-1", "10.00", day(-1), uom=""),
    ]

    assert _types_and_erp_values(_price_findings("10.00", erp_prices)) == [
        (DiscrepancyType.PRICE_UOM_MISSING, None)
    ]


def test_newer_row_without_uom_wins_over_older_stock_uom_row() -> None:
    erp_prices = [price("SKU-1", "10.00", day(-10)), price("SKU-1", "99.00", day(-1), uom=None)]

    assert _types_and_erp_values(_price_findings("10.00", erp_prices)) == [
        (DiscrepancyType.PRICE_UOM_MISSING, None),
        (DiscrepancyType.PRICE_MISMATCH, Decimal("99.00")),
    ]


def test_row_without_uom_that_does_not_otherwise_fit_is_ignored() -> None:
    erp_prices = [price("SKU-1", "10.00"), price("SKU-1", "5.00", uom=None, customer="TEST-C")]

    assert _price_findings("10.00", erp_prices).discrepancies == []


def test_variant_without_own_price_uses_template_price() -> None:
    variant_item = item("SKU-1", variant_of="TPL-1")
    findings = _price_findings("12.00", [price("TPL-1", "10.00")], erp_item=variant_item)

    [found] = findings.discrepancies
    assert (found.type, found.erp_value) == (DiscrepancyType.PRICE_MISMATCH, Decimal("10.00"))
    assert found.detail == "ERPNext price inherited from the template item"


def test_variant_own_price_beats_template_price() -> None:
    variant_item = item("SKU-1", variant_of="TPL-1")
    erp_prices = [price("TPL-1", "99.00", day(-1)), price("SKU-1", "10.00", day(-30))]

    assert _price_findings("10.00", erp_prices, erp_item=variant_item).discrepancies == []


def test_variant_falls_back_to_template_when_own_rows_do_not_fit_one_unit() -> None:
    # ERPNext returns no price for the variant (no row fits qty 1), then tries the template.
    variant_item = item("SKU-1", variant_of="TPL-1")
    erp_prices = [price("SKU-1", "99.00", packing_unit=6), price("TPL-1", "10.00")]

    assert _price_findings("10.00", erp_prices, erp_item=variant_item).discrepancies == []


def test_no_variant_fallback_for_non_variants() -> None:
    findings = _price_findings("10.00", [price("TPL-1", "10.00")])

    assert [d.type for d in findings.discrepancies] == [DiscrepancyType.PRICE_MISSING]


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


def _stock_findings(
    erp_bins: list,
    erp_item: ErpItem | None = None,
    bins_trustworthy: bool = True,
    **variant_fields: object,
):
    shopify = index_shopify_variants([variant("SKU-1", **variant_fields)])
    erp = index_erp_items([erp_item or item("SKU-1")], options())
    matched = match_products(shopify, erp)
    return check_stock(matched, erp_bins, options(), bins_trustworthy=bins_trustworthy)


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


def test_missing_bin_is_not_zero_when_bin_data_is_unconfirmed() -> None:
    findings = _stock_findings([], bins_trustworthy=False, on_hand=2)

    assert findings.discrepancies == []
    [skipped] = findings.skipped
    assert (skipped.check, skipped.key) == (CheckName.STOCK, "SKU-1")
    assert "not assuming zero" in skipped.reason


def test_shopify_side_skip_reason_wins_over_unconfirmed_bin_data() -> None:
    findings = _stock_findings([], bins_trustworthy=False, inventory_tracked=False)

    [skipped] = findings.skipped
    assert skipped.reason == "Inventory is not tracked in Shopify for this variant."


def test_existing_bin_is_still_compared_when_bin_data_is_unconfirmed() -> None:
    findings = _stock_findings([stock_bin("SKU-1", "3")], bins_trustworthy=False, on_hand=5)

    assert [d.type for d in findings.discrepancies] == [DiscrepancyType.STOCK_MISMATCH]


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
