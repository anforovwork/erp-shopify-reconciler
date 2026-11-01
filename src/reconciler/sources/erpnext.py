"""Read-only ERPNext REST client: everything reconcile() needs, as one ErpData.

Every request goes through the read-only HTTP layer (GET on /api/resource only). Any error,
permission problem or malformed row raises; ErpData is only built from complete data, which is
what makes `warehouse_confirmed` and `bins_complete` trustworthy.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from reconciler.models import ErpBin, ErpData, ErpItem, ErpItemPrice, ErpSalesOrder
from reconciler.reconcile import ConfigurationError
from reconciler.sources.http import (
    IncompleteResponse,
    ReadOnlyClient,
    SourceError,
    iter_erpnext_list,
)

ITEM_FIELDS = [
    "item_code",
    "item_group",
    "stock_uom",
    "disabled",
    "is_sales_item",
    "is_stock_item",
    "has_variants",
    "variant_of",
]
ITEM_PRICE_FIELDS = [
    "item_code",
    "price_list",
    "price_list_rate",
    "uom",
    "customer",
    "supplier",
    "batch_no",
    "valid_from",
    "valid_upto",
    "packing_unit",
]
BIN_FIELDS = ["item_code", "warehouse", "actual_qty"]
_PERMISSION_DENIED = (401, 403)


@dataclass(frozen=True)
class ErpNextScope:
    """What to read; built from settings by the caller."""

    price_list: str
    warehouse: str
    order_id_field: str | None
    page_size: int = 500


def _utc_now() -> datetime:
    return datetime.now(UTC)


def fetch_erp_data(
    client: ReadOnlyClient, scope: ErpNextScope, now: Callable[[], datetime] = _utc_now
) -> ErpData:
    """Read every record reconcile() needs. Raises instead of returning partial data."""
    fetched_at = now()
    check_credentials(client)
    currency = fetch_price_list_currency(client, scope)
    confirm_warehouse(client, scope)
    items = _records(client, scope, "Item", ITEM_FIELDS, None, ErpItem)
    price_filter = [["price_list", "=", scope.price_list]]
    prices = _records(client, scope, "Item Price", ITEM_PRICE_FIELDS, price_filter, ErpItemPrice)
    bins = _records(client, scope, "Bin", BIN_FIELDS, [["warehouse", "=", scope.warehouse]], ErpBin)
    sales_orders = fetch_sales_orders(client, scope)
    try:
        return ErpData(
            fetched_at=fetched_at,
            price_list_currency=currency,
            items=tuple(items),
            prices=tuple(prices),
            bins=tuple(bins),
            warehouse_confirmed=True,  # confirm_warehouse() raised otherwise
            bins_complete=True,  # every Bin page was read; any failure raised above
            sales_orders=sales_orders,
        )
    except ValidationError as error:
        raise IncompleteResponse(f"ERPNext data invalid in {_fields(error)}") from None


def check_credentials(client: ReadOnlyClient) -> None:
    try:
        body = client.request_json("GET", "/api/method/frappe.auth.get_logged_user")
    except SourceError as error:
        if error.status_code in _PERMISSION_DENIED:
            raise ConfigurationError(
                f"ERPNext rejected the API credentials (HTTP {error.status_code}); "
                "check ERPNEXT_API_KEY and ERPNEXT_API_SECRET."
            ) from None
        raise
    user = body.get("message") if isinstance(body, dict) else None
    if not isinstance(user, str) or not user or user == "Guest":
        raise ConfigurationError("ERPNext did not accept the API credentials (not logged in).")


def fetch_price_list_currency(client: ReadOnlyClient, scope: ErpNextScope) -> str:
    fields = ["name", "currency", "enabled", "selling"]
    row = _single(client, scope, "Price List", fields, scope.price_list, "ERPNEXT_PRICE_LIST")
    if not _flag(row, "enabled", "Price List"):
        raise ConfigurationError("ERPNEXT_PRICE_LIST is disabled in ERPNext.")
    if not _flag(row, "selling", "Price List"):
        raise ConfigurationError("ERPNEXT_PRICE_LIST is not a selling price list.")
    currency = row.get("currency")
    if not isinstance(currency, str):
        raise IncompleteResponse("Price List: currency missing")
    return currency


def confirm_warehouse(client: ReadOnlyClient, scope: ErpNextScope) -> None:
    row = _single(
        client, scope, "Warehouse", ["name", "is_group"], scope.warehouse, "ERPNEXT_WAREHOUSE"
    )
    if _flag(row, "is_group", "Warehouse"):
        raise ConfigurationError(
            "ERPNEXT_WAREHOUSE is a group warehouse; stock is held in its child warehouses."
        )


def fetch_sales_orders(
    client: ReadOnlyClient, scope: ErpNextScope
) -> tuple[ErpSalesOrder, ...] | None:
    """None = no mapping field configured or it does not exist (the order check is skipped).

    The field is looked up in Custom Field: for a list query Frappe answers HTTP 403 both for an
    unknown field and for missing permission, so that answer cannot tell the two apart.
    """
    field = scope.order_id_field
    if field is None or not mapping_field_exists(client, scope, field):
        return None
    rows = _rows(client, scope, "Sales Order", ["name", "docstatus", field], [[field, "is", "set"]])
    orders = [
        {"name": r.get("name"), "docstatus": r.get("docstatus"), "shopify_order_id": r.get(field)}
        for r in rows
    ]
    return tuple(_validate(orders, ErpSalesOrder, "Sales Order"))


def mapping_field_exists(client: ReadOnlyClient, scope: ErpNextScope, field: str) -> bool:
    filters = [["dt", "=", "Sales Order"], ["fieldname", "=", field]]
    try:
        return bool(_rows(client, scope, "Custom Field", ["fieldname"], filters))
    except SourceError as error:
        if error.status_code in _PERMISSION_DENIED:
            raise ConfigurationError(
                "The ERPNext API user cannot read Custom Field, so the order mapping field "
                "cannot be checked; grant read access (docs/erpnext-readonly-user.md)."
            ) from None
        raise


# --- helpers -------------------------------------------------------------------------------


def _rows(
    client: ReadOnlyClient,
    scope: ErpNextScope,
    doctype: str,
    fields: list[str],
    filters: list[list[Any]] | None,
) -> list[dict[str, Any]]:
    try:
        return list(
            iter_erpnext_list(
                client, doctype, fields=fields, filters=filters, page_size=scope.page_size
            )
        )
    except SourceError as error:
        if error.status_code in _PERMISSION_DENIED:
            raise ConfigurationError(
                f"The ERPNext API user cannot read {doctype} (HTTP {error.status_code}); "
                "see docs/erpnext-readonly-user.md."
            ) from None
        raise


def _records[M: BaseModel](
    client: ReadOnlyClient,
    scope: ErpNextScope,
    doctype: str,
    fields: list[str],
    filters: list[list[Any]] | None,
    model: type[M],
) -> list[M]:
    return _validate(_rows(client, scope, doctype, fields, filters), model, doctype)


def _validate[M: BaseModel](
    rows: Iterable[dict[str, Any]], model: type[M], doctype: str
) -> list[M]:
    """Rows -> models. Errors name the fields, never the (untrusted) values."""
    try:
        return [model.model_validate(row) for row in rows]
    except ValidationError as error:
        raise IncompleteResponse(f"{doctype}: unexpected data in {_fields(error)}") from None


def _single(
    client: ReadOnlyClient,
    scope: ErpNextScope,
    doctype: str,
    fields: list[str],
    name: str,
    setting: str,
) -> dict[str, Any]:
    rows = _rows(client, scope, doctype, fields, [["name", "=", name]])
    if len(rows) != 1:
        raise ConfigurationError(f"{setting} was not found in ERPNext (or is not readable).")
    return rows[0]


def _flag(row: dict[str, Any], key: str, doctype: str) -> bool:
    value = row.get(key)
    if value not in (0, 1):
        raise IncompleteResponse(f"{doctype}: {key} is not a 0/1 flag")
    return bool(value)


def _fields(error: ValidationError) -> list[str]:
    return sorted({str(issue["loc"][0]) for issue in error.errors() if issue["loc"]})
