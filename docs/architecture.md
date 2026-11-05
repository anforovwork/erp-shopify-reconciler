# Architecture

_To be written once the data flow is implemented: sources → models → reconcile → report / MCP._

## Shopify access (implemented)

```
.env: SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET (SecretStr)
  │
  ▼
ShopifyAccessToken ── POST /admin/oauth/access_token ──┐   grant_type=client_credentials
  │  token in memory only; renewed 5 min before expiry  │
  ▼                                                     ▼
ShopifyGraphQL ────── POST /admin/api/<v>/graphql.json ─► ReadOnlyTransport (shopify_policy) ─► Shopify
                       one query + fragments only
```

- **One guard for both requests.** The token client and the GraphQL client share one
  `ReadOnlyTransport` with `shopify_policy` (`sources/http.py`). Besides GraphQL queries it allows
  exactly one request: `POST` to the exact path `/admin/oauth/access_token`, HTTPS, form-encoded,
  with exactly `grant_type=client_credentials`, the configured `client_id` and a `client_secret`.
  Any other grant, extra field (such as `scope`), OAuth path or REST endpoint is a `ReadOnlyViolation`
  and never leaves the process.
- **Scopes.** The app must hold exactly `read_products`, `read_inventory` and `read_orders`. This is
  checked on the token response (when it lists scopes) and again through
  `currentAppInstallation.accessScopes` before any data is read. Any other scope or a missing one stops
  the run (exit code 2).
- **Reading** (`sources/shopify.py`, `fetch_shopify_data`):
  1. scopes
  2. shop (`currencyCode`, `taxesIncluded`)
  3. all product variants, filtered to `ACTIVE` products in code, with `on_hand` at
     `SHOPIFY_LOCATION_ID`
  4. orders created since the lookback start

  Cursor pagination, `THROTTLED` and HTTP 429 back-off and timeouts come from `sources/http.py`. Any
  error or malformed node raises; partial data is never returned.
- **Location without `read_locations`.** The location counts as confirmed when at least one variant
  has an inventory level there. Otherwise a wrong id and an empty location look the same, so the run
  is refused.
- **Smoke test.** `scripts/shopify_smoke.py` runs the same path against the real store and prints
  only yes/no checks and counts.
