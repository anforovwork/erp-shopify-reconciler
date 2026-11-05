"""Read-only HTTP layer shared by the Shopify and ERPNext clients.

Safety is enforced in an httpx *transport*, so no client code path can bypass it:

* ERPNext: only `GET` on `/api/resource/...` (plus an explicit allowlist of read-only
  `/api/method/...` calls), never with `run_method` or `cmd` parameters — Frappe executes a
  document method for `GET /api/resource/<doctype>/<name>?run_method=...`.
* Shopify: only `POST` of a strict JSON body to the configured `/admin/api/<version>/graphql.json`,
  whose GraphQL document (parsed by graphql-core) is exactly one query plus fragments — plus
  exactly one other request: the client-credentials token exchange on `/admin/oauth/access_token`.

Requests go only to the configured host, redirects are never followed, and error messages
contain the method, path and status code — never headers, tokens or response bodies.
"""

import json
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl

import httpx
from graphql import GraphQLError, OperationType, parse
from graphql.language import FragmentDefinitionNode, OperationDefinitionNode
from pydantic import SecretStr

from reconciler.reconcile import ConfigurationError

# --- Errors --------------------------------------------------------------------------------


class ReadOnlyViolation(Exception):
    """A request that could change data was about to be sent. This is a programming error."""


class SourceError(Exception):
    """The source system answered with an error, or could not be reached after retries."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class IncompleteResponse(SourceError):
    """A response was malformed or incomplete; partial data is never returned."""


class ShopifyQueryError(SourceError):
    """Shopify answered a query with GraphQL errors. `codes` are the sanitised error codes."""

    def __init__(self, message: str, codes: tuple[str, ...]) -> None:
        super().__init__(message)
        self.codes = codes


# --- Read-only guards ----------------------------------------------------------------------

ERPNEXT_SAFE_METHODS = frozenset({"frappe.auth.get_logged_user"})
_FORBIDDEN_ERPNEXT_PARAMS = frozenset({"run_method", "cmd"})

Policy = Callable[[httpx.Request], None]


_METHOD_OVERRIDE_HEADERS = ("x-http-method-override", "x-http-method", "x-method-override")
_UNSAFE_PATH_CHARS = frozenset(";\\%") | {chr(c) for c in range(32)} | {"\x7f"}


def erpnext_policy(host: str, allowed_methods: frozenset[str] = ERPNEXT_SAFE_METHODS) -> Policy:
    """Checks run on the *decoded* URL (httpx decodes %XX), so encoded tricks are caught too."""

    def check(request: httpx.Request) -> None:
        _check_host(request, host)
        if request.method != "GET":
            raise ReadOnlyViolation(f"ERPNext: only GET is allowed, not {request.method}")
        if request.content or "content-length" in request.headers:
            # Frappe builds form_dict from a JSON body even on GET, replacing the URL params.
            raise ReadOnlyViolation("ERPNext: GET requests must not have a body")
        if any(name in request.headers for name in _METHOD_OVERRIDE_HEADERS):
            raise ReadOnlyViolation("ERPNext: method-override headers are not allowed")
        _check_erpnext_path(request.url.path, allowed_methods)
        if ";" in request.url.query.decode("ascii", "replace"):
            raise ReadOnlyViolation("ERPNext: ';' in the query string is not allowed")
        if _FORBIDDEN_ERPNEXT_PARAMS & {_param_key(key) for key in request.url.params}:
            raise ReadOnlyViolation("ERPNext: run_method/cmd parameters execute code")

    return check


def _check_erpnext_path(path: str, allowed_methods: frozenset[str]) -> None:
    segments = path.split("/")[1:]
    if _UNSAFE_PATH_CHARS & set(path) or any(s in ("", ".", "..") for s in segments[:-1]):
        raise ReadOnlyViolation("ERPNext: unsafe or ambiguous path")
    if path.startswith("/api/method/"):
        if path.removeprefix("/api/method/") not in allowed_methods:
            raise ReadOnlyViolation("ERPNext: this /api/method call is not on the allowlist")
    elif not path.startswith("/api/resource/") or segments[-1] in (".", ".."):
        raise ReadOnlyViolation("ERPNext: only /api/resource/ paths are allowed")


def _param_key(key: str) -> str:
    return key.strip().lower().removesuffix("[]")


SHOPIFY_TOKEN_PATH = "/admin/oauth/access_token"  # noqa: S105 - a URL path, not a secret
_TOKEN_FORM_KEYS = ("client_id", "client_secret", "grant_type")


def shopify_policy(host: str, api_version: str, client_id: str) -> Policy:
    """GraphQL queries, plus the one token exchange for this app (see _check_token_request)."""
    graphql_path = f"/admin/api/{api_version}/graphql.json"

    def check(request: httpx.Request) -> None:
        _check_host(request, host)
        if request.url.scheme != "https":
            raise ReadOnlyViolation("Shopify: only https:// is allowed")
        if request.url.path == SHOPIFY_TOKEN_PATH:
            _check_token_request(request, client_id)
            return
        if request.method != "POST" or request.url.path != graphql_path:
            raise ReadOnlyViolation("Shopify: only POST to the GraphQL Admin endpoint is allowed")
        if request.url.query:
            raise ReadOnlyViolation("Shopify: URL parameters are not allowed")
        content_type = request.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise ReadOnlyViolation("Shopify: request body must be application/json")
        body = _strict_json_object(request.content)
        if not set(body) <= _GRAPHQL_BODY_KEYS:
            raise ReadOnlyViolation("Shopify: unexpected keys in the GraphQL request body")
        variables = body.get("variables")
        operation_name = body.get("operationName")
        if not isinstance(body.get("query"), str):
            raise ReadOnlyViolation("Shopify: request body has no GraphQL query")
        if variables is not None and not isinstance(variables, dict):
            raise ReadOnlyViolation("Shopify: GraphQL variables must be an object")
        if operation_name is not None and not isinstance(operation_name, str):
            raise ReadOnlyViolation("Shopify: operationName must be a string")
        ensure_query_only(body["query"], operation_name)

    return check


def _check_token_request(request: httpx.Request, client_id: str) -> None:
    """Allow only `grant_type=client_credentials` for the configured app: it issues a token and
    changes no shop data. Other grants, extra fields (e.g. `scope`) and other OAuth paths are
    rejected. The raw path is compared, so an encoded variant of the path is rejected too."""
    if request.method != "POST" or request.url.raw_path != SHOPIFY_TOKEN_PATH.encode():
        raise ReadOnlyViolation("Shopify: token request must be a POST to the exact token path")
    if any(name in request.headers for name in _METHOD_OVERRIDE_HEADERS):
        raise ReadOnlyViolation("Shopify: method-override headers are not allowed")
    content_type = request.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise ReadOnlyViolation("Shopify: token request must be form-encoded")
    try:
        pairs = parse_qsl(
            request.content.decode("ascii"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=len(_TOKEN_FORM_KEYS),
        )
    except ValueError:  # includes UnicodeDecodeError and too many fields
        raise ReadOnlyViolation("Shopify: token request body is malformed") from None
    if sorted(key for key, _ in pairs) != sorted(_TOKEN_FORM_KEYS):
        raise ReadOnlyViolation("Shopify: token request must have exactly the expected fields")
    form = dict(pairs)
    if form["grant_type"] != "client_credentials":
        raise ReadOnlyViolation("Shopify: only the client_credentials grant is allowed")
    if form["client_id"] != client_id or not form["client_secret"]:
        raise ReadOnlyViolation("Shopify: token request is not for the configured app")


# Persisted-query hashes ("extensions") or anything else could make the server run a document
# we never inspected, so only these keys are accepted.
_GRAPHQL_BODY_KEYS = frozenset({"query", "variables", "operationName"})


def _strict_json_object(content: bytes) -> dict[str, Any]:
    """Parse a JSON object, rejecting duplicate keys: a server that keeps the *first* `query`
    while we inspect the *last* one would otherwise run a document we never checked."""

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ReadOnlyViolation("Shopify: duplicate keys in the JSON body")
        return dict(pairs)

    try:
        body = json.loads(content, object_pairs_hook=no_duplicates)
    except ValueError:
        raise ReadOnlyViolation("Shopify: request body is not JSON") from None
    if not isinstance(body, dict):
        raise ReadOnlyViolation("Shopify: request body is not a JSON object")
    return body


def _check_host(request: httpx.Request, host: str) -> None:
    if request.url.host != host:
        raise ReadOnlyViolation("Request to an unexpected host")


MAX_GRAPHQL_CHARS = 20_000
MAX_GRAPHQL_TOKENS = 2_000


def ensure_query_only(document: str, operation_name: str | None = None) -> None:
    """Allow exactly one `query` operation plus fragments, parsed by graphql-core.

    Rejected: invalid syntax, mutations, subscriptions, more than one operation (even if all are
    queries), schema/type-system definitions, oversized documents, and an operationName that
    does not name the operation. The parser's error text (which quotes the document) is dropped.
    """
    if not isinstance(document, str) or len(document) > MAX_GRAPHQL_CHARS:
        raise ReadOnlyViolation("GraphQL document is missing or too large")
    try:
        ast = parse(document, no_location=True, max_tokens=MAX_GRAPHQL_TOKENS)
    except GraphQLError:
        raise ReadOnlyViolation("GraphQL document is not valid GraphQL") from None
    operations = [d for d in ast.definitions if isinstance(d, OperationDefinitionNode)]
    if any(
        not isinstance(d, OperationDefinitionNode | FragmentDefinitionNode) for d in ast.definitions
    ):
        raise ReadOnlyViolation("GraphQL document may only contain a query and fragments")
    if len(operations) != 1:
        raise ReadOnlyViolation("GraphQL document must contain exactly one operation")
    [operation] = operations
    if operation.operation is not OperationType.QUERY:
        raise ReadOnlyViolation("GraphQL operation must be a query (no mutations/subscriptions)")
    if operation_name is not None and (
        operation.name is None or operation.name.value != operation_name
    ):
        raise ReadOnlyViolation("operationName does not name the query in the document")


class ReadOnlyTransport(httpx.BaseTransport):
    """Checks every request against a policy before the wrapped transport sees it."""

    def __init__(self, inner: httpx.BaseTransport, policy: Policy) -> None:
        self._inner = inner
        self._policy = policy

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._policy(request)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


# --- Retries -------------------------------------------------------------------------------

RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


@dataclass(frozen=True)
class RetryPolicy:
    """All requests are reads, so retrying them is always safe."""

    max_attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 30.0
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def backoff(self, attempt: int) -> float:
        return min(self.base_delay * 2 ** (attempt - 1), self.max_delay)


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Retry-After in seconds (what Shopify and Frappe send); anything else -> normal backoff."""
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None
    return seconds if 0 <= seconds < float("inf") else None


# --- JSON ----------------------------------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name} in JSON")


def parse_json(response: httpx.Response) -> Any:
    """Numbers with a fraction become Decimal, never float; NaN/Infinity are rejected."""
    try:
        return json.loads(response.content, parse_float=Decimal, parse_constant=_reject_constant)
    except ValueError:
        raise IncompleteResponse(f"Invalid JSON from {_where(response.request)}") from None


def _where(request: httpx.Request) -> str:
    return f"{request.method} {request.url.path}"


# --- Client --------------------------------------------------------------------------------


class ReadOnlyClient:
    """httpx.Client behind a ReadOnlyTransport, with retries and safe error messages."""

    def __init__(
        self,
        base_url: str,
        headers: Mapping[str, str],
        policy: Policy,
        *,
        retry: RetryPolicy | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        auth: httpx.Auth | None = None,
    ) -> None:
        self.retry = retry or RetryPolicy()
        self._client = httpx.Client(
            base_url=base_url,
            headers=dict(headers),
            auth=auth,
            timeout=timeout,
            follow_redirects=False,
            transport=ReadOnlyTransport(transport or httpx.HTTPTransport(), policy),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ReadOnlyClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
    ) -> Any:
        """Send with retries for timeouts, network errors and 429/502/503/504."""
        for attempt in range(1, self.retry.max_attempts + 1):
            last_attempt = attempt == self.retry.max_attempts
            try:
                response = self._client.request(
                    method, path, params=params, json=json_body, data=form_body
                )
            except httpx.TransportError as error:
                if last_attempt:
                    raise SourceError(
                        f"{method} {path} failed after {attempt} attempts: {type(error).__name__}"
                    ) from None
                self.retry.sleep(self.retry.backoff(attempt))
                continue
            if response.status_code in RETRYABLE_STATUS and not last_attempt:
                delay = retry_after_seconds(response)
                wait = self.retry.backoff(attempt) if delay is None else delay
                self.retry.sleep(min(wait, self.retry.max_delay))
                continue
            if not response.is_success:
                raise SourceError(
                    f"{_where(response.request)} returned HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            return parse_json(response)
        raise AssertionError("unreachable")  # pragma: no cover


# --- ERPNext -------------------------------------------------------------------------------


def erpnext_client(
    base_url: str,
    api_key: SecretStr,
    api_secret: SecretStr,
    *,
    retry: RetryPolicy | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ReadOnlyClient:
    token = f"token {api_key.get_secret_value()}:{api_secret.get_secret_value()}"
    return ReadOnlyClient(
        base_url,
        {"Authorization": token, "Accept": "application/json"},
        erpnext_policy(httpx.URL(base_url).host),
        retry=retry,
        transport=transport,
    )


def iter_erpnext_list(
    client: ReadOnlyClient,
    doctype: str,
    *,
    fields: list[str],
    filters: list[list[Any]] | None = None,
    order_by: str = "name asc",
    page_size: int = 500,
    max_pages: int = 10_000,
) -> Iterator[dict[str, Any]]:
    """All rows of `GET /api/resource/<doctype>`, page by page (limit_start / limit_page_length).

    Pages are ordered by a stable key; the listing ends at the first page shorter than
    `page_size`. Anything unexpected raises IncompleteResponse instead of yielding a partial list.
    """
    path = f"/api/resource/{doctype}"
    for page in range(max_pages):
        params = {
            "fields": json.dumps(fields),
            "filters": json.dumps(filters or []),
            "order_by": order_by,
            "limit_start": str(page * page_size),
            "limit_page_length": str(page_size),
        }
        body = client.request_json("GET", path, params=params)
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise IncompleteResponse(f"GET {path}: response has no list of rows")
        if len(rows) > page_size:
            raise IncompleteResponse(f"GET {path}: page larger than requested; paging unreliable")
        yield from rows
        if len(rows) < page_size:
            return
    raise IncompleteResponse(f"GET {path}: more than {max_pages} pages; stopping")


# --- Shopify -------------------------------------------------------------------------------


ALLOWED_SHOPIFY_SCOPES = frozenset({"read_products", "read_inventory", "read_orders"})
_SCOPE_HANDLE = re.compile(r"^[a-z_]{1,64}$")


def check_scopes(granted: Iterable[str]) -> None:
    """The app must hold exactly the three read scopes: anything else (above all write_*) could
    change data, and a missing one would make the data incomplete."""
    scopes = set(granted)
    extra = sorted(_printable_scope(s) for s in scopes - ALLOWED_SHOPIFY_SCOPES)
    missing = sorted(ALLOWED_SHOPIFY_SCOPES - scopes)
    if extra:
        raise ConfigurationError(
            f"The Shopify app has scopes outside {sorted(ALLOWED_SHOPIFY_SCOPES)}: {extra}. "
            "Reinstall it with the read scopes only; this tool never runs with more."
        )
    if missing:
        raise ConfigurationError(f"The Shopify app is missing required scopes: {missing}.")


def _printable_scope(scope: str) -> str:
    """Scope handles come from Shopify; anything not shaped like a handle is not echoed."""
    return scope if _SCOPE_HANDLE.fullmatch(scope) else "<unrecognised scope>"


TOKEN_REFRESH_MARGIN = 300.0
"""Seconds before expiry at which a new token is requested."""
_CREDENTIALS_REJECTED = (400, 401, 403)


class ShopifyAccessToken:
    """Admin API token from the client-credentials grant. Held in memory only, as SecretStr;
    renewed shortly before it expires. Never logged, printed or stored."""

    def __init__(
        self,
        client: ReadOnlyClient,
        client_id: SecretStr,
        client_secret: SecretStr,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._client_id = client_id
        self._client_secret = client_secret
        self._clock = clock
        self._token: SecretStr | None = None
        self._refresh_at = 0.0

    def get(self) -> SecretStr:
        if self._token is None or self._clock() >= self._refresh_at:
            self._token = self._request()
        return self._token

    def _request(self) -> SecretStr:
        started = self._clock()
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id.get_secret_value(),
            "client_secret": self._client_secret.get_secret_value(),
        }
        try:
            body = self._client.request_json("POST", SHOPIFY_TOKEN_PATH, form_body=form)
        except SourceError as error:
            if error.status_code in _CREDENTIALS_REJECTED:
                raise ConfigurationError(
                    f"Shopify rejected the app credentials (HTTP {error.status_code}); check "
                    "SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET and that the app is installed."
                ) from None
            raise
        token, lifetime = _token_fields(body)
        self._refresh_at = started + lifetime - min(TOKEN_REFRESH_MARGIN, lifetime / 2)
        return token


def _token_fields(body: object) -> tuple[SecretStr, int]:
    where = f"POST {SHOPIFY_TOKEN_PATH}"
    if not isinstance(body, dict):
        raise IncompleteResponse(f"{where}: response is not an object")
    token = body.get("access_token")
    lifetime = body.get("expires_in")
    if not isinstance(token, str) or not token:
        raise IncompleteResponse(f"{where}: no access_token")
    if type(lifetime) is not int or lifetime <= 0:
        raise IncompleteResponse(f"{where}: expires_in is not a positive integer")
    scope = body.get("scope")
    if scope is not None:
        if not isinstance(scope, str):
            raise IncompleteResponse(f"{where}: scope is not a string")
        check_scopes(part.strip() for part in scope.split(",") if part.strip())
    return SecretStr(token), lifetime


class _ShopifyTokenAuth(httpx.Auth):
    """Adds the current access token to each GraphQL request (also on every retry)."""

    def __init__(self, token: ShopifyAccessToken) -> None:
        self._token = token

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        request.headers["X-Shopify-Access-Token"] = self._token.get().get_secret_value()
        yield request


def shopify_client(
    store_domain: str,
    client_id: SecretStr,
    client_secret: SecretStr,
    api_version: str,
    *,
    retry: RetryPolicy | None = None,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> "ShopifyGraphQL":
    """Two clients behind the same guard: one only fetches tokens (no token header), the other
    sends GraphQL queries with the current token."""
    policy = shopify_policy(store_domain, api_version, client_id.get_secret_value())
    inner = transport or httpx.HTTPTransport()
    base_url = f"https://{store_domain}"
    headers = {"Accept": "application/json"}
    token_client = ReadOnlyClient(base_url, headers, policy, retry=retry, transport=inner)
    token = ShopifyAccessToken(token_client, client_id, client_secret, clock)
    client = ReadOnlyClient(
        base_url, headers, policy, retry=retry, transport=inner, auth=_ShopifyTokenAuth(token)
    )
    return ShopifyGraphQL(client, f"/admin/api/{api_version}/graphql.json", (token_client,))


class ShopifyGraphQL:
    """GraphQL queries with THROTTLED handling. Any GraphQL error fails the whole call."""

    def __init__(
        self, client: ReadOnlyClient, path: str, also_close: tuple[ReadOnlyClient, ...] = ()
    ) -> None:
        self._client = client
        self._path = path
        self._also_close = also_close

    def close(self) -> None:
        self._client.close()
        for client in self._also_close:
            client.close()

    def __enter__(self) -> "ShopifyGraphQL":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def query(self, document: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        ensure_query_only(document)  # fail fast, before any network I/O
        retry = self._client.retry
        body: Mapping[str, Any] = {"query": document, "variables": dict(variables or {})}
        for attempt in range(1, retry.max_attempts + 1):
            result = self._client.request_json("POST", self._path, json_body=body)
            if not isinstance(result, dict):
                raise IncompleteResponse(f"POST {self._path}: response is not an object")
            errors = result.get("errors")
            if errors and _is_throttled(errors) and attempt < retry.max_attempts:
                retry.sleep(min(_throttle_wait(result, retry, attempt), retry.max_delay))
                continue
            if errors:
                codes = sorted({_error_code(e) for e in errors if isinstance(e, dict)}) or [
                    "UNKNOWN"
                ]
                raise ShopifyQueryError(f"POST {self._path}: GraphQL errors {codes}", tuple(codes))
            data = result.get("data")
            if not isinstance(data, dict):
                raise IncompleteResponse(f"POST {self._path}: response has no data")
            return data
        raise SourceError(f"POST {self._path}: still THROTTLED after {retry.max_attempts} attempts")

    def iter_connection(
        self,
        document: str,
        path: tuple[str, ...],
        variables: Mapping[str, Any] | None = None,
        max_pages: int = 10_000,
    ) -> Iterator[dict[str, Any]]:
        """All nodes of a cursor-paginated connection.

        The query must take an `$after: String` variable and select
        `nodes { ... } pageInfo { hasNextPage endCursor }` at `path`.
        """
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(max_pages):
            data = self.query(document, {**(variables or {}), "after": cursor})
            connection = _dig(data, path)
            nodes = connection.get("nodes")
            page_info = connection.get("pageInfo")
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                raise IncompleteResponse(f"{'.'.join(path)}: missing nodes or pageInfo")
            yield from nodes
            if page_info.get("hasNextPage") is not True:
                if page_info.get("hasNextPage") is not False:
                    raise IncompleteResponse(f"{'.'.join(path)}: hasNextPage is missing")
                return
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise IncompleteResponse(f"{'.'.join(path)}: missing or repeated endCursor")
            seen.add(cursor)
        raise IncompleteResponse(f"{'.'.join(path)}: more than {max_pages} pages; stopping")


def _dig(data: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    node: Any = data
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, dict):
        raise IncompleteResponse(f"{'.'.join(path)}: not found in response")
    return node


def _error_code(error: dict[str, Any]) -> str:
    extensions = error.get("extensions")
    code = extensions.get("code") if isinstance(extensions, dict) else None
    return code if isinstance(code, str) and code.isupper() and len(code) <= 40 else "UNKNOWN"


def _is_throttled(errors: object) -> bool:
    return isinstance(errors, list) and any(
        isinstance(e, dict) and _error_code(e) == "THROTTLED" for e in errors
    )


def _throttle_wait(result: dict[str, Any], retry: RetryPolicy, attempt: int) -> float:
    """Seconds until enough query cost is restored, from Shopify's cost extension."""
    try:
        cost = result["extensions"]["cost"]
        status = cost["throttleStatus"]
        missing = Decimal(str(cost["requestedQueryCost"])) - Decimal(
            str(status["currentlyAvailable"])
        )
        rate = Decimal(str(status["restoreRate"]))
        if rate > 0:
            return float(max(missing, Decimal(0)) / rate) + 0.1
    except (KeyError, TypeError, ArithmeticError, ValueError):
        pass
    return retry.backoff(attempt)
