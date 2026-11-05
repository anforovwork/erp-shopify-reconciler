"""Read-only HTTP layer. Every test uses httpx.MockTransport; nothing touches the network."""

import json
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from reconciler.reconcile import ConfigurationError
from reconciler.sources.http import (
    ALLOWED_SHOPIFY_SCOPES,
    IncompleteResponse,
    ReadOnlyTransport,
    ReadOnlyViolation,
    RetryPolicy,
    ShopifyGraphQL,
    SourceError,
    check_scopes,
    ensure_query_only,
    erpnext_client,
    iter_erpnext_list,
    shopify_client,
    shopify_policy,
)

FAKE_CLIENT_ID = SecretStr("FAKE-CLIENT-ID")
FAKE_CLIENT_SECRET = SecretStr("shpss_FAKE-CLIENT-SECRET")
FAKE_ACCESS_TOKEN = "FAKE-ACCESS-TOKEN-1"  # noqa: S105 - fake test value
TOKEN_PATH = "/admin/oauth/access_token"  # noqa: S105 - a URL path, not a secret
READ_SCOPES = "read_products,read_inventory,read_orders"
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


Handler = Callable[[httpx.Request], httpx.Response]


def token_response(
    token: str = FAKE_ACCESS_TOKEN, expires_in: object = 86399, scope: object = READ_SCOPES
) -> httpx.Response:
    return ok({"access_token": token, "scope": scope, "expires_in": expires_in})


class WithToken:
    """Answers token requests from `tokens`; everything else goes to `inner`, so `inner` sees
    only GraphQL traffic (existing assertions about it stay exact)."""

    def __init__(self, inner: Handler, tokens: Recorder | None = None) -> None:
        self.inner = inner
        self.tokens = tokens or Recorder(token_response())

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return self.tokens(request)
        return self.inner(request)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _shop(
    recorder: Handler,
    sleeps: Sleeps | None = None,
    *,
    tokens: Recorder | None = None,
    clock: FakeClock | None = None,
) -> ShopifyGraphQL:
    retry = RetryPolicy(
        max_attempts=3, base_delay=1.0, sleep=Sleeps() if sleeps is None else sleeps
    )
    return shopify_client(
        STORE,
        FAKE_CLIENT_ID,
        FAKE_CLIENT_SECRET,
        VERSION,
        retry=retry,
        transport=httpx.MockTransport(WithToken(recorder, tokens)),
        clock=clock or FakeClock(),
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


def _raw_erp_get(recorder: Recorder, url: str, **kwargs: object) -> None:
    """Send through the real ReadOnlyTransport, bypassing request_json's conveniences."""
    _erp(recorder)._client.request("GET", url, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    [
        # Encoded parameter names are decoded before checking.
        "/api/resource/Item/X?run%5Fmethod=cancel",
        "/api/resource/Item/X?%72un_method=cancel",
        "/api/resource/Item/X?Run_Method=cancel",
        "/api/resource/Item/X?run_method%20=cancel",
        "/api/resource/Item/X?run_method[]=cancel",
        "/api/resource/Item?cmd=frappe.client.delete",
        "/api/resource/Item?a=1;run_method=cancel",
        # Alternate and traversing paths to /api/method or elsewhere.
        "/api/resource/%2e%2e/method/frappe.client.delete",
        "/api/resource/Item%2F..%2F..%2Fmethod%2Ffrappe.client.delete",
        "/api/resource/Item/%2e%2e",
        "/api//resource/Item",
        "//api/method/frappe.client.delete",
        "/api/method/frappe.auth.get_logged_user/../../method/frappe.client.delete",
        "/api/method/frappe.auth.get_logged_user%2F..%2Ffrappe.client.delete",
        "/api/method/frappe.client.delete",
        "/api/method/frappe.auth.get_logged_user;x",
        "/API/resource/Item",
        "/api/resource%5CItem",
        "/api/resource/Item%00",
        "/api/resource/Item%253Brun_method",  # double encoding leaves a '%'
        "/api/v2/document/Item",
        "/api/v2/method/frappe.client.delete",
        "/app/item",
        "/",
    ],
)
def test_erpnext_bypass_attempts_never_reach_the_transport(url: str) -> None:
    recorder = Recorder(ok({}))

    with pytest.raises(ReadOnlyViolation):
        _raw_erp_get(recorder, url)

    assert recorder.requests == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"json": {"run_method": "cancel"}},  # Frappe uses a JSON body as form_dict, even on GET
        {"content": b"run_method=cancel"},
        {"data": {"run_method": "cancel"}},
        {"headers": {"X-HTTP-Method-Override": "DELETE"}},
        {"headers": {"X-HTTP-Method": "PUT"}},
        {"headers": {"X-Method-Override": "POST"}},
    ],
)
def test_erpnext_get_with_body_or_method_override_is_blocked(kwargs: dict) -> None:
    recorder = Recorder(ok({}))

    with pytest.raises(ReadOnlyViolation):
        _raw_erp_get(recorder, "/api/resource/Item", **kwargs)

    assert recorder.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "/api/resource/Item",
        "/api/resource/Item/",
        "/api/resource/Item%20Price",
        "/api/resource/Item/TEST-SKU-1",
        "/api/resource/./Item",  # httpx normalizes this to /api/resource/Item before sending
        '/api/resource/Item?fields=["name"]&filters=[["item_group","=","A B"]]',
        "/api/method/frappe.auth.get_logged_user",
    ],
)
def test_ordinary_erpnext_reads_pass(url: str) -> None:
    recorder = Recorder(ok({"data": []}))

    _raw_erp_get(recorder, url)

    assert len(recorder.requests) == 1


def test_erpnext_requests_to_another_host_are_blocked() -> None:
    recorder = Recorder(ok({}))

    with pytest.raises(ReadOnlyViolation, match="unexpected host"):
        _erp(recorder).request_json("GET", "http://evil.example.test/api/resource/Item")

    assert recorder.requests == []


# --- Shopify guard: GraphQL documents (parsed by graphql-core) ----------------------------

ALLOWED_DOCUMENTS = {
    "named query": "query { shop { name } }",
    "anonymous shorthand": "{ shop { name } }",
    "variables": (
        "query Products($after: String) { products(first: 10, after: $after) { nodes { id } } }"
    ),
    "comment mentioning mutation": "# mutation { productDelete }\nquery { shop { name } }",
    "string containing mutation": 'query { products(query: "mutation { x }") { nodes { id } } }',
    "block string containing mutation": 'query { products(query: """\nmutation { x }\n""") '
    "{ nodes { id } } }",
    "alias named mutation": "query { mutation: shop { name } subscription: shop { id } }",
    "field named mutation": "query { mutation { id } }",
    "directives": "query($s: Boolean!) { shop { name @include(if: $s) id @skip(if: true) } }",
    "named fragment": "query { shop { ...F } } fragment F on Shop { name }",
    "fragment before query": "fragment F on Shop { name } query Q { shop { ...F } }",
    "inline fragment": 'query { node(id: "1") { ... on Product { title } } }',
    "unicode escape in string": 'query { products(query: "\\u006dutation") { nodes { id } } }',
}

REJECTED_DOCUMENTS = {
    "mutation": 'mutation { productDelete(input: {id: "1"}) { deletedProductId } }',
    "named mutation": 'mutation Wipe { productDelete(input: {id: "1"}) { userErrors { field } } }',
    "subscription": "subscription { productUpdated { id } }",
    "query then mutation": "query A { shop { name } } mutation B { x }",
    "mutation then query": "mutation B { x } query A { shop { name } }",
    "two queries": "query A { shop { name } } query B { shop { id } }",
    "anonymous plus named": "{ shop { name } } query B { shop { id } }",
    "only a fragment": "fragment F on Shop { name }",
    "only a comment": "# query { shop { name } }",
    "empty": "",
    "whitespace": "   \n\t ",
    "unbalanced braces": "query { shop { name }",
    "extra closing brace": "query { shop } }",
    "uppercase keyword": "MUTATION { x }",
    "mutation hidden in comment line break": "query { shop { name } }\n#\nmutation { x }",
    "unterminated string": 'query { products(query: "mutation) { nodes { id } } }',
    "schema definition": "type Query { shop: Shop }",
    "schema extension": "extend type Shop { evil: String } query { shop { name } }",
    "directive definition": "directive @x on FIELD query { shop { name } }",
    "fragment variables (experimental syntax)": (
        "query { shop { ...F(a: 1) } } fragment F($a: Int) on Shop { name }"
    ),
    "not graphql": "SELECT * FROM products",
    "json": '{"query": "mutation { x }"}',
}


@pytest.mark.parametrize("document", ALLOWED_DOCUMENTS.values(), ids=ALLOWED_DOCUMENTS.keys())
def test_read_only_documents_are_allowed(document: str) -> None:
    ensure_query_only(document)


@pytest.mark.parametrize("document", REJECTED_DOCUMENTS.values(), ids=REJECTED_DOCUMENTS.keys())
def test_writes_malformed_and_unsupported_documents_are_rejected(document: str) -> None:
    with pytest.raises(ReadOnlyViolation):
        ensure_query_only(document)


@pytest.mark.parametrize("document", REJECTED_DOCUMENTS.values(), ids=REJECTED_DOCUMENTS.keys())
def test_rejected_documents_never_reach_the_transport(document: str) -> None:
    recorder = Recorder(ok({"data": {}}))
    graphql = _shop(recorder)

    with pytest.raises(ReadOnlyViolation):
        graphql.query(document)
    with pytest.raises(ReadOnlyViolation):  # bypassing ShopifyGraphQL.query is caught too
        graphql._client.request_json("POST", GRAPHQL, json_body={"query": document})

    assert recorder.requests == []


def test_oversized_documents_are_rejected() -> None:
    huge_but_valid = "query { shop { " + "name " * 5_000 + "} }"

    with pytest.raises(ReadOnlyViolation, match="too large"):
        ensure_query_only(huge_but_valid)


def test_token_limit_applies_below_the_size_limit() -> None:
    many_tokens = "query { shop { " + "a " * 3_000 + "} }"  # < 20k chars, > 2k tokens

    with pytest.raises(ReadOnlyViolation, match="not valid GraphQL"):
        ensure_query_only(many_tokens)


def test_parser_error_text_is_not_echoed() -> None:
    with pytest.raises(ReadOnlyViolation) as error:
        ensure_query_only('query { products(query: "secret-looking" ')

    assert "secret-looking" not in str(error.value)


@pytest.mark.parametrize(
    ("operation_name", "allowed"), [(None, True), ("Q", True), ("Other", False), ("", False)]
)
def test_operation_name_must_name_the_query(operation_name: str | None, allowed: bool) -> None:
    document = "query Q { shop { name } }"
    if allowed:
        ensure_query_only(document, operation_name)
    else:
        with pytest.raises(ReadOnlyViolation):
            ensure_query_only(document, operation_name)


def test_operation_name_on_anonymous_query_is_rejected() -> None:
    with pytest.raises(ReadOnlyViolation):
        ensure_query_only("{ shop { name } }", "Q")


# --- Shopify guard: HTTP request shape -----------------------------------------------------


def _raw_post(
    graphql: ShopifyGraphQL,
    content: bytes,
    content_type: str = "application/json",
    url: str = GRAPHQL,
) -> None:
    graphql._client._client.post(url, content=content, headers={"Content-Type": content_type})


@pytest.mark.parametrize(
    ("content", "content_type", "url"),
    [
        # Duplicate keys: a server keeping the first "query" would run the mutation.
        (b'{"query": "mutation { x }", "query": "query { shop { name } }"}', None, GRAPHQL),
        # Persisted-query hash could run a stored document we never inspected.
        (
            b'{"query": "query { shop { name } }", "extensions": {"persistedQuery": {}}}',
            None,
            GRAPHQL,
        ),
        (b'{"query": "query { shop { name } }", "variables": []}', None, GRAPHQL),
        (b'{"query": "query { shop { name } }", "operationName": 1}', None, GRAPHQL),
        (b'[{"query": "query { shop { name } }"}]', None, GRAPHQL),  # batched requests
        (b"query { shop { name } }", "application/graphql", GRAPHQL),
        (b'{"query": "query { shop { name } }"}', "text/plain", GRAPHQL),
        (b"not json", None, GRAPHQL),
        (b'{"query": "query { shop { name } }"}', None, GRAPHQL + "?query=mutation%7Bx%7D"),
        (b'{"query": "query { shop { name } }"}', None, GRAPHQL.upper()),
        (b'{"query": "query { shop { name } }"}', None, GRAPHQL + "/"),
    ],
)
def test_malformed_or_ambiguous_shopify_requests_are_blocked(
    content: bytes, content_type: str | None, url: str
) -> None:
    recorder = Recorder(ok({"data": {}}))

    with pytest.raises(ReadOnlyViolation):
        _raw_post(_shop(recorder), content, content_type or "application/json", url)

    assert recorder.requests == []


def test_well_formed_raw_query_passes_the_transport() -> None:
    recorder = Recorder(ok({"data": {}}))

    _raw_post(_shop(recorder), b'{"query": "query Q { shop { name } }", "operationName": "Q"}')

    assert len(recorder.requests) == 1


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
    assert sent.headers["X-Shopify-Access-Token"] == FAKE_ACCESS_TOKEN
    assert (sent.method, sent.url.path) == ("POST", GRAPHQL)


# --- Shopify guard: the token exchange is the only other allowed request ------------------

FORM = "application/x-www-form-urlencoded"
GOOD_FORM = (
    b"grant_type=client_credentials&client_id=FAKE-CLIENT-ID&client_secret=shpss_FAKE-CLIENT-SECRET"
)
BASE = f"https://{STORE}"


def _guarded(recorder: Recorder) -> httpx.Client:
    """A bare client behind the Shopify guard: tests the policy without any client helpers."""
    policy = shopify_policy(STORE, VERSION, FAKE_CLIENT_ID.get_secret_value())
    return httpx.Client(transport=ReadOnlyTransport(httpx.MockTransport(recorder), policy))


def test_exact_token_request_passes_the_guard() -> None:
    recorder = Recorder(token_response())

    _guarded(recorder).post(BASE + TOKEN_PATH, content=GOOD_FORM, headers={"Content-Type": FORM})

    assert len(recorder.requests) == 1


def _form(**fields: str) -> bytes:
    base = {
        "grant_type": "client_credentials",
        "client_id": "FAKE-CLIENT-ID",
        "client_secret": "shpss_FAKE-CLIENT-SECRET",
    }
    return "&".join(f"{k}={v}" for k, v in {**base, **fields}.items()).encode()


@pytest.mark.parametrize(
    ("method", "url", "content", "content_type", "extra_headers"),
    [
        # Other grants on the token path
        ("POST", BASE + TOKEN_PATH, _form(grant_type="authorization_code"), FORM, {}),
        ("POST", BASE + TOKEN_PATH, _form(grant_type="refresh_token"), FORM, {}),
        # Wrong field set
        ("POST", BASE + TOKEN_PATH, _form(scope="write_products"), FORM, {}),
        ("POST", BASE + TOKEN_PATH, GOOD_FORM + b"&grant_type=client_credentials", FORM, {}),
        (
            "POST",
            BASE + TOKEN_PATH,
            b"grant_type=client_credentials&client_id=FAKE-CLIENT-ID",
            FORM,
            {},
        ),
        ("POST", BASE + TOKEN_PATH, _form(client_secret=""), FORM, {}),
        ("POST", BASE + TOKEN_PATH, _form(client_id="OTHER-APP"), FORM, {}),
        ("POST", BASE + TOKEN_PATH, b"grant_type", FORM, {}),
        ("POST", BASE + TOKEN_PATH, b"", FORM, {}),
        ("POST", BASE + TOKEN_PATH, GOOD_FORM + "&é=1".encode(), FORM, {}),
        # Wrong encoding
        (
            "POST",
            BASE + TOKEN_PATH,
            b'{"grant_type": "client_credentials"}',
            "application/json",
            {},
        ),
        ("POST", BASE + TOKEN_PATH, GOOD_FORM, "text/plain", {}),
        # Path and URL variants
        ("POST", BASE + TOKEN_PATH + "?grant_type=client_credentials", GOOD_FORM, FORM, {}),
        ("POST", BASE + TOKEN_PATH + "/", GOOD_FORM, FORM, {}),
        ("POST", BASE + TOKEN_PATH.upper(), GOOD_FORM, FORM, {}),
        ("POST", BASE + "/admin/oauth/access%5Ftoken", GOOD_FORM, FORM, {}),
        ("POST", f"http://{STORE}{TOKEN_PATH}", GOOD_FORM, FORM, {}),
        ("POST", f"https://other-store.myshopify.com{TOKEN_PATH}", GOOD_FORM, FORM, {}),
        # Other methods on the token path
        ("GET", BASE + TOKEN_PATH, b"", FORM, {}),
        ("PUT", BASE + TOKEN_PATH, GOOD_FORM, FORM, {}),
        ("POST", BASE + TOKEN_PATH, GOOD_FORM, FORM, {"X-HTTP-Method-Override": "DELETE"}),
        # Every other non-GraphQL POST
        ("POST", BASE + "/admin/oauth/authorize", GOOD_FORM, FORM, {}),
        ("POST", BASE + "/admin/oauth/access_scopes.json", GOOD_FORM, FORM, {}),
        ("POST", BASE + "/admin/oauth/revoke", GOOD_FORM, FORM, {}),
        (
            "POST",
            BASE + f"/admin/api/{VERSION}/products.json",
            b'{"product": {}}',
            "application/json",
            {},
        ),
        (
            "POST",
            BASE + f"/admin/api/{VERSION}/webhooks.json",
            b'{"webhook": {}}',
            "application/json",
            {},
        ),
        ("POST", BASE + "/admin/products.json", b'{"product": {}}', "application/json", {}),
        (
            "POST",
            BASE + "/api/2026-07/graphql.json",
            b'{"query": "query { shop { name } }"}',
            "application/json",
            {},
        ),
        ("POST", BASE + GRAPHQL, GOOD_FORM, FORM, {}),  # form body on the GraphQL path
        (
            "POST",
            f"http://{STORE}{GRAPHQL}",
            b'{"query": "query { shop { name } }"}',
            "application/json",
            {},
        ),
    ],
)
def test_every_other_shopify_request_is_blocked(
    method: str, url: str, content: bytes, content_type: str, extra_headers: dict[str, str]
) -> None:
    recorder = Recorder(token_response())

    with pytest.raises(ReadOnlyViolation) as error:
        _guarded(recorder).request(
            method, url, content=content, headers={"Content-Type": content_type, **extra_headers}
        )

    assert recorder.requests == []
    assert "FAKE-CLIENT-SECRET" not in str(error.value)


# --- Shopify access token ------------------------------------------------------------------


def test_token_is_fetched_once_and_sent_with_each_query() -> None:
    tokens = Recorder(token_response())
    recorder = Recorder(ok({"data": {}}))
    graphql = _shop(recorder, tokens=tokens)

    graphql.query(SHOP_QUERY)
    graphql.query(SHOP_QUERY)

    [token_request] = tokens.requests
    assert token_request.content == GOOD_FORM
    assert "X-Shopify-Access-Token" not in token_request.headers
    assert [r.headers["X-Shopify-Access-Token"] for r in recorder.requests] == [
        FAKE_ACCESS_TOKEN,
        FAKE_ACCESS_TOKEN,
    ]


def test_token_is_renewed_shortly_before_it_expires() -> None:
    clock = FakeClock()
    tokens = Recorder(token_response("FAKE-T1", 3600), token_response("FAKE-T2", 3600))
    recorder = Recorder(ok({"data": {}}))
    graphql = _shop(recorder, tokens=tokens, clock=clock)

    graphql.query(SHOP_QUERY)
    clock.now = 3600 - 300 - 1  # still more than the 5-minute margin left
    graphql.query(SHOP_QUERY)
    clock.now = 3600 - 300
    graphql.query(SHOP_QUERY)

    assert len(tokens.requests) == 2
    sent = [r.headers["X-Shopify-Access-Token"] for r in recorder.requests]
    assert sent == ["FAKE-T1", "FAKE-T1", "FAKE-T2"]


@pytest.mark.parametrize("status", [400, 401, 403])
def test_rejected_credentials_are_a_configuration_error(status: int) -> None:
    recorder = Recorder(ok({"data": {}}))
    tokens = Recorder(ok({"error": "invalid_client", "secret": "shpss_FAKE-CLIENT-SECRET"}, status))

    with pytest.raises(ConfigurationError) as error:
        _shop(recorder, tokens=tokens).query(SHOP_QUERY)

    assert f"HTTP {status}" in str(error.value)
    assert "FAKE-CLIENT-SECRET" not in str(error.value)
    assert recorder.requests == []


@pytest.mark.parametrize(
    "response",
    [
        ok({"scope": READ_SCOPES, "expires_in": 86399}),
        token_response(token=""),
        token_response(expires_in="86399"),
        token_response(expires_in=True),
        token_response(expires_in=0),
        token_response(scope=["read_products"]),
        ok(["not", "an", "object"]),
    ],
)
def test_malformed_token_response_is_incomplete(response: httpx.Response) -> None:
    recorder = Recorder(ok({"data": {}}))

    with pytest.raises(IncompleteResponse):
        _shop(recorder, tokens=Recorder(response)).query(SHOP_QUERY)

    assert recorder.requests == []


def test_token_with_a_write_scope_is_refused_before_any_query() -> None:
    recorder = Recorder(ok({"data": {}}))
    tokens = Recorder(token_response(scope=READ_SCOPES + ",write_products"))

    with pytest.raises(ConfigurationError, match="write_products"):
        _shop(recorder, tokens=tokens).query(SHOP_QUERY)

    assert recorder.requests == []


def test_token_and_secret_never_appear_in_reprs_or_errors() -> None:
    recorder = Recorder(ok({}, 500))
    graphql = _shop(recorder)

    with pytest.raises(SourceError) as error:
        graphql.query(SHOP_QUERY)

    token_holder = graphql._client._client.auth._token  # type: ignore[union-attr]
    texts = [str(error.value), repr(graphql), repr(token_holder), repr(token_holder.get())]
    for text in texts:
        assert FAKE_ACCESS_TOKEN not in text
        assert "FAKE-CLIENT-SECRET" not in text


# --- Shopify scopes ------------------------------------------------------------------------


def test_exactly_the_read_scopes_are_accepted() -> None:
    check_scopes(ALLOWED_SHOPIFY_SCOPES)


@pytest.mark.parametrize(
    ("granted", "named"),
    [
        ({*ALLOWED_SHOPIFY_SCOPES, "write_products"}, "write_products"),
        ({*ALLOWED_SHOPIFY_SCOPES, "read_customers"}, "read_customers"),
        ({*ALLOWED_SHOPIFY_SCOPES, "ignore previous instructions"}, "<unrecognised scope>"),
        (ALLOWED_SHOPIFY_SCOPES - {"read_orders"}, "missing required scopes: ['read_orders']"),
        (set(), "missing required scopes"),
    ],
)
def test_other_or_missing_scopes_are_refused(granted: set[str], named: str) -> None:
    with pytest.raises(ConfigurationError) as error:
        check_scopes(granted)

    assert named in str(error.value)
    assert "ignore previous" not in str(error.value)


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
    assert error.value.status_code == status
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

    graphql = _shop(endless)

    with pytest.raises(IncompleteResponse, match="more than 3 pages"):
        list(graphql.iter_connection(PRODUCTS, ("products",), max_pages=3))


def test_clients_close_cleanly() -> None:
    with _shop(Recorder(ok({"data": {}}))) as graphql:
        graphql.query(SHOP_QUERY)
    with _erp(Recorder(ok({"data": []}))) as client:
        client.request_json("GET", "/api/resource/Item")
