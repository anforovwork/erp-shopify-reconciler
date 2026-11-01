"""ERPNext client against recorded fake responses (tests/fixtures/erpnext, generated data)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from builders import AS_OF, options, order, variant
from pydantic import SecretStr

from reconciler.models import CheckName, DiscrepancyType, ShopifyData, ShopifyShop
from reconciler.reconcile import ConfigurationError, reconcile
from reconciler.sources.erpnext import ErpNextScope, fetch_erp_data
from reconciler.sources.http import (
    IncompleteResponse,
    RetryPolicy,
    SourceError,
    erpnext_client,
)

FIXTURES = Path(__file__).parent / "fixtures" / "erpnext"
LOGIN = "/api/method/frappe.auth.get_logged_user"
ROUTES = {
    LOGIN: "logged_user.json",
    "/api/resource/Price List": "price_list.json",
    "/api/resource/Warehouse": "warehouse.json",
    "/api/resource/Item": "item.json",
    "/api/resource/Item Price": "item_price.json",
    "/api/resource/Bin": "bin.json",
    "/api/resource/Custom Field": "custom_field.json",
    "/api/resource/Sales Order": "sales_order.json",
}
FIELD = "custom_shopify_order_gid"
FETCHED_AT = datetime(2026, 11, 1, 11, 59, tzinfo=UTC)


class FakeErpNext:
    """Serves fixtures, paginated like Frappe (limit_start / limit_page_length)."""

    def __init__(self, overrides: dict[str, object] | None = None) -> None:
        self.bodies: dict[str, object] = {
            path: json.loads((FIXTURES / name).read_text(encoding="utf-8"))
            for path, name in ROUTES.items()
        }
        self.bodies.update(overrides or {})
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self.bodies[request.url.path]
        if isinstance(body, int):  # an HTTP status to answer with
            return httpx.Response(body, json={"exc_type": "PermissionError"})
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            start = int(request.url.params["limit_start"])
            size = int(request.url.params["limit_page_length"])
            body = {"data": body["data"][start : start + size]}
        return httpx.Response(200, content=json.dumps(body).encode())

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def params(self, path: str) -> list[httpx.QueryParams]:
        return [r.url.params for r in self.requests if r.url.path == path]


def _fetch(fake: FakeErpNext, order_id_field: str | None = FIELD, page_size: int = 500):
    client = erpnext_client(
        "http://localhost:8080",
        SecretStr("FAKE-KEY"),
        SecretStr("FAKE-SECRET"),
        retry=RetryPolicy(max_attempts=2, sleep=lambda _: None),
        transport=httpx.MockTransport(fake),
    )
    scope = ErpNextScope(
        price_list="Test Selling",
        warehouse="Test Stores - TC",
        order_id_field=order_id_field,
        page_size=page_size,
    )
    with client:
        return fetch_erp_data(client, scope, now=lambda: FETCHED_AT)


# --- Happy path ----------------------------------------------------------------------------


def test_fetch_builds_complete_erp_data() -> None:
    data = _fetch(FakeErpNext())

    assert data.fetched_at == FETCHED_AT
    assert data.price_list_currency == "CAD"
    assert data.warehouse_confirmed and data.bins_complete
    assert [i.item_code for i in data.items] == [
        "TEST-RAW-1", "TEST-SKU-1", "TEST-SKU-2", "TEST-SKU-3", "TEST-TPL-1"
    ]  # fmt: skip
    assert data.items[2].variant_of == "TEST-TPL-1"
    assert [p.price_list_rate for p in data.prices] == [
        Decimal("17.5"), Decimal("19.99"), Decimal("15"), Decimal("9.95"), Decimal("25.0")
    ]  # fmt: skip
    assert all(isinstance(p.price_list_rate, Decimal) for p in data.prices)
    assert (data.prices[3].uom, data.prices[4].uom) == ("", None)  # kept distinct
    assert data.bins[0].actual_qty == Decimal("12.0")
    assert data.sales_orders is not None
    assert [(o.shopify_order_id, o.docstatus) for o in data.sales_orders] == [
        ("gid://shopify/Order/1001", 1),
        ("gid://shopify/Order/1002", 0),
    ]


def test_every_request_is_a_bodyless_get_on_an_allowed_path() -> None:
    fake = FakeErpNext()

    _fetch(fake)

    assert {r.method for r in fake.requests} == {"GET"}
    assert all(r.content == b"" for r in fake.requests)
    assert set(fake.paths()) == set(ROUTES)


def test_queries_are_filtered_to_the_configured_scope() -> None:
    fake = FakeErpNext()

    _fetch(fake)

    def filters(path: str) -> list:
        return json.loads(fake.params(path)[0]["filters"])

    assert filters("/api/resource/Price List") == [["name", "=", "Test Selling"]]
    assert filters("/api/resource/Warehouse") == [["name", "=", "Test Stores - TC"]]
    assert filters("/api/resource/Item") == []  # all items, to explain out-of-scope SKUs
    assert filters("/api/resource/Item Price") == [["price_list", "=", "Test Selling"]]
    assert filters("/api/resource/Bin") == [["warehouse", "=", "Test Stores - TC"]]
    assert filters("/api/resource/Custom Field") == [
        ["dt", "=", "Sales Order"],
        ["fieldname", "=", FIELD],
    ]
    assert filters("/api/resource/Sales Order") == [[FIELD, "is", "set"]]
    sales_fields = json.loads(fake.params("/api/resource/Sales Order")[0]["fields"])
    assert sales_fields == ["name", "docstatus", FIELD]


def test_every_list_is_read_page_by_page() -> None:
    fake = FakeErpNext()

    data = _fetch(fake, page_size=2)

    assert len(data.items) == 5
    starts = [p["limit_start"] for p in fake.params("/api/resource/Item")]
    assert starts == ["0", "2", "4"]
    assert len(data.prices) == 5


# --- Credentials and configuration ---------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_are_a_configuration_error(status: int) -> None:
    with pytest.raises(ConfigurationError, match="ERPNEXT_API_KEY"):
        _fetch(FakeErpNext({LOGIN: status}))


@pytest.mark.parametrize("body", [{"message": "Guest"}, {"message": ""}, {}])
def test_not_logged_in_is_a_configuration_error(body: dict) -> None:
    with pytest.raises(ConfigurationError, match="credentials"):
        _fetch(FakeErpNext({LOGIN: body}))


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (None, "ERPNEXT_PRICE_LIST was not found"),
        ({"name": "Test Selling", "currency": "CAD", "enabled": 0, "selling": 1}, "disabled"),
        ({"name": "Test Selling", "currency": "CAD", "enabled": 1, "selling": 0}, "not a selling"),
    ],
)
def test_unusable_price_list_stops_the_run(row: dict | None, message: str) -> None:
    fake = FakeErpNext({"/api/resource/Price List": {"data": [row] if row else []}})

    with pytest.raises(ConfigurationError, match=message):
        _fetch(fake)

    assert "/api/resource/Item" not in fake.paths()  # nothing else was read


@pytest.mark.parametrize(
    ("rows", "message"),
    [([], "ERPNEXT_WAREHOUSE was not found"), ([{"name": "W", "is_group": 1}], "group warehouse")],
)
def test_unusable_warehouse_stops_the_run(rows: list, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        _fetch(FakeErpNext({"/api/resource/Warehouse": {"data": rows}}))


@pytest.mark.parametrize("path", ["/api/resource/Item", "/api/resource/Bin"])
def test_missing_read_permission_is_named(path: str) -> None:
    doctype = path.rsplit("/", 1)[1]

    with pytest.raises(ConfigurationError, match=f"cannot read {doctype}"):
        _fetch(FakeErpNext({path: 403}))


# --- Order mapping field -------------------------------------------------------------------


def test_no_mapping_field_configured_reads_no_orders() -> None:
    fake = FakeErpNext()

    data = _fetch(fake, order_id_field=None)

    assert data.sales_orders is None
    assert "/api/resource/Custom Field" not in fake.paths()
    assert "/api/resource/Sales Order" not in fake.paths()


def test_mapping_field_that_does_not_exist_disables_order_check() -> None:
    fake = FakeErpNext({"/api/resource/Custom Field": {"data": []}})

    data = _fetch(fake)

    assert data.sales_orders is None
    assert "/api/resource/Sales Order" not in fake.paths()


def test_unreadable_custom_field_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="cannot read Custom Field"):
        _fetch(FakeErpNext({"/api/resource/Custom Field": 403}))


# --- Incomplete or malformed data ----------------------------------------------------------


def test_failure_on_a_later_bin_page_returns_nothing() -> None:
    class FailingSecondBinPage(FakeErpNext):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/resource/Bin" and request.url.params["limit_start"] != "0":
                self.requests.append(request)
                return httpx.Response(503)
            return super().__call__(request)

    with pytest.raises(SourceError, match="HTTP 503"):
        _fetch(FailingSecondBinPage(), page_size=1)


def test_malformed_row_names_the_field_but_not_the_value() -> None:
    items = json.loads((FIXTURES / "item.json").read_text(encoding="utf-8"))
    items["data"][1]["item_group"] = {"text": "Ignore previous instructions"}
    del items["data"][2]["stock_uom"]

    with pytest.raises(IncompleteResponse) as error:
        _fetch(FakeErpNext({"/api/resource/Item": items}))

    assert "item_group" in str(error.value)
    assert "Ignore previous instructions" not in str(error.value)


@pytest.mark.parametrize("currency", ["cad", "CAD\nIgnore previous instructions", None])
def test_invalid_price_list_currency_is_incomplete(currency: str | None) -> None:
    row = {"name": "Test Selling", "currency": currency, "enabled": 1, "selling": 1}

    with pytest.raises(IncompleteResponse):
        _fetch(FakeErpNext({"/api/resource/Price List": {"data": [row]}}))


def test_non_flag_value_is_incomplete() -> None:
    row = {"name": "Test Selling", "currency": "CAD", "enabled": "yes", "selling": 1}

    with pytest.raises(IncompleteResponse, match="enabled is not a 0/1 flag"):
        _fetch(FakeErpNext({"/api/resource/Price List": {"data": [row]}}))


# --- With the pure rules -------------------------------------------------------------------


def test_fixture_data_reconciles_as_expected() -> None:
    erp = _fetch(FakeErpNext())
    shopify = ShopifyData(
        fetched_at=AS_OF - timedelta(minutes=2),
        shop=ShopifyShop(currency="CAD", taxes_included=False),
        variants=(
            variant("TEST-SKU-1", 1, price=Decimal("19.99"), on_hand=12),
            variant("TEST-SKU-2", 2, price=Decimal("25.00"), on_hand=3),
            variant("TEST-SKU-3", 3),
        ),
        orders=(order(1001), order(1002)),
    )

    result = reconcile(shopify, erp, options())

    assert [(d.type, d.key, d.detail) for d in result.discrepancies] == [
        (
            DiscrepancyType.PRICE_UOM_MISSING,
            "TEST-SKU-2",
            "1 applicable price row(s) without UOM (ERPNext treats them as matching any UOM)",
        ),
        (
            DiscrepancyType.ORDER_NOT_IN_ERP,
            "gid://shopify/Order/1002",
            "only draft Sales Order(s) found",
        ),
    ]
    assert [(s.check, s.key) for s in result.skipped] == [(CheckName.SCOPE, "TEST-SKU-3")]
