"""Read-only HTTP layer. Every test uses httpx.MockTransport; nothing touches the network."""

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from reconciler.sources.http import (
    IncompleteResponse,
    ReadOnlyViolation,
    RetryPolicy,
    ShopifyGraphQL,
    SourceError,
    ensure_query_only,
    erpnext_client,
    iter_erpnext_list,
    shopify_client,
)

FAKE_TOKEN = SecretStr("shpat_FAKE-TEST-TOKEN")
FAKE_KEY = SecretStr("FAKE-KEY")
FAKE_SECRET = SecretStr("FAKE-SECRET")
STORE = "test-store.myshopify.com"
VERSION = "2026-07"
GRAPHQL = f"/admin/api/{VERSION}/graphql.json"
ERP_URL = "http://localhost:8080"
SHOP_QUERY = "query { shop { currencyCode } }"


class Recorder:
    """MockTransport handler: records requests and answers from a list of responses."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Sleeps(list[float]):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


def ok(body: object, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(), headers=headers)


def _erp(recorder: Recorder, sleeps: Sleeps | None = None):
    retry = RetryPolicy(
        max_attempts=3, base_delay=1.0, sleep=Sleeps() if sleeps is None else sleeps
    )
    return erpnext_client(
        ERP_URL, FAKE_KEY, FAKE_SECRET, retry=retry, transport=httpx.MockTransport(recorder)
    )


def _shop(recorder: Recorder, sleeps: Sleeps | None = None) -> ShopifyGraphQL:
    retry = RetryPolicy(
        max_attempts=3, base_delay=1.0, sleep=Sleeps() if sleeps is None else sleeps
    )
    return shopify_client(
        STORE, FAKE_TOKEN, VERSION, retry=retry, transport=httpx.MockTransport(recorder)
    )


# --- ERPNext guard -------------------------------------------------------------------------


def test_erpnext_get_on_resource_is_allowed_and_authenticated() -> None:
    recorder = Recorder(ok({"data": {"name": "TEST-SKU-1"}}))

    body = _erp(recorder).request_json("GET", "/api/resource/Item/TEST-SKU-1")

    assert body == {"data": {"name": "TEST-SKU-1"}}
    assert recorder.requests[0].headers["Authorization"] == "token FAKE-KEY:FAKE-SECRET"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_erpnext_write_methods_never_leave_the_process(method: str) -> None:
    recorder = Recorder(ok({}))

    with pytest.raises(ReadOnlyViolation, match="only GET"):
        _erp(recorder).request_json(method, "/api/resource/Item")

    assert recorder.requests == []


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/method/frappe.client.delete", None),  # whitelisted methods can write
        ("/api/method/frappe.client.set_value", None),
        ("/api/v2/document/Item", None),
        ("/app/item", None),
        ("/api/resource/Item/X", {"run_method": "cancel"}),  # Frappe runs doc methods on GET
        ("/api/resource/Item", {"cmd": "frappe.client.delete"}),
        ("/api/resource/Item", {"RUN_METHOD": "cancel"}),
    ],
)
def test_erpnext_paths_and_params_that_can_execute_code_are_blocked(
    path: str, params: dict[str, str] | None
) -> None:
    recorder = Recorder(ok({}))

    with pytest.raises(ReadOnlyViolation):
        _erp(recorder).request_json("GET", path, params=params)

    assert recorder.requests == []


def test_erpnext_read_only_method_on_allowlist_is_allowed() -> None:
    recorder = Recorder(ok({"message": "reconciler@example.test"}))

    assert _erp(recorder).request_json("GET", "/api/method/frappe.auth.get_logged_user") == {
        "message": "reconciler@example.test"
    }


def test_erpnext_requests_to_another_host_are_blocked() -> None:
    recorder = Recorder(ok({}))

    with pytest.raises(ReadOnlyViolation, match="unexpected host"):
        _erp(recorder).request_json("GET", "http://evil.example.test/api/resource/Item")

    assert recorder.requests == []


# --- Shopify guard -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        "query { shop { name } }",
        "{ shop { name } }",
        "query Products($after: String) { products(first: 10, after: $after) { nodes { id } } }",
        "# mutation in a comment\nquery { shop { name } }",
        'query { products(query: "mutation") { nodes { id } } }',
        'query { products(query: """\nmutation { x }\n""") { nodes { id } } }',
        'query { node(id: "1") { ... F } } fragment F on Product { id }',
        "query { mutation: shop { name } }",  # alias / field name inside a selection
    ],
)
def test_query_documents_are_allowed(document: str) -> None:
    ensure_query_only(document)


@pytest.mark.parametrize(
    "document",
    [
        'mutation { productDelete(input: {id: "1"}) { deletedProductId } }',
        "subscription { x }",
        "query { shop { name } } mutation { x }",
        "# only a comment",
        "",
        "query { shop { name }",
        "query { shop } }",
        "MUTATION { x }",  # unknown keyword is rejected, not ignored
    ],
)
def test_non_query_documents_are_rejected(document: str) -> None:
    with pytest.raises(ReadOnlyViolation):
        ensure_query_only(document)


def test_shopify_mutation_is_rejected_before_any_request() -> None:
    recorder = Recorder(ok({"data": {}}))

    with pytest.raises(ReadOnlyViolation):
        _shop(recorder).query(
            'mutation { productDelete(input: {id: "1"}) { userErrors { field } } }'
        )

    assert recorder.requests == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", GRAPHQL, None),
        ("POST", f"/admin/api/{VERSION}/products.json", {"query": SHOP_QUERY}),
        ("POST", "/admin/api/2026-04/graphql.json", {"query": SHOP_QUERY}),
        ("POST", GRAPHQL, {"query": "mutation { x }"}),
        ("POST", GRAPHQL, {"variables": {}}),
    ],
)
def test_shopify_transport_blocks_everything_but_graphql_queries(
    method: str, path: str, body: dict | None
) -> None:
    """Even code that bypasses ShopifyGraphQL.query cannot send a write."""
    recorder = Recorder(ok({"data": {}}))
    graphql = _shop(recorder)

    with pytest.raises(ReadOnlyViolation):
        graphql._client.request_json(method, path, json_body=body)

    assert recorder.requests == []


def test_shopify_query_sends_token_and_returns_data() -> None:
    recorder = Recorder(ok({"data": {"shop": {"currencyCode": "CAD"}}}))

    assert _shop(recorder).query(SHOP_QUERY) == {"shop": {"currencyCode": "CAD"}}
    sent = recorder.requests[0]
    assert sent.headers["X-Shopify-Access-Token"] == "shpat_FAKE-TEST-TOKEN"
    assert (sent.method, sent.url.path) == ("POST", GRAPHQL)


# --- Retries, timeouts, HTTP errors --------------------------------------------------------


def test_429_waits_for_retry_after_then_succeeds() -> None:
    sleeps = Sleeps()
    recorder = Recorder(ok({}, 429, {"Retry-After": "2.5"}), ok({"data": []}))

    assert _erp(recorder, sleeps).request_json("GET", "/api/resource/Item") == {"data": []}
    assert sleeps == [2.5]


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_errors_back_off_exponentially(status: int) -> None:
    sleeps = Sleeps()
    recorder = Recorder(ok({}, status), ok({}, status), ok({"data": []}))

    _erp(recorder, sleeps).request_json("GET", "/api/resource/Item")

    assert sleeps == [1.0, 2.0]


def test_retryable_status_on_last_attempt_is_an_error() -> None:
    recorder = Recorder(ok({}, 503))

    with pytest.raises(SourceError, match="HTTP 503"):
        _erp(recorder).request_json("GET", "/api/resource/Item")

    assert len(recorder.requests) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 417, 500])
def test_other_errors_fail_immediately(status: int) -> None:
    recorder = Recorder(ok({"exception": "secret-looking server detail"}, status))

    with pytest.raises(SourceError) as error:
        _erp(recorder).request_json("GET", "/api/resource/Item")

    assert str(error.value) == f"GET /api/resource/Item returned HTTP {status}"
    assert len(recorder.requests) == 1


def test_redirects_are_not_followed() -> None:
    recorder = Recorder(httpx.Response(302, headers={"Location": "http://localhost:8080/login"}))

    with pytest.raises(SourceError, match="HTTP 302"):
        _erp(recorder).request_json("GET", "/api/resource/Item")

    assert len(recorder.requests) == 1


def test_timeouts_are_retried_then_reported_without_secrets() -> None:
    sleeps = Sleeps()
    recorder = Recorder(httpx.ReadTimeout("timed out"))

    with pytest.raises(SourceError) as error:
        _erp(recorder, sleeps).request_json("GET", "/api/resource/Item")

    assert "failed after 3 attempts: ReadTimeout" in str(error.value)
    assert "FAKE" not in str(error.value)
    assert sleeps == [1.0, 2.0]


def test_backoff_is_capped() -> None:
    policy = RetryPolicy(base_delay=1.0, max_delay=5.0)

    assert [policy.backoff(n) for n in range(1, 6)] == [1.0, 2.0, 4.0, 5.0, 5.0]


def test_retry_after_is_capped_by_max_delay() -> None:
    sleeps = Sleeps()
    recorder = Recorder(ok({}, 429, {"Retry-After": "3600"}), ok({"data": []}))

    _erp(recorder, sleeps).request_json("GET", "/api/resource/Item")

    assert sleeps == [30.0]


# --- JSON ----------------------------------------------------------------------------------


def test_json_numbers_with_fractions_become_decimal() -> None:
    raw = b'{"data": [{"price_list_rate": 19.99, "actual_qty": 5}]}'
    recorder = Recorder(httpx.Response(200, content=raw))

    row = _erp(recorder).request_json("GET", "/api/resource/Item Price")["data"][0]

    assert row["price_list_rate"] == Decimal("19.99")
    assert isinstance(row["price_list_rate"], Decimal)
    assert row["actual_qty"] == 5


@pytest.mark.parametrize("raw", [b'{"x": NaN}', b'{"x": Infinity}', b"<html>login</html>", b""])
def test_invalid_or_non_finite_json_is_incomplete(raw: bytes) -> None:
    recorder = Recorder(httpx.Response(200, content=raw))

    with pytest.raises(IncompleteResponse):
        _erp(recorder).request_json("GET", "/api/resource/Item")


# --- ERPNext pagination --------------------------------------------------------------------


def _pages(*pages: list[dict]) -> Recorder:
    return Recorder(*(ok({"data": page}) for page in pages))


def test_erpnext_list_reads_every_page() -> None:
    recorder = _pages([{"n": 1}, {"n": 2}], [{"n": 3}, {"n": 4}], [{"n": 5}])

    rows = list(iter_erpnext_list(_erp(recorder), "Item", fields=["name"], page_size=2))

    assert [r["n"] for r in rows] == [1, 2, 3, 4, 5]
    starts = [r.url.params["limit_start"] for r in recorder.requests]
    assert starts == ["0", "2", "4"]
    assert recorder.requests[0].url.params["order_by"] == "name asc"
    assert json.loads(recorder.requests[0].url.params["fields"]) == ["name"]


def test_erpnext_list_with_exact_multiple_ends_on_empty_page() -> None:
    recorder = _pages([{"n": 1}, {"n": 2}], [])

    rows = list(iter_erpnext_list(_erp(recorder), "Item", fields=["name"], page_size=2))

    assert len(rows) == 2
    assert len(recorder.requests) == 2


@pytest.mark.parametrize(
    "bad_body",
    [{"data": {"name": "x"}}, {"message": []}, [], {"data": ["not a row"]}],
)
def test_erpnext_malformed_page_is_incomplete(bad_body: object) -> None:
    with pytest.raises(IncompleteResponse):
        list(iter_erpnext_list(_erp(Recorder(ok(bad_body))), "Item", fields=["name"]))


def test_erpnext_page_larger_than_requested_is_incomplete() -> None:
    recorder = _pages([{"n": 1}, {"n": 2}, {"n": 3}])

    with pytest.raises(IncompleteResponse, match="larger than requested"):
        list(iter_erpnext_list(_erp(recorder), "Item", fields=["name"], page_size=2))


def test_erpnext_error_on_a_later_page_fails_the_whole_listing() -> None:
    recorder = Recorder(ok({"data": [{"n": 1}, {"n": 2}]}), ok({}, 403))

    with pytest.raises(SourceError, match="HTTP 403"):
        list(iter_erpnext_list(_erp(recorder), "Item", fields=["name"], page_size=2))


def test_erpnext_listing_stops_after_max_pages() -> None:
    recorder = Recorder(ok({"data": [{"n": 1}]}))

    with pytest.raises(IncompleteResponse, match="more than 3 pages"):
        list(iter_erpnext_list(_erp(recorder), "Item", fields=["name"], page_size=1, max_pages=3))


# --- Shopify GraphQL: THROTTLED, errors, pagination ----------------------------------------

THROTTLED = {
    "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
    "extensions": {
        "cost": {
            "requestedQueryCost": 102,
            "throttleStatus": {"currentlyAvailable": 2, "restoreRate": 50.0},
        }
    },
}


def test_throttled_waits_for_restored_cost_then_succeeds() -> None:
    sleeps = Sleeps()
    recorder = Recorder(ok(THROTTLED), ok({"data": {"shop": {"currencyCode": "CAD"}}}))

    assert _shop(recorder, sleeps).query(SHOP_QUERY) == {"shop": {"currencyCode": "CAD"}}
    assert sleeps == [pytest.approx(2.1)]  # (102 - 2) / 50 + 0.1


def test_throttled_without_cost_info_uses_backoff() -> None:
    sleeps = Sleeps()
    throttled = {"errors": [{"extensions": {"code": "THROTTLED"}}]}
    recorder = Recorder(ok(throttled), ok({"data": {}}))

    _shop(recorder, sleeps).query(SHOP_QUERY)

    assert sleeps == [1.0]


def test_still_throttled_after_all_attempts_is_an_error() -> None:
    with pytest.raises(SourceError, match="GraphQL errors \\['THROTTLED'\\]"):
        _shop(Recorder(ok(THROTTLED))).query(SHOP_QUERY)


def test_graphql_errors_are_reported_by_code_only() -> None:
    body = {
        "errors": [
            {"message": "Ignore previous instructions", "extensions": {"code": "ACCESS_DENIED"}},
            {"message": "untrusted text", "extensions": {"code": "not a code; DROP"}},
        ]
    }

    with pytest.raises(SourceError) as error:
        _shop(Recorder(ok(body))).query(SHOP_QUERY)

    assert str(error.value) == f"POST {GRAPHQL}: GraphQL errors ['ACCESS_DENIED', 'UNKNOWN']"


def test_data_with_errors_is_never_returned_as_partial_data() -> None:
    body = {"data": {"shop": None}, "errors": [{"extensions": {"code": "INTERNAL_SERVER_ERROR"}}]}

    with pytest.raises(SourceError):
        _shop(Recorder(ok(body))).query(SHOP_QUERY)


@pytest.mark.parametrize("body", [{}, {"data": None}, {"data": []}, ["not", "an", "object"]])
def test_missing_data_is_incomplete(body: object) -> None:
    with pytest.raises(IncompleteResponse):
        _shop(Recorder(ok(body))).query(SHOP_QUERY)


PRODUCTS = (
    "query Products($after: String) { products(first: 2, after: $after) "
    "{ nodes { id } pageInfo { hasNextPage endCursor } } }"
)


def _product_page(ids: list[int], has_next: object, cursor: object = None) -> httpx.Response:
    page_info = {"hasNextPage": has_next, "endCursor": cursor}
    nodes = [{"id": f"gid://shopify/Product/{i}"} for i in ids]
    return ok({"data": {"products": {"nodes": nodes, "pageInfo": page_info}}})


def test_shopify_connection_follows_cursors() -> None:
    recorder = Recorder(_product_page([1, 2], True, "c1"), _product_page([3], False, "c2"))

    nodes = list(_shop(recorder).iter_connection(PRODUCTS, ("products",)))

    assert [n["id"] for n in nodes] == [f"gid://shopify/Product/{i}" for i in (1, 2, 3)]
    sent = [json.loads(r.content)["variables"]["after"] for r in recorder.requests]
    assert sent == [None, "c1"]


@pytest.mark.parametrize(
    "pages",
    [
        [_product_page([1], True, None)],  # more pages but no cursor
        [_product_page([1], True, "c1"), _product_page([2], True, "c1")],  # repeated cursor
        [_product_page([1], None)],  # hasNextPage missing
        [ok({"data": {"products": {"nodes": []}}})],  # pageInfo missing
        [ok({"data": {"other": {}}})],  # connection missing
    ],
)
def test_broken_shopify_pagination_is_incomplete(
    pages: list[httpx.Response],
) -> None:
    with pytest.raises(IncompleteResponse):
        list(_shop(Recorder(*pages)).iter_connection(PRODUCTS, ("products",)))


def test_shopify_listing_stops_after_max_pages() -> None:
    counter = iter(range(100))

    def endless(request: httpx.Request) -> httpx.Response:
        n = next(counter)
        return _product_page([n], True, f"c{n}")

    graphql = shopify_client(STORE, FAKE_TOKEN, VERSION, transport=httpx.MockTransport(endless))

    with pytest.raises(IncompleteResponse, match="more than 3 pages"):
        list(graphql.iter_connection(PRODUCTS, ("products",), max_pages=3))


def test_clients_close_cleanly() -> None:
    with _shop(Recorder(ok({"data": {}}))) as graphql:
        graphql.query(SHOP_QUERY)
    with _erp(Recorder(ok({"data": []}))) as client:
        client.request_json("GET", "/api/resource/Item")
