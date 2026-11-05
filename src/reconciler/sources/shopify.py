"""Read-only Shopify Admin GraphQL client: everything reconcile() needs, as one ShopifyData.

Every request goes through the read-only HTTP layer (GraphQL queries plus the token exchange).
Any error, permission problem or malformed node raises; ShopifyData is only built from complete
data. Values from Shopify are untrusted: error messages name fields, never values.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from reconciler.models import ShopifyData, ShopifyOrder, ShopifyShop, ShopifyVariant
from reconciler.reconcile import ConfigurationError
from reconciler.sources.http import (
    IncompleteResponse,
    ShopifyGraphQL,
    ShopifyQueryError,
    SourceError,
    check_scopes,
)

ACCESS_SCOPES_QUERY = """
query AccessScopes {
  currentAppInstallation { accessScopes { handle } }
}
"""

SHOP_QUERY = """
query Shop {
  shop { currencyCode taxesIncluded }
}
"""

# No `query: "product_status:active"` filter: the search index can lag behind, which would
# silently drop a just-activated product. All variants are read and filtered here instead.
VARIANTS_QUERY = """
query Variants($first: Int!, $after: String, $locationId: ID!) {
  productVariants(first: $first, after: $after) {
    nodes {
      id
      sku
      price
      product { status }
      inventoryItem {
        tracked
        inventoryLevel(locationId: $locationId) {
          quantities(names: ["on_hand"]) { name quantity }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

ORDERS_QUERY = """
query Orders($first: Int!, $after: String, $query: String!) {
  orders(first: $first, after: $after, sortKey: CREATED_AT, query: $query) {
    nodes { id createdAt displayFinancialStatus cancelledAt test }
    pageInfo { hasNextPage endCursor }
  }
}
"""

ACTIVE = "ACTIVE"
ORDER_GID_PREFIX = "gid://shopify/Order/"
UNKNOWN_FINANCIAL_STATUS = "UNKNOWN"
"""Used when Shopify returns no financial status; such an order is never PAID, so not eligible."""
_PERMISSION_DENIED = (401, 403)


@dataclass(frozen=True)
class ShopifyScope:
    """What to read; built from settings by the caller."""

    location_id: str
    order_since: datetime
    """Start of the order lookback; computed from the same as_of that reconcile() uses."""
    variant_page_size: int = 100
    """100 keeps the query cost well below Shopify's 1000-point limit per query."""
    order_page_size: int = 100


def _utc_now() -> datetime:
    return datetime.now(UTC)


def fetch_shopify_data(
    graphql: ShopifyGraphQL, scope: ShopifyScope, now: Callable[[], datetime] = _utc_now
) -> ShopifyData:
    """Read every record reconcile() needs. Raises instead of returning partial data."""
    fetched_at = now()
    with _permission_errors():
        check_access_scopes(graphql)
        shop = fetch_shop(graphql)
        variants = fetch_variants(graphql, scope)
        orders = fetch_orders(graphql, scope)
    try:
        return ShopifyData(
            fetched_at=fetched_at, shop=shop, variants=tuple(variants), orders=tuple(orders)
        )
    except ValidationError as error:
        raise IncompleteResponse(f"Shopify data invalid in {_fields(error)}") from None


def check_access_scopes(graphql: ShopifyGraphQL) -> None:
    data = graphql.query(ACCESS_SCOPES_QUERY)
    scopes = _dig_list(data, ("currentAppInstallation", "accessScopes"))
    handles = [s.get("handle") if isinstance(s, dict) else None for s in scopes]
    if not all(isinstance(h, str) for h in handles):
        raise IncompleteResponse("currentAppInstallation.accessScopes: handle missing")
    check_scopes(handles)  # type: ignore[arg-type]  # all str, checked above


def fetch_shop(graphql: ShopifyGraphQL) -> ShopifyShop:
    shop = graphql.query(SHOP_QUERY).get("shop")
    if not isinstance(shop, dict):
        raise IncompleteResponse("shop: not found in response")
    row = {"currency": shop.get("currencyCode"), "taxes_included": shop.get("taxesIncluded")}
    return _validate(row, ShopifyShop, "shop")


def fetch_variants(graphql: ShopifyGraphQL, scope: ShopifyScope) -> list[ShopifyVariant]:
    """Variants of ACTIVE products, with on_hand at the configured location.

    The location is confirmed through inventory data (no read_locations scope): at least one
    variant, of any product status, must have an inventory level there. Otherwise a wrong
    SHOPIFY_LOCATION_ID and an empty location look the same, so the run is refused.
    """
    variables = {"first": scope.variant_page_size, "locationId": scope.location_id}
    variants: list[ShopifyVariant] = []
    location_seen = False
    for node in graphql.iter_connection(VARIANTS_QUERY, ("productVariants",), variables):
        level = _inventory_level(node)
        location_seen = location_seen or level is not None
        if _product_status(node) == ACTIVE:
            variants.append(_variant(node, level))
    if not location_seen:
        raise ConfigurationError(
            "SHOPIFY_LOCATION_ID could not be confirmed: no inventory item is stocked at that "
            "location. Check the location id, or stock at least one item there."
        )
    return variants


def fetch_orders(graphql: ShopifyGraphQL, scope: ShopifyScope) -> list[ShopifyOrder]:
    """Orders created since `order_since`. Eligibility (PAID, not cancelled, not test) is
    decided by reconcile.is_eligible_order, not here. Draft orders are never queried."""
    variables = {"first": scope.order_page_size, "query": order_search(scope.order_since)}
    return [_order(node) for node in graphql.iter_connection(ORDERS_QUERY, ("orders",), variables)]


def order_search(since: datetime) -> str:
    """Shopify search syntax, built only from our own datetime (never from external text).
    Truncating to whole seconds can only include more orders; reconcile() filters exactly."""
    if since.tzinfo is None:
        raise ValueError("order_since must be timezone-aware")
    return f"created_at:>='{since.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}'"


# --- node -> model -------------------------------------------------------------------------


def _product_status(node: dict[str, Any]) -> str:
    product = node.get("product")
    status = product.get("status") if isinstance(product, dict) else None
    if not isinstance(status, str):
        raise IncompleteResponse("productVariants: product.status missing")
    return status


def _inventory_item(node: dict[str, Any]) -> dict[str, Any]:
    item = node.get("inventoryItem")
    if not isinstance(item, dict):
        raise IncompleteResponse("productVariants: inventoryItem missing")
    return item


def _inventory_level(node: dict[str, Any]) -> dict[str, Any] | None:
    """None = the variant is not stocked at the location."""
    level = _inventory_item(node).get("inventoryLevel")
    if level is not None and not isinstance(level, dict):
        raise IncompleteResponse("productVariants: inventoryLevel malformed")
    return level


def _on_hand(level: dict[str, Any] | None) -> int | None:
    if level is None:
        return None
    quantities = level.get("quantities")
    if not isinstance(quantities, list):
        raise IncompleteResponse("productVariants: inventoryLevel.quantities missing")
    on_hand = [q for q in quantities if isinstance(q, dict) and q.get("name") == "on_hand"]
    if len(on_hand) != 1 or type(on_hand[0].get("quantity")) is not int:
        raise IncompleteResponse("productVariants: on_hand quantity missing or not an integer")
    return on_hand[0]["quantity"]


def _variant(node: dict[str, Any], level: dict[str, Any] | None) -> ShopifyVariant:
    row = {
        "variant_id": node.get("id"),
        "sku": node.get("sku"),
        "price": node.get("price"),
        "inventory_tracked": _inventory_item(node).get("tracked"),
        "on_hand": _on_hand(level),
    }
    return _validate(row, ShopifyVariant, "productVariants")


def _order(node: dict[str, Any]) -> ShopifyOrder:
    order_id = node.get("id")
    if not isinstance(order_id, str) or not order_id.startswith(ORDER_GID_PREFIX):
        raise IncompleteResponse("orders: id is not an order GID")
    status = node.get("displayFinancialStatus")
    cancelled_at = node.get("cancelledAt")
    if cancelled_at is not None and not isinstance(cancelled_at, str):
        raise IncompleteResponse("orders: cancelledAt malformed")
    row = {
        "order_id": order_id,
        "created_at": node.get("createdAt"),
        "financial_status": UNKNOWN_FINANCIAL_STATUS if status is None else status,
        "cancelled": cancelled_at is not None,
        "test": node.get("test"),
    }
    return _validate(row, ShopifyOrder, "orders")


# --- helpers -------------------------------------------------------------------------------


@contextmanager
def _permission_errors() -> Iterator[None]:
    """HTTP 401/403 and GraphQL ACCESS_DENIED mean the app is misconfigured, not a glitch."""
    try:
        yield
    except ShopifyQueryError as error:
        if "ACCESS_DENIED" in error.codes:
            raise ConfigurationError(
                "Shopify denied access to the data (ACCESS_DENIED); check that the app is "
                "installed with read_products, read_inventory and read_orders."
            ) from None
        raise
    except SourceError as error:
        if error.status_code in _PERMISSION_DENIED:
            raise ConfigurationError(
                f"Shopify rejected the request (HTTP {error.status_code}); check the app "
                "installation and its scopes."
            ) from None
        raise


def _dig_list(data: dict[str, Any], path: tuple[str, ...]) -> list[Any]:
    node: Any = data
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, list):
        raise IncompleteResponse(f"{'.'.join(path)}: not found in response")
    return node


def _validate[M: BaseModel](row: dict[str, Any], model: type[M], where: str) -> M:
    """Row -> model. Errors name the fields, never the (untrusted) values."""
    try:
        return model.model_validate(row)
    except ValidationError as error:
        raise IncompleteResponse(f"{where}: unexpected data in {_fields(error)}") from None


def _fields(error: ValidationError) -> list[str]:
    return sorted({str(issue["loc"][0]) for issue in error.errors() if issue["loc"]})
