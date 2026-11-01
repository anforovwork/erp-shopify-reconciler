"""Comparison rules (pure functions, no network).

Every function takes already-fetched data and returns findings. Nothing here reads the clock,
the environment or the network, so every rule can be tested with plain in-memory data.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from reconciler.models import (
    CheckName,
    Discrepancy,
    DiscrepancyType,
    ErpBin,
    ErpData,
    ErpItem,
    ErpItemPrice,
    ErpSalesOrder,
    ReconciliationResult,
    ShopifyData,
    ShopifyOrder,
    ShopifyShop,
    ShopifyVariant,
    SkippedCheck,
    System,
)

PRICE_TOLERANCE = Decimal("0.01")
"""A price difference must be strictly greater than this to count as a mismatch."""


class ConfigurationError(Exception):
    """Settings or system configuration make a reliable comparison impossible."""


class ReconcileOptions(BaseModel):
    """Business settings the rules need; built from configuration by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: AwareDatetime
    """Run time. Price validity uses as_of.date(); the order lookback counts back from it."""
    item_groups: frozenset[str] = Field(min_length=1)
    price_list: str
    currency: str
    erp_prices_include_tax: bool | None
    warehouse: str
    unit_uoms: frozenset[str] = Field(min_length=1)
    order_lookback_days: int = Field(gt=0)


@dataclass
class Findings:
    discrepancies: list[Discrepancy] = field(default_factory=list)
    skipped: list[SkippedCheck] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def extend(self, other: "Findings") -> None:
        self.discrepancies.extend(other.discrepancies)
        self.skipped.extend(other.skipped)
        self.warnings.extend(other.warnings)


@dataclass
class SkuIndex[T]:
    """Records keyed by normalized SKU. Ambiguous SKUs are kept out of `unique`."""

    unique: dict[str, T]
    duplicated: dict[str, int]
    empty: list[T]


@dataclass
class MatchedProduct:
    variant: ShopifyVariant
    item: ErpItem


def normalize_sku(raw: str | None) -> str:
    """Trim surrounding whitespace only. SKUs stay case-sensitive."""
    return (raw or "").strip()


def is_in_scope(item: ErpItem, options: ReconcileOptions) -> bool:
    return (
        not item.disabled
        and item.is_sales_item
        and item.is_stock_item
        and not item.has_variants
        and item.item_group in options.item_groups
    )


def index_by_sku[T](records: Iterable[T], sku_of: Callable[[T], str | None]) -> SkuIndex[T]:
    groups: dict[str, list[T]] = {}
    empty: list[T] = []
    for record in records:
        sku = normalize_sku(sku_of(record))
        if sku:
            groups.setdefault(sku, []).append(record)
        else:
            empty.append(record)
    unique = {sku: group[0] for sku, group in groups.items() if len(group) == 1}
    duplicated = {sku: len(group) for sku, group in groups.items() if len(group) > 1}
    return SkuIndex(unique=unique, duplicated=duplicated, empty=empty)


def index_shopify_variants(variants: Iterable[ShopifyVariant]) -> SkuIndex[ShopifyVariant]:
    return index_by_sku(variants, lambda variant: variant.sku)


def index_erp_items(items: Iterable[ErpItem], options: ReconcileOptions) -> SkuIndex[ErpItem]:
    in_scope = (item for item in items if is_in_scope(item, options))
    return index_by_sku(in_scope, lambda item: item.item_code)


def check_sku_quality(
    shopify: SkuIndex[ShopifyVariant], erp: SkuIndex[ErpItem]
) -> list[Discrepancy]:
    findings = [
        Discrepancy(
            type=DiscrepancyType.SKU_EMPTY,
            key=variant.variant_id,
            source_of_truth=System.ERPNEXT,
            suggested_action="Set this Shopify variant's SKU to the matching ERPNext item code.",
        )
        for variant in shopify.empty
    ]
    findings += [
        Discrepancy(
            type=DiscrepancyType.SKU_EMPTY,
            key=item.item_code,
            source_of_truth=System.ERPNEXT,
            suggested_action="Fix the ERPNext item code; it is blank after trimming.",
        )
        for item in erp.empty
    ]
    for system, index in ((System.SHOPIFY, shopify), (System.ERPNEXT, erp)):
        findings += [
            _duplicate_sku(sku, count, system) for sku, count in sorted(index.duplicated.items())
        ]
    return findings


def _duplicate_sku(sku: str, count: int, system: System) -> Discrepancy:
    return Discrepancy(
        type=DiscrepancyType.SKU_DUPLICATE,
        key=sku,
        source_of_truth=System.ERPNEXT,
        suggested_action=(
            f"Make this SKU unique in {_label(system)}; other checks skip it until then."
        ),
        detail=f"appears {count} times in {_label(system)}",
    )


def ambiguous_skus(shopify: SkuIndex[ShopifyVariant], erp: SkuIndex[ErpItem]) -> set[str]:
    return set(shopify.duplicated) | set(erp.duplicated)


def match_products(
    shopify: SkuIndex[ShopifyVariant], erp: SkuIndex[ErpItem]
) -> dict[str, MatchedProduct]:
    """SKUs present exactly once on both sides."""
    ambiguous = ambiguous_skus(shopify, erp)
    return {
        sku: MatchedProduct(variant=shopify.unique[sku], item=erp.unique[sku])
        for sku in sorted(shopify.unique.keys() & erp.unique.keys())
        if sku not in ambiguous
    }


def check_missing(shopify: SkuIndex[ShopifyVariant], erp: SkuIndex[ErpItem]) -> list[Discrepancy]:
    ambiguous = ambiguous_skus(shopify, erp)
    missing_in_shopify = sorted(erp.unique.keys() - shopify.unique.keys() - ambiguous)
    missing_in_erp = sorted(shopify.unique.keys() - erp.unique.keys() - ambiguous)
    findings = [
        Discrepancy(
            type=DiscrepancyType.MISSING_IN_SHOPIFY,
            key=sku,
            source_of_truth=System.ERPNEXT,
            suggested_action=(
                "Create the product in Shopify, or move the ERPNext item out of the "
                "configured item groups if it is not sold online."
            ),
        )
        for sku in missing_in_shopify
    ]
    findings += [
        Discrepancy(
            type=DiscrepancyType.MISSING_IN_ERP,
            key=sku,
            source_of_truth=System.ERPNEXT,
            suggested_action=(
                "Not found among enabled sales items in the configured ERPNext item groups: "
                "create or fix the ERPNext item, or correct the Shopify SKU."
            ),
        )
        for sku in missing_in_erp
    ]
    return findings


# --- Prices --------------------------------------------------------------------------------


def validate_currencies(shop_currency: str, price_list_currency: str, configured: str) -> None:
    """No currency conversion in v1: all three must be equal, otherwise stop."""
    wrong = [
        f"{source} uses {currency}"
        for source, currency in (
            ("the Shopify store", shop_currency),
            ("the ERPNext price list", price_list_currency),
        )
        if currency != configured
    ]
    if wrong:
        raise ConfigurationError(
            f"Currency mismatch: CURRENCY is {configured}, but {' and '.join(wrong)}. "
            "Currency conversion is not supported."
        )


def tax_treatment_matches(shop: ShopifyShop, options: ReconcileOptions) -> bool:
    return options.erp_prices_include_tax is not None and (
        options.erp_prices_include_tax == shop.taxes_included
    )


def is_applicable_price(price: ErpItemPrice, item: ErpItem, options: ReconcileOptions) -> bool:
    run_date = options.as_of.date()
    return (
        price.item_code == item.item_code
        and price.price_list == options.price_list
        and not price.customer
        and not price.batch_no
        and price.uom == item.stock_uom
        and (price.valid_from is None or price.valid_from <= run_date)
        and (price.valid_upto is None or run_date <= price.valid_upto)
    )


def check_prices(
    matched: dict[str, MatchedProduct],
    prices: Iterable[ErpItemPrice],
    shop: ShopifyShop,
    options: ReconcileOptions,
) -> Findings:
    findings = Findings()
    compare = tax_treatment_matches(shop, options)
    if not compare:
        findings.skipped.append(
            SkippedCheck(
                check=CheckName.PRICE,
                key=None,
                reason=(
                    "Price comparison unsupported: ERPNEXT_PRICES_INCLUDE_TAX is not set or "
                    "does not match Shopify's taxesIncluded setting. Missing and ambiguous "
                    "prices are still checked."
                ),
            )
        )
    prices_by_code: dict[str, list[ErpItemPrice]] = {}
    for price in prices:
        prices_by_code.setdefault(price.item_code, []).append(price)

    for sku, product in matched.items():
        candidates = prices_by_code.get(product.item.item_code, [])
        applicable = [p for p in candidates if is_applicable_price(p, product.item, options)]
        finding = _price_finding(sku, product.variant.price, applicable, compare)
        if finding:
            findings.discrepancies.append(finding)
    return findings


def _price_finding(
    sku: str, shopify_price: Decimal, applicable: list[ErpItemPrice], compare: bool
) -> Discrepancy | None:
    if not applicable:
        return Discrepancy(
            type=DiscrepancyType.PRICE_MISSING,
            key=sku,
            shopify_value=shopify_price,
            source_of_truth=System.ERPNEXT,
            suggested_action=(
                "Add one Item Price on the configured selling price list (stock UOM, no customer, "
                "no batch, valid today)."
            ),
        )
    if len(applicable) > 1:
        return Discrepancy(
            type=DiscrepancyType.PRICE_AMBIGUOUS,
            key=sku,
            shopify_value=shopify_price,
            source_of_truth=System.ERPNEXT,
            suggested_action="Keep exactly one applicable Item Price on the selling price list.",
            detail=f"{len(applicable)} applicable price rows",
        )
    erp_price = applicable[0].price_list_rate
    if compare and abs(shopify_price - erp_price) > PRICE_TOLERANCE:
        return Discrepancy(
            type=DiscrepancyType.PRICE_MISMATCH,
            key=sku,
            shopify_value=shopify_price,
            erp_value=erp_price,
            source_of_truth=System.ERPNEXT,
            suggested_action="Update the Shopify price to the ERPNext price.",
        )
    return None


# --- Stock ---------------------------------------------------------------------------------


def check_stock(
    matched: dict[str, MatchedProduct], bins: Iterable[ErpBin], options: ReconcileOptions
) -> Findings:
    """Shopify on_hand at the configured location vs ERPNext Bin actual_qty in the warehouse."""
    erp_qty = {b.item_code: b.actual_qty for b in bins if b.warehouse == options.warehouse}
    findings = Findings()
    for sku, product in matched.items():
        # No Bin row means no stock transaction yet, i.e. zero.
        qty = erp_qty.get(product.item.item_code, Decimal(0))
        reason = _stock_skip_reason(product, qty, options)
        if reason:
            findings.skipped.append(SkippedCheck(check=CheckName.STOCK, key=sku, reason=reason))
        elif Decimal(product.variant.on_hand or 0) != qty:
            findings.discrepancies.append(
                Discrepancy(
                    type=DiscrepancyType.STOCK_MISMATCH,
                    key=sku,
                    shopify_value=Decimal(product.variant.on_hand or 0),
                    erp_value=qty,
                    source_of_truth=System.ERPNEXT,
                    suggested_action=(
                        "Confirm the ERPNext warehouse count, then set Shopify on-hand "
                        "inventory to match it."
                    ),
                )
            )
    return findings


def _stock_skip_reason(
    product: MatchedProduct, erp_qty: Decimal, options: ReconcileOptions
) -> str | None:
    if not product.variant.inventory_tracked:
        return "Inventory is not tracked in Shopify for this variant."
    if product.variant.on_hand is None:
        return "Variant is not stocked at the configured Shopify location."
    if product.item.stock_uom not in options.unit_uoms:
        return "ERPNext stock UOM is not in ERPNEXT_UNIT_UOMS, so units are not comparable."
    if erp_qty != erp_qty.to_integral_value():
        return "ERPNext quantity is fractional; Shopify counts whole units."
    return None


# --- Orders --------------------------------------------------------------------------------

ORDER_CHECK_DISABLED = (
    "Order reconciliation disabled: ERPNEXT_ORDER_ID_FIELD is not configured or the field "
    "does not exist on ERPNext Sales Order. The tool does not create it."
)
SUBMITTED = 1
"""ERPNext docstatus of a submitted document; only these count as a match."""
_DOCSTATUS_LABELS = {0: "draft", 2: "cancelled"}


def is_eligible_order(order: ShopifyOrder, options: ReconcileOptions) -> bool:
    since = options.as_of - timedelta(days=options.order_lookback_days)
    return (
        order.created_at >= since
        and order.financial_status == "PAID"
        and not order.cancelled
        and not order.test
    )


def check_orders(
    orders: Iterable[ShopifyOrder],
    sales_orders: Iterable[ErpSalesOrder] | None,
    options: ReconcileOptions,
) -> Findings:
    if sales_orders is None:
        return Findings(
            skipped=[SkippedCheck(check=CheckName.ORDER, key=None, reason=ORDER_CHECK_DISABLED)],
            warnings=[ORDER_CHECK_DISABLED],
        )
    statuses: dict[str, set[int]] = {}
    for sales_order in sales_orders:
        order_id = (sales_order.shopify_order_id or "").strip()
        if order_id:
            statuses.setdefault(order_id, set()).add(sales_order.docstatus)

    findings = Findings()
    eligible = sorted(
        (o for o in orders if is_eligible_order(o, options)), key=lambda o: o.created_at
    )
    for order in eligible:
        found = statuses.get(order.order_id, set())
        if SUBMITTED not in found:
            findings.discrepancies.append(_order_not_in_erp(order.order_id, found))
    return findings


def _order_not_in_erp(order_id: str, docstatuses: set[int]) -> Discrepancy:
    others = sorted(_DOCSTATUS_LABELS.get(status, f"docstatus {status}") for status in docstatuses)
    detail = f"only {' and '.join(others)} Sales Order(s) found" if others else ""
    return Discrepancy(
        type=DiscrepancyType.ORDER_NOT_IN_ERP,
        key=order_id,
        source_of_truth=System.SHOPIFY,
        suggested_action=(
            "Create (or submit) the ERPNext Sales Order for this Shopify order and store "
            "the order GID in the mapping field."
        ),
        detail=detail,
    )


# --- Everything together -------------------------------------------------------------------


def reconcile(
    shopify: ShopifyData, erp: ErpData, options: ReconcileOptions
) -> ReconciliationResult:
    """Run every rule. Raises ConfigurationError when currencies cannot be compared."""
    validate_currencies(shopify.shop.currency, erp.price_list_currency, options.currency)

    shopify_index = index_shopify_variants(shopify.variants)
    erp_index = index_erp_items(erp.items, options)
    matched = match_products(shopify_index, erp_index)

    findings = Findings(discrepancies=check_sku_quality(shopify_index, erp_index))
    findings.discrepancies += check_missing(shopify_index, erp_index)
    findings.extend(check_prices(matched, erp.prices, shopify.shop, options))
    findings.extend(check_stock(matched, erp.bins, options))
    findings.extend(check_orders(shopify.orders, erp.sales_orders, options))

    return ReconciliationResult(
        generated_at=options.as_of,
        shopify_fetched_at=shopify.fetched_at,
        erpnext_fetched_at=erp.fetched_at,
        discrepancies=tuple(findings.discrepancies),
        skipped=tuple(findings.skipped),
        warnings=tuple(findings.warnings),
    )


def _label(system: System) -> str:
    return "Shopify" if system is System.SHOPIFY else "ERPNext"
