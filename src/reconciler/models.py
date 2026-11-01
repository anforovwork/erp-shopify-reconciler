"""Data models: records read from each system, and the reconciliation result.

Input models mirror only the fields the rules need. Text that came from an external system
(SKUs, item codes, order ids) is untrusted data, never instructions.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict


def _reject_float(value: object) -> object:
    if isinstance(value, float):
        raise ValueError("money and quantities must be Decimal, int or str, never float")
    return value


Amount = Annotated[Decimal, BeforeValidator(_reject_float)]
"""A price or quantity. Floats are rejected so rounding errors cannot sneak in."""


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class System(StrEnum):
    SHOPIFY = "shopify"
    ERPNEXT = "erpnext"


# --- Shopify -------------------------------------------------------------------------------


class ShopifyShop(_Record):
    currency: str
    taxes_included: bool


class ShopifyVariant(_Record):
    variant_id: str
    sku: str | None
    price: Amount
    inventory_tracked: bool
    on_hand: int | None
    """Physical quantity at the configured location; None = not stocked there."""


class ShopifyOrder(_Record):
    order_id: str
    """Order GID, e.g. gid://shopify/Order/123."""
    created_at: AwareDatetime
    financial_status: str
    cancelled: bool
    test: bool


class ShopifyData(_Record):
    fetched_at: AwareDatetime
    shop: ShopifyShop
    variants: tuple[ShopifyVariant, ...]
    orders: tuple[ShopifyOrder, ...]


# --- ERPNext -------------------------------------------------------------------------------


class ErpItem(_Record):
    item_code: str
    item_group: str
    stock_uom: str
    disabled: bool
    is_sales_item: bool
    is_stock_item: bool
    has_variants: bool


class ErpItemPrice(_Record):
    item_code: str
    price_list: str
    price_list_rate: Amount
    uom: str | None
    customer: str | None
    batch_no: str | None
    valid_from: date | None
    valid_upto: date | None
    packing_unit: int
    """0 = any quantity; n > 0 = only applies to multiples of n (ERPNext semantics)."""


class ErpBin(_Record):
    item_code: str
    warehouse: str
    actual_qty: Amount


class ErpSalesOrder(_Record):
    name: str
    shopify_order_id: str | None
    docstatus: int
    """0 = draft, 1 = submitted, 2 = cancelled."""


class ErpData(_Record):
    fetched_at: AwareDatetime
    price_list_currency: str
    items: tuple[ErpItem, ...]
    prices: tuple[ErpItemPrice, ...]
    bins: tuple[ErpBin, ...]
    sales_orders: tuple[ErpSalesOrder, ...] | None
    """None = order mapping field unavailable; the order check is skipped."""


# --- Results -------------------------------------------------------------------------------


class DiscrepancyCategory(StrEnum):
    COMPARISON = "comparison"
    DATA_QUALITY = "data_quality"


class DiscrepancyType(StrEnum):
    MISSING_IN_SHOPIFY = "MISSING_IN_SHOPIFY"
    MISSING_IN_ERP = "MISSING_IN_ERP"
    PRICE_MISMATCH = "PRICE_MISMATCH"
    STOCK_MISMATCH = "STOCK_MISMATCH"
    ORDER_NOT_IN_ERP = "ORDER_NOT_IN_ERP"
    SKU_EMPTY = "SKU_EMPTY"
    SKU_DUPLICATE = "SKU_DUPLICATE"
    PRICE_MISSING = "PRICE_MISSING"
    PRICE_AMBIGUOUS = "PRICE_AMBIGUOUS"
    PRICE_UOM_MISSING = "PRICE_UOM_MISSING"

    @property
    def category(self) -> DiscrepancyCategory:
        if self in _DATA_QUALITY_TYPES:
            return DiscrepancyCategory.DATA_QUALITY
        return DiscrepancyCategory.COMPARISON


_DATA_QUALITY_TYPES = frozenset(
    {
        DiscrepancyType.SKU_EMPTY,
        DiscrepancyType.SKU_DUPLICATE,
        DiscrepancyType.PRICE_MISSING,
        DiscrepancyType.PRICE_AMBIGUOUS,
        DiscrepancyType.PRICE_UOM_MISSING,
    }
)


class Discrepancy(_Record):
    type: DiscrepancyType
    key: str
    """SKU, Shopify variant id (for an empty SKU) or Shopify order GID."""
    shopify_value: Amount | None = None
    erp_value: Amount | None = None
    source_of_truth: System
    suggested_action: str
    detail: str = ""
    """Extra context written by this tool (never copied from external data)."""

    @property
    def category(self) -> DiscrepancyCategory:
        return self.type.category


class CheckName(StrEnum):
    SKU = "sku"
    SCOPE = "scope"
    PRICE = "price"
    STOCK = "stock"
    ORDER = "order"


class SkippedCheck(_Record):
    check: CheckName
    key: str | None
    """The SKU or order skipped; None = the whole check was skipped."""
    reason: str


class ReconciliationResult(_Record):
    generated_at: AwareDatetime
    shopify_fetched_at: AwareDatetime
    erpnext_fetched_at: AwareDatetime
    discrepancies: tuple[Discrepancy, ...]
    skipped: tuple[SkippedCheck, ...]
    warnings: tuple[str, ...]

    @property
    def has_discrepancies(self) -> bool:
        return bool(self.discrepancies)
