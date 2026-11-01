"""Builders for generated test data. Every value here is fake; there is no real store data."""

from datetime import UTC, date, datetime
from decimal import Decimal

from reconciler.models import (
    ErpBin,
    ErpItem,
    ErpItemPrice,
    ErpSalesOrder,
    ShopifyOrder,
    ShopifyVariant,
)
from reconciler.reconcile import ReconcileOptions

AS_OF = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
GROUP = "Test Products"
PRICE_LIST = "Test Selling"
WAREHOUSE = "Test Stores - TC"


def options(**overrides: object) -> ReconcileOptions:
    fields: dict[str, object] = {
        "as_of": AS_OF,
        "item_groups": frozenset({GROUP}),
        "price_list": PRICE_LIST,
        "currency": "CAD",
        "erp_prices_include_tax": False,
        "warehouse": WAREHOUSE,
        "unit_uoms": frozenset({"Nos"}),
        "order_lookback_days": 30,
    }
    return ReconcileOptions.model_validate(fields | overrides)


def variant(sku: str | None, n: int = 1, **overrides: object) -> ShopifyVariant:
    fields: dict[str, object] = {
        "variant_id": f"gid://shopify/ProductVariant/{n}",
        "sku": sku,
        "price": Decimal("10.00"),
        "inventory_tracked": True,
        "on_hand": 5,
    }
    return ShopifyVariant.model_validate(fields | overrides)


def item(code: str, **overrides: object) -> ErpItem:
    fields: dict[str, object] = {
        "item_code": code,
        "item_group": GROUP,
        "stock_uom": "Nos",
        "disabled": False,
        "is_sales_item": True,
        "is_stock_item": True,
        "has_variants": False,
    }
    return ErpItem.model_validate(fields | overrides)


def price(code: str, rate: str = "10.00", **overrides: object) -> ErpItemPrice:
    fields: dict[str, object] = {
        "item_code": code,
        "price_list": PRICE_LIST,
        "price_list_rate": Decimal(rate),
        "uom": "Nos",
        "customer": None,
        "batch_no": None,
        "valid_from": None,
        "valid_upto": None,
    }
    return ErpItemPrice.model_validate(fields | overrides)


def stock_bin(code: str, qty: str = "5", **overrides: object) -> ErpBin:
    fields: dict[str, object] = {
        "item_code": code,
        "warehouse": WAREHOUSE,
        "actual_qty": Decimal(qty),
    }
    return ErpBin.model_validate(fields | overrides)


def order(n: int, created_at: datetime = AS_OF, **overrides: object) -> ShopifyOrder:
    fields: dict[str, object] = {
        "order_id": f"gid://shopify/Order/{n}",
        "created_at": created_at,
        "financial_status": "PAID",
        "cancelled": False,
        "test": False,
    }
    return ShopifyOrder.model_validate(fields | overrides)


def sales_order(n: int, docstatus: int = 1, **overrides: object) -> ErpSalesOrder:
    fields: dict[str, object] = {
        "name": f"SAL-ORD-TEST-{n:05d}",
        "shopify_order_id": f"gid://shopify/Order/{n}",
        "docstatus": docstatus,
    }
    return ErpSalesOrder.model_validate(fields | overrides)


def day(offset: int) -> date:
    """A date relative to AS_OF."""
    return date.fromordinal(AS_OF.date().toordinal() + offset)
