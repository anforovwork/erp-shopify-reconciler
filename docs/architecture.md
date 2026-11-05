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

## `reconcile run` (implemented)

```
load_settings(.env) ─► to_options(as_of = now, UTC)
  │
  ├─► fetch_erp_live      ─► ErpData       (sources/erpnext.py, read-only)
  ├─► fetch_shopify_live  ─► ShopifyData   (sources/shopify.py, read-only)
  ▼
reconcile(shopify, erp, options) ─► ReconciliationResult      (pure, no network)
  ▼
reports/snapshot-<UTC>.json   options + both data sets + result   (never overwritten)
reports/latest.md, latest.csv rendered from the result             (replaced atomically)
```

- **All or nothing.** Outputs are written only after both fetches and `reconcile()` succeed, so
  a failed run never leaves a partial or stale-looking report. Each file is written to a
  temporary file and then moved into place. The snapshot is written first and refuses to
  overwrite an existing file. If two runs fall in the same second, the second one stops before
  it touches `latest.*`.
- **Snapshot** (`snapshot.py`). It holds only pydantic models, never `Settings`, so it contains no
  credentials. Decimals are stored as JSON strings. `load_snapshot` validates it back into the
  same models, so `reconcile report` (next task) can render it without the network.
- **Terminal output.** Only timestamps, counts per discrepancy type, skipped/warning counts
  and the output paths are printed. SKUs, order ids and warning text appear only in the escaped
  reports. Errors print as `ERROR: <type>: <message>` on stderr; client and config messages name
  fields, never values.
- **Exit codes.** `0` no discrepancies, `1` discrepancies found, `2` configuration error, source
  error, read-only violation or file error.
- **Files are private.** Reports and snapshots are created readable by the owner only (mode 0600)
  and are git-ignored (`reports/*`).
