"""The full rule set on one small, generated scenario."""

from datetime import timedelta
from decimal import Decimal

import pytest
from builders import AS_OF, item, options, order, price, sales_order, stock_bin, variant

from reconciler.models import CheckName, DiscrepancyType, ErpData, ShopifyData, ShopifyShop
from reconciler.reconcile import ConfigurationError, reconcile

SHOPIFY_FETCHED = AS_OF - timedelta(minutes=2)
ERP_FETCHED = AS_OF - timedelta(minutes=1)


def _shopify(currency: str = "CAD") -> ShopifyData:
    return ShopifyData(
        fetched_at=SHOPIFY_FETCHED,
        shop=ShopifyShop(currency=currency, taxes_included=False),
        variants=(
            variant("OK-1", 1),
            variant("PRICE-1", 2, price=Decimal("12.50")),
            variant("STOCK-1", 3, on_hand=7),
            variant("ONLY-SHOP", 4),
            variant(None, 5),
            variant("DUP-1", 6),
            variant("DUP-1", 7),
            variant("UNTRACKED-1", 8, inventory_tracked=False),
            variant("DISABLED-1", 9),
        ),
        orders=(order(1), order(2), order(3, test=True)),
    )


def _erp(sales_orders: tuple | None = (sales_order(1),)) -> ErpData:
    codes = ["OK-1", "PRICE-1", "STOCK-1", "ONLY-ERP", "DUP-1", "UNTRACKED-1"]
    return ErpData(
        fetched_at=ERP_FETCHED,
        price_list_currency="CAD",
        items=(
            *(item(code) for code in codes),
            item("RAW-1", item_group="Raw Material"),
            item("DISABLED-1", disabled=True),
        ),
        prices=tuple(price(code) for code in codes if code != "STOCK-1"),
        bins=tuple(stock_bin(code, "5") for code in codes),
        sales_orders=sales_orders,
    )


def test_full_reconciliation() -> None:
    result = reconcile(_shopify(), _erp(), options())

    assert [(d.type, d.key) for d in result.discrepancies] == [
        (DiscrepancyType.SKU_EMPTY, "gid://shopify/ProductVariant/5"),
        (DiscrepancyType.SKU_DUPLICATE, "DUP-1"),
        (DiscrepancyType.MISSING_IN_SHOPIFY, "ONLY-ERP"),
        (DiscrepancyType.MISSING_IN_ERP, "ONLY-SHOP"),
        (DiscrepancyType.PRICE_MISMATCH, "PRICE-1"),
        (DiscrepancyType.PRICE_MISSING, "STOCK-1"),
        (DiscrepancyType.STOCK_MISMATCH, "STOCK-1"),
        (DiscrepancyType.ORDER_NOT_IN_ERP, "gid://shopify/Order/2"),
    ]
    assert [(s.check, s.key) for s in result.skipped] == [
        (CheckName.SCOPE, "DISABLED-1"),
        (CheckName.STOCK, "UNTRACKED-1"),
    ]
    assert result.warnings == ()
    assert result.generated_at == AS_OF
    assert result.shopify_fetched_at == SHOPIFY_FETCHED
    assert result.erpnext_fetched_at == ERP_FETCHED


def test_clean_data_has_no_discrepancies() -> None:
    shopify = ShopifyData(
        fetched_at=SHOPIFY_FETCHED,
        shop=ShopifyShop(currency="CAD", taxes_included=False),
        variants=(variant("OK-1"),),
        orders=(order(1),),
    )
    erp = ErpData(
        fetched_at=ERP_FETCHED,
        price_list_currency="CAD",
        items=(item("OK-1"),),
        prices=(price("OK-1"),),
        bins=(stock_bin("OK-1", "5"),),
        sales_orders=(sales_order(1),),
    )

    result = reconcile(shopify, erp, options())

    assert not result.has_discrepancies
    assert result.skipped == ()


def test_without_order_mapping_field_orders_are_skipped_with_warning() -> None:
    result = reconcile(_shopify(), _erp(sales_orders=None), options())

    assert DiscrepancyType.ORDER_NOT_IN_ERP not in {d.type for d in result.discrepancies}
    assert (CheckName.ORDER, None) in [(s.check, s.key) for s in result.skipped]
    assert len(result.warnings) == 1


def test_currency_mismatch_stops_the_run() -> None:
    with pytest.raises(ConfigurationError):
        reconcile(_shopify(currency="USD"), _erp(), options())
