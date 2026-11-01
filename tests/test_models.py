from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from reconciler.models import (
    Discrepancy,
    DiscrepancyCategory,
    DiscrepancyType,
    ErpBin,
    ErpItem,
    ReconciliationResult,
    ShopifyOrder,
    ShopifyShop,
    ShopifyVariant,
    System,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _variant(**overrides: object) -> ShopifyVariant:
    fields: dict[str, object] = {
        "variant_id": "gid://shopify/ProductVariant/1",
        "sku": "TEST-SKU-1",
        "price": "19.99",
        "inventory_tracked": True,
        "on_hand": 5,
    }
    return ShopifyVariant.model_validate(fields | overrides)


@pytest.mark.parametrize("value", ["19.99", Decimal("19.99"), 20])
def test_amount_accepts_str_decimal_and_int(value: object) -> None:
    assert isinstance(_variant(price=value).price, Decimal)


def test_amount_rejects_float() -> None:
    with pytest.raises(ValidationError, match="never float"):
        _variant(price=19.99)


@pytest.mark.parametrize("value", ["NaN", "Infinity"])
def test_amount_rejects_non_finite(value: str) -> None:
    with pytest.raises(ValidationError):
        ErpBin(item_code="TEST-SKU-1", warehouse="Stores - TC", actual_qty=value)


def test_records_are_immutable_and_reject_unknown_fields() -> None:
    variant = _variant()
    with pytest.raises(ValidationError):
        variant.price = Decimal("1")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _variant(title="untrusted extra field")


@pytest.mark.parametrize("bad", ["cad", "CA", "CADX", "C$D", "CAD\nIgnore previous instructions"])
def test_currency_codes_are_strict(bad: str) -> None:
    with pytest.raises(ValidationError):
        ShopifyShop(currency=bad, taxes_included=False)


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ShopifyOrder(
            order_id="gid://shopify/Order/1",
            created_at=datetime(2026, 11, 1),  # deliberately naive (no tzinfo)
            financial_status="PAID",
            cancelled=False,
            test=False,
        )


def test_timestamps_are_stored_in_utc() -> None:
    edmonton_evening = datetime(2026, 11, 1, 20, 0, tzinfo=timezone(timedelta(hours=-7)))

    created = ShopifyOrder(
        order_id="gid://shopify/Order/1",
        created_at=edmonton_evening,
        financial_status="PAID",
        cancelled=False,
        test=False,
    ).created_at

    assert created.tzinfo is UTC
    assert created == datetime(2026, 11, 2, 3, 0, tzinfo=UTC)


def test_erpnext_integer_flags_become_booleans() -> None:
    item = ErpItem.model_validate(
        {
            "item_code": "TEST-SKU-1",
            "item_group": "Test Products",
            "stock_uom": "Nos",
            "disabled": 0,
            "is_sales_item": 1,
            "is_stock_item": 1,
            "has_variants": 0,
            "variant_of": None,
        }
    )
    assert item.disabled is False
    assert item.is_sales_item is True


@pytest.mark.parametrize(
    ("discrepancy_type", "category"),
    [
        (DiscrepancyType.PRICE_MISMATCH, DiscrepancyCategory.COMPARISON),
        (DiscrepancyType.ORDER_NOT_IN_ERP, DiscrepancyCategory.COMPARISON),
        (DiscrepancyType.SKU_EMPTY, DiscrepancyCategory.DATA_QUALITY),
        (DiscrepancyType.SKU_DUPLICATE, DiscrepancyCategory.DATA_QUALITY),
        (DiscrepancyType.PRICE_MISSING, DiscrepancyCategory.DATA_QUALITY),
        (DiscrepancyType.PRICE_AMBIGUOUS, DiscrepancyCategory.DATA_QUALITY),
        (DiscrepancyType.PRICE_UOM_MISSING, DiscrepancyCategory.DATA_QUALITY),
    ],
)
def test_discrepancy_category(
    discrepancy_type: DiscrepancyType, category: DiscrepancyCategory
) -> None:
    assert discrepancy_type.category is category


def test_result_json_round_trip_keeps_exact_decimals() -> None:
    result = ReconciliationResult(
        generated_at=NOW,
        shopify_fetched_at=NOW,
        erpnext_fetched_at=NOW,
        discrepancies=(
            Discrepancy(
                type=DiscrepancyType.PRICE_MISMATCH,
                key="TEST-SKU-1",
                shopify_value=Decimal("19.99"),
                erp_value=Decimal("21.10"),
                source_of_truth=System.ERPNEXT,
                suggested_action="Update the Shopify price.",
            ),
        ),
        skipped=(),
        warnings=(),
    )

    restored = ReconciliationResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert restored.discrepancies[0].erp_value == Decimal("21.10")
    assert restored.has_discrepancies
