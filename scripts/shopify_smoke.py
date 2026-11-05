"""Read-only smoke test of the Shopify client against a real Shopify store.

Usage (from the repository root):  .venv/bin/python scripts/shopify_smoke.py

Reads settings from .env. Prints only counts and yes/no checks — never settings, credentials,
tokens, scopes, currency codes, ids, SKUs, prices or URLs. Every request goes through the
read-only HTTP layer, so only the token exchange and GraphQL queries can be sent.
"""

import sys
from datetime import UTC, datetime, timedelta

import httpx

from reconciler.config import Settings, load_settings
from reconciler.models import ShopifyData
from reconciler.reconcile import ConfigurationError, is_eligible_order
from reconciler.sources.http import (
    SHOPIFY_TOKEN_PATH,
    ReadOnlyViolation,
    SourceError,
    shopify_client,
)
from reconciler.sources.shopify import ShopifyScope, fetch_shopify_data


class CountingTransport(httpx.BaseTransport):
    """Counts requests that passed the read-only guard, by kind (token / GraphQL)."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner
        self.kinds: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.kinds.append("token" if request.url.path == SHOPIFY_TOKEN_PATH else "graphql")
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def main() -> int:
    try:
        settings = load_settings()
    except ConfigurationError as error:
        print(error)
        return 2
    transport = CountingTransport(httpx.HTTPTransport())
    as_of = datetime.now(UTC)
    options = settings.to_options(as_of)
    scope = ShopifyScope(
        location_id=settings.shopify_location_id,
        order_since=as_of - timedelta(days=settings.order_lookback_days),
    )
    graphql = shopify_client(
        settings.shopify_store_domain,
        settings.shopify_client_id,
        settings.shopify_client_secret,
        settings.shopify_api_version,
        transport=transport,
    )
    try:
        with graphql:
            data = fetch_shopify_data(graphql, scope)
    except (ConfigurationError, SourceError, ReadOnlyViolation) as error:
        print(f"FAILED: {type(error).__name__}: {error}")
        print(f"requests sent: {len(transport.kinds)} ({_kinds(transport.kinds)})")
        return 2

    variants = data.variants
    with_sku = sum(bool((v.sku or "").strip()) for v in variants)
    tracked = sum(v.inventory_tracked for v in variants)
    stocked = sum(v.on_hand is not None for v in variants)
    eligible = sum(is_eligible_order(o, options) for o in data.orders)
    rows = [
        ("access token obtained", _yes(transport.kinds.count("token") >= 1)),
        ("scopes exactly the three read scopes", "yes"),  # fetch_shopify_data raised otherwise
        ("shop currency equals CURRENCY", _yes(data.shop.currency == settings.currency)),
        ("taxesIncluded equals ERPNEXT_PRICES_INCLUDE_TAX", _tax(data, settings)),
        ("location confirmed", "yes"),  # fetch_shopify_data raised otherwise
        (
            "active variants read",
            f"{len(variants)} (with SKU: {with_sku}, tracked: {tracked}, "
            f"stocked at location: {stocked})",
        ),
        (
            f"orders in the last {settings.order_lookback_days} days",
            f"{len(data.orders)} (eligible: {eligible})",
        ),
        ("requests sent", f"{len(transport.kinds)} ({_kinds(transport.kinds)})"),
    ]
    print("Shopify read-only smoke test")
    for label, value in rows:
        print(f"  {label:.<50} {value}")
    counts = {
        "variants with a SKU": with_sku,
        "stocked variants": stocked,
        "eligible orders": eligible,
    }
    empty = [name for name, count in counts.items() if count == 0]
    if empty:
        print(
            "WARNING: no " + ", ".join(empty) + " found. The connection works, but there is "
            "not enough test data to show that reconciliation is correct."
        )
    return 0


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def _tax(data: ShopifyData, settings: Settings) -> str:
    configured = settings.erpnext_prices_include_tax
    if configured is None:
        return "not set (prices will not be compared)"
    return _yes(data.shop.taxes_included == configured)


def _kinds(kinds: list[str]) -> str:
    return ", ".join(f"{k}: {kinds.count(k)}" for k in sorted(set(kinds))) or "none"


if __name__ == "__main__":
    sys.exit(main())
