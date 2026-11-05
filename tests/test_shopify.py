"""Shopify client against recorded fake responses (tests/fixtures/shopify, generated data)."""

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from builders import AS_OF, item, options, price, stock_bin
from graphql import parse
from graphql.language import OperationDefinitionNode
from pydantic import SecretStr

from reconciler.models import CheckName, DiscrepancyType, ErpData
from reconciler.reconcile import ConfigurationError, reconcile
from reconciler.sources.http import IncompleteResponse, RetryPolicy, SourceError, shopify_client
from reconciler.sources.shopify import ShopifyScope, fetch_shopify_data, order_search

FIXTURES = Path(__file__).parent / "fixtures" / "shopify"
STORE = "test-store.myshopify.com"
VERSION = "2026-07"
GRAPHQL = f"/admin/api/{VERSION}/graphql.json"
TOKEN_PATH = "/admin/oauth/access_token"  # noqa: S105 - a URL path, not a secret
LOCATION = "gid://shopify/Location/123"
FETCHED_AT = datetime(2026, 11, 1, 11, 59, tzinfo=UTC)
SINCE = AS_OF - timedelta(days=30)
THROTTLED = {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeShopify:
    """Answers the token exchange and routes GraphQL by operation name. Connections are paged
    by `first` / `after` (cursor = index of the next node)."""

    def __init__(
        self,
        overrides: dict[str, object] | None = None,
        throttle_once: tuple[str, str] | None = None,
    ) -> None:
        self.singles: dict[str, object] = {
            "AccessScopes": _load("access_scopes.json"),
            "Shop": _load("shop.json"),
        }
        self.connections: dict[str, tuple[str, list[dict]]] = {
            "Variants": ("productVariants", _load("variants.json")["nodes"]),
            "Orders": ("orders", _load("orders.json")["nodes"]),
        }
        self.overrides = overrides or {}
        self.throttle_once = throttle_once
        self.token_requests: list[httpx.Request] = []
        self.requests: list[httpx.Request] = []
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            self.token_requests.append(request)
            scope = "read_products,read_inventory,read_orders"
            return _ok({"access_token": "FAKE-ACCESS", "scope": scope, "expires_in": 86399})
        self.requests.append(request)
        body = json.loads(request.content)
        name = _operation_name(body["query"])
        variables = body.get("variables") or {}
        self.calls.append((name, variables))
        if (name, variables.get("after")) == self.throttle_once:
            self.throttle_once = None
            return _ok(THROTTLED)
        answer = self.overrides.get(name)
        if isinstance(answer, int):  # an HTTP status to answer with
            return httpx.Response(answer, json={"errors": "denied"})
        if answer is not None:
            return _ok(answer)
        if name in self.singles:
            return _ok(self.singles[name])
        field, nodes = self.connections[name]
        start = int(variables.get("after") or 0)
        end = start + variables["first"]
        page = {"hasNextPage": end < len(nodes), "endCursor": str(end)}
        return _ok({"data": {field: {"nodes": nodes[start:end], "pageInfo": page}}})

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def variables(self, name: str) -> list[dict]:
        return [v for n, v in self.calls if n == name]


def _operation_name(document: str) -> str:
    [operation] = [d for d in parse(document).definitions if isinstance(d, OperationDefinitionNode)]
    assert operation.name is not None
    return operation.name.value


def _ok(body: object) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(body).encode())


def _fetch(fake: FakeShopify, page_size: int = 100, sleeps: list[float] | None = None):
    graphql = shopify_client(
        STORE,
        SecretStr("FAKE-CLIENT-ID"),
        SecretStr("FAKE-CLIENT-SECRET"),
        VERSION,
        retry=RetryPolicy(max_attempts=3, sleep=(sleeps if sleeps is not None else []).append),
        transport=httpx.MockTransport(fake),
    )
    scope = ShopifyScope(
        location_id=LOCATION,
        order_since=SINCE,
        variant_page_size=page_size,
        order_page_size=page_size,
    )
    with graphql:
        return fetch_shopify_data(graphql, scope, now=lambda: FETCHED_AT)


def _variants_with(**changes: object) -> dict:
    """The variants fixture with the first node's fields changed (None removes a field)."""
    nodes = _load("variants.json")["nodes"]
    for key, value in changes.items():
        if value is None:
            del nodes[0][key]
        else:
            nodes[0][key] = value
    return {"data": {"productVariants": {"nodes": nodes, "pageInfo": _last_page()}}}


def _last_page() -> dict:
    return {"hasNextPage": False, "endCursor": None}


# --- Happy path ----------------------------------------------------------------------------


def test_fetch_builds_complete_shopify_data() -> None:
    data = _fetch(FakeShopify())

    assert data.fetched_at == FETCHED_AT
    assert (data.shop.currency, data.shop.taxes_included) == ("CAD", False)
    assert [v.sku for v in data.variants] == [
        "TEST-SKU-1", "TEST-SKU-2", "TEST-SKU-4", None, "TEST-SKU-3"
    ]  # fmt: skip
    assert [v.on_hand for v in data.variants] == [12, None, 0, 1, 3]
    assert [v.inventory_tracked for v in data.variants] == [True, True, False, True, True]
    assert data.variants[0].price == Decimal("19.99")
    assert all(isinstance(v.price, Decimal) for v in data.variants)
    assert [o.order_id for o in data.orders] == [
        f"gid://shopify/Order/{n}" for n in range(1001, 1006)
    ]
    assert [o.cancelled for o in data.orders] == [False, True, False, False, False]
    assert [o.test for o in data.orders] == [False, False, False, True, False]
    assert data.orders[4].financial_status == "UNKNOWN"  # null in Shopify: never PAID
    assert data.orders[4].created_at == datetime(2026, 10, 31, 18, 0, tzinfo=UTC)


def test_draft_and_archived_products_are_excluded() -> None:
    skus = {v.sku for v in _fetch(FakeShopify()).variants}

    assert "TEST-DRAFT-1" not in skus
    assert "TEST-ARCH-1" not in skus


def test_only_one_token_request_then_graphql_queries() -> None:
    fake = FakeShopify()

    _fetch(fake)

    assert len(fake.token_requests) == 1
    assert {(r.method, r.url.path) for r in fake.requests} == {("POST", GRAPHQL)}
    assert fake.names() == ["AccessScopes", "Shop", "Variants", "Orders"]


def test_queries_are_scoped_to_the_location_and_lookback() -> None:
    fake = FakeShopify()

    _fetch(fake)

    [variants] = fake.variables("Variants")
    [orders] = fake.variables("Orders")
    assert variants["locationId"] == LOCATION
    assert orders["query"] == "created_at:>='2026-10-02T12:00:00Z'"


def test_every_variant_and_order_page_is_read() -> None:
    fake = FakeShopify()

    data = _fetch(fake, page_size=3)

    assert [v["after"] for v in fake.variables("Variants")] == [None, "3", "6"]
    assert [v["after"] for v in fake.variables("Orders")] == [None, "3"]
    assert len(data.variants) == 5
    assert len(data.orders) == 5


def test_throttled_page_is_retried_without_losing_data() -> None:
    fake = FakeShopify(throttle_once=("Variants", "3"))
    sleeps: list[float] = []

    data = _fetch(fake, page_size=3, sleeps=sleeps)

    assert [v["after"] for v in fake.variables("Variants")] == [None, "3", "3", "6"]
    assert len(sleeps) == 1
    assert len(data.variants) == 5


# --- Scopes and location -------------------------------------------------------------------


def _scopes(*handles: str) -> dict:
    nodes = [{"handle": h} for h in handles]
    return {"data": {"currentAppInstallation": {"accessScopes": nodes}}}


@pytest.mark.parametrize(
    ("handles", "named"),
    [
        (("read_products", "read_inventory", "read_orders", "write_products"), "write_products"),
        (("read_products", "read_inventory", "read_orders", "read_customers"), "read_customers"),
        (("read_products", "read_inventory"), "read_orders"),
    ],
)
def test_wrong_scopes_stop_before_any_data_is_read(handles: tuple[str, ...], named: str) -> None:
    fake = FakeShopify({"AccessScopes": _scopes(*handles)})

    with pytest.raises(ConfigurationError, match=named):
        _fetch(fake)

    assert fake.names() == ["AccessScopes"]


@pytest.mark.parametrize(
    "body",
    [
        {"data": {"currentAppInstallation": None}},
        {"data": {"currentAppInstallation": {"accessScopes": [{"name": "read_orders"}]}}},
    ],
)
def test_unreadable_scopes_are_incomplete(body: dict) -> None:
    with pytest.raises(IncompleteResponse):
        _fetch(FakeShopify({"AccessScopes": body}))


def test_unconfirmed_location_stops_the_run() -> None:
    nodes = _load("variants.json")["nodes"]
    for node in nodes:
        node["inventoryItem"]["inventoryLevel"] = None
    body = {"data": {"productVariants": {"nodes": nodes, "pageInfo": _last_page()}}}

    with pytest.raises(ConfigurationError, match="SHOPIFY_LOCATION_ID could not be confirmed"):
        _fetch(FakeShopify({"Variants": body}))


def test_location_stocked_only_by_a_draft_product_still_counts_as_confirmed() -> None:
    nodes = _load("variants.json")["nodes"]
    for node in nodes:
        if node["product"]["status"] != "DRAFT":
            node["inventoryItem"]["inventoryLevel"] = None
    body = {"data": {"productVariants": {"nodes": nodes, "pageInfo": _last_page()}}}

    data = _fetch(FakeShopify({"Variants": body}))

    assert all(v.on_hand is None for v in data.variants)


# --- Errors --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"price": None},  # field removed
        {"price": "not a price"},
        {"product": {}},
        {"sku": ["TEST-SKU-1"]},
        {"inventoryItem": None},
        {
            "inventoryItem": {
                "tracked": True,
                "inventoryLevel": {"quantities": [{"name": "available", "quantity": 1}]},
            }
        },
        {
            "inventoryItem": {
                "tracked": True,
                "inventoryLevel": {"quantities": [{"name": "on_hand", "quantity": 1.5}]},
            }
        },
        {
            "inventoryItem": {
                "tracked": True,
                "inventoryLevel": {"quantities": [{"name": "on_hand", "quantity": "12"}]},
            }
        },
        {"inventoryItem": {"tracked": True, "inventoryLevel": []}},
    ],
)
def test_malformed_variant_fails_without_echoing_values(changes: dict) -> None:
    with pytest.raises(IncompleteResponse) as error:
        _fetch(FakeShopify({"Variants": _variants_with(**changes)}))

    assert "TEST-SKU" not in str(error.value)
    assert "not a price" not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "gid://shopify/DraftOrder/1"},
        {"id": None},
        {"createdAt": "2026-10-30T15:00:00"},  # no timezone
        {"cancelledAt": 5},
        {"test": None},
    ],
)
def test_malformed_order_is_incomplete(changes: dict) -> None:
    nodes = _load("orders.json")["nodes"]
    nodes[0].update(changes)
    body = {"data": {"orders": {"nodes": nodes, "pageInfo": _last_page()}}}

    with pytest.raises(IncompleteResponse):
        _fetch(FakeShopify({"Orders": body}))


@pytest.mark.parametrize("name", ["Shop", "Variants", "Orders"])
def test_access_denied_is_a_configuration_error(name: str) -> None:
    denied = {"errors": [{"message": "Access denied", "extensions": {"code": "ACCESS_DENIED"}}]}

    with pytest.raises(ConfigurationError, match="ACCESS_DENIED"):
        _fetch(FakeShopify({name: denied}))


@pytest.mark.parametrize("status", [401, 403])
def test_http_permission_errors_are_configuration_errors(status: int) -> None:
    with pytest.raises(ConfigurationError, match=f"HTTP {status}"):
        _fetch(FakeShopify({"Shop": status}))


def test_other_graphql_errors_stay_source_errors() -> None:
    failed = {"errors": [{"message": "boom", "extensions": {"code": "INTERNAL_SERVER_ERROR"}}]}

    with pytest.raises(SourceError) as error:
        _fetch(FakeShopify({"Orders": failed}))

    assert not isinstance(error.value, ConfigurationError)
    assert "boom" not in str(error.value)


def test_missing_shop_is_incomplete() -> None:
    with pytest.raises(IncompleteResponse):
        _fetch(FakeShopify({"Shop": {"data": {"shop": None}}}))


# --- Order search --------------------------------------------------------------------------


def test_order_search_is_in_utc_whole_seconds() -> None:
    since = datetime(2026, 10, 2, 6, 0, 30, 999, tzinfo=timezone(timedelta(hours=-6)))

    assert order_search(since) == "created_at:>='2026-10-02T12:00:30Z'"


def test_order_search_needs_a_timezone() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        order_search(datetime(2026, 10, 2))  # deliberately naive


# --- End to end with reconcile() -----------------------------------------------------------


def test_fetched_data_reconciles_against_erpnext() -> None:
    shopify = _fetch(FakeShopify())
    codes = ("TEST-SKU-1", "TEST-SKU-2", "TEST-SKU-3", "TEST-SKU-4")
    erp = ErpData(
        fetched_at=FETCHED_AT,
        price_list_currency="CAD",
        items=tuple(item(code) for code in codes),
        prices=(
            price("TEST-SKU-1", "19.99"),
            price("TEST-SKU-2", "14.00"),
            price("TEST-SKU-3", "25.00"),
            price("TEST-SKU-4", "9.95"),
        ),
        bins=(stock_bin("TEST-SKU-1", "12"), stock_bin("TEST-SKU-3", "5")),
        warehouse_confirmed=True,
        bins_complete=True,
        sales_orders=(),
    )

    result = reconcile(shopify, erp, options())

    found = {(d.type, d.key) for d in result.discrepancies}
    assert found == {
        (DiscrepancyType.SKU_EMPTY, "gid://shopify/ProductVariant/4"),
        (DiscrepancyType.PRICE_MISMATCH, "TEST-SKU-2"),
        (DiscrepancyType.STOCK_MISMATCH, "TEST-SKU-3"),
        (DiscrepancyType.ORDER_NOT_IN_ERP, "gid://shopify/Order/1001"),
    }
    stock_skips = {s.key for s in result.skipped if s.check is CheckName.STOCK}
    assert stock_skips == {"TEST-SKU-2", "TEST-SKU-4"}  # not stocked / not tracked
