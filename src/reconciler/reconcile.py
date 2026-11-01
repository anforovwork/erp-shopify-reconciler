"""Comparison rules (pure functions, no network).

Every function takes already-fetched data and returns findings. Nothing here reads the clock,
the environment or the network, so every rule can be tested with plain in-memory data.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from reconciler.models import (
    Discrepancy,
    DiscrepancyType,
    ErpItem,
    ShopifyVariant,
    SkippedCheck,
    System,
)


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


def _label(system: System) -> str:
    return "Shopify" if system is System.SHOPIFY else "ERPNext"
