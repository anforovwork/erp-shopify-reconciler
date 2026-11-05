"""Read-only smoke test of the ERPNext client against a real ERPNext instance.

Usage (from the repository root):  .venv/bin/python scripts/erpnext_smoke.py

Reads settings from .env. Prints only counts and yes/no checks — never settings, credentials,
user names, URLs or record values. Every request goes through the read-only HTTP layer.
"""

import sys
from datetime import UTC, datetime

import httpx

from reconciler.config import load_settings
from reconciler.reconcile import ConfigurationError, is_in_scope
from reconciler.sources.erpnext import ErpNextScope, fetch_erp_data
from reconciler.sources.http import ReadOnlyViolation, SourceError, erpnext_client


class CountingTransport(httpx.HTTPTransport):
    def __init__(self) -> None:
        super().__init__()
        self.methods: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.methods.append(request.method)
        return super().handle_request(request)


def main() -> int:
    try:
        settings = load_settings()
    except ConfigurationError as error:
        print(error)
        return 2
    transport = CountingTransport()
    scope = ErpNextScope(
        price_list=settings.erpnext_price_list,
        warehouse=settings.erpnext_warehouse,
        order_id_field=settings.erpnext_order_id_field,
    )
    client = erpnext_client(
        str(settings.erpnext_url),
        settings.erpnext_api_key,
        settings.erpnext_api_secret,
        transport=transport,
    )
    try:
        with client:
            data = fetch_erp_data(client, scope)
    except (ConfigurationError, SourceError, ReadOnlyViolation) as error:
        print(f"FAILED: {type(error).__name__}: {error}")
        print(f"requests sent: {len(transport.methods)} ({_methods(transport.methods)})")
        return 2

    options = settings.to_options(datetime.now(UTC))
    in_scope = {i.item_code for i in data.items if is_in_scope(i, options)}
    scoped_prices = sum(p.item_code in in_scope for p in data.prices)
    scoped_bins = sum(b.item_code in in_scope for b in data.bins)
    rows = [
        ("credentials accepted", "yes"),
        ("price list found, enabled, selling", f"yes (currency {data.price_list_currency})"),
        ("warehouse found, not a group", "yes"),
        ("items read", f"{len(data.items)} (in scope: {len(in_scope)})"),
        (
            "item price rows on the list",
            f"{len(data.prices)} (for in-scope items: {scoped_prices})",
        ),
        ("bin rows in the warehouse", f"{len(data.bins)} (for in-scope items: {scoped_bins})"),
        ("order mapping", _orders(settings.erpnext_order_id_field, data.sales_orders)),
        ("requests sent", f"{len(transport.methods)} ({_methods(transport.methods)})"),
    ]
    print("ERPNext read-only smoke test")
    for label, value in rows:
        print(f"  {label:.<40} {value}")
    counts = {"in-scope items": len(in_scope), "prices": scoped_prices, "bins": scoped_bins}
    empty = [name for name, count in counts.items() if count == 0]
    if empty:
        print(
            "WARNING: no " + ", ".join(empty) + " found. The connection works, but there is "
            "not enough test data to show that reconciliation is correct."
        )
    return 0


def _methods(methods: list[str]) -> str:
    return ", ".join(f"{m}: {methods.count(m)}" for m in sorted(set(methods))) or "none"


def _orders(field: str | None, sales_orders: tuple | None) -> str:
    if field is None:
        return "not configured (order check skipped)"
    if sales_orders is None:
        return "field not found (order check skipped)"
    return f"field found; sales orders with it set: {len(sales_orders)}"


if __name__ == "__main__":
    sys.exit(main())
