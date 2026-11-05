# erp-shopify-reconciler

[![CI](https://github.com/anforovwork/erp-shopify-reconciler/actions/workflows/ci.yml/badge.svg)](https://github.com/anforovwork/erp-shopify-reconciler/actions/workflows/ci.yml)

**One trusted answer when Shopify and ERPNext disagree on price, stock or orders.**

A read-only tool that compares the storefront (Shopify) with the ERP (ERPNext), reports every
mismatch with the system that is right and a suggested fix, and exposes the results to Claude
through a read-only MCP server. Built Claude Code-first — see [How I built it](#how-i-built-it).
About 17 hours from an empty machine to a verified demo: [HOURS.md](HOURS.md).

## Why I built this

My production work on ERP, data and internal tools belongs to my employer and can't be published.
So I built this project from scratch — infrastructure included — to show the same kind of work in
public: two systems of record that disagree, one source of truth for every number, read-only
access with least-privilege credentials, secrets that never leave `.env`, and an AI assistant that
can answer questions about the data without being able to change anything.

## Demo result

A live run against a local ERPNext and a Shopify dev store loaded with the same 300 synthetic
products (`DEMO-BOS-*` SKUs), after a few edits in Shopify only.
Full report: [docs/sample-report.md](docs/sample-report.md).

| Finding | SKU | Shopify | ERPNext | Suggested action |
|---|---|---|---|---|
| `MISSING_IN_SHOPIFY` | DEMO-BOS-0150 | not active | in catalogue | Create it in Shopify, or move the ERPNext item out of the online item groups |
| `MISSING_IN_ERP` | DEMO-BOS-9999 | active | no item | Create the ERPNext item, or correct the Shopify SKU |
| `PRICE_MISMATCH` | DEMO-BOS-0001 | 479.99 | 499.99 | Set the Shopify price to the ERPNext price |
| `PRICE_MISMATCH` | DEMO-BOS-0100 | 22.99 | 21.99 | Set the Shopify price to the ERPNext price |
| `STOCK_MISMATCH` | DEMO-BOS-0002 | 15 | 18 | Confirm the ERPNext count, then set Shopify on-hand to match |
| `STOCK_MISMATCH` | DEMO-BOS-0300 | 0 | 11 | Confirm the ERPNext count, then set Shopify on-hand to match |

- 6 discrepancies → **exit code 1**. ERPNext is the source of truth for all six.
- Same stores before the edits (identical data): **0 discrepancies across 300 SKUs**, exit code 0.
- The order check was skipped (and shown as skipped) because the demo ERPNext has no Sales Order
  field for the Shopify order ID yet.

## Safety by design

- **Read-only guard in the HTTP transport** ([`sources/http.py`](src/reconciler/sources/http.py)),
  so no client code can bypass it. ERPNext: `GET` on `/api/resource/...` only, no request body,
  no `run_method`/`cmd`, no method-override headers, paths checked after URL decoding. Shopify:
  `POST` only to the GraphQL endpoint, and only if graphql-core parses the document as exactly one
  query (mutations and subscriptions are rejected). Only the configured host; redirects never followed.
- **Exact scopes.** The Shopify app must hold exactly `read_products`, `read_inventory`,
  `read_orders`, checked at startup; any `write_*` scope stops the run. ERPNext uses a
  [dedicated read-only user](docs/erpnext-readonly-user.md).
- **Secrets never logged.** Credentials come only from `.env` (git-ignored) as `SecretStr`; the
  Shopify access token lives in memory only; error messages name fields, never values.
- **Gitleaks** scans staged changes on every commit (pre-commit hook) and the full history in CI.
- **Prompt-injection handling.** SKUs and other store data are treated as untrusted text; the MCP
  server instructions and every tool description tell Claude to treat them as data, never
  instructions. Tools return minimal structured data — no customer details or free text.
- **MCP reads snapshots only.** The server (stdio) reads saved snapshots, imports nothing from the
  API clients, never loads `.env` and cannot contact Shopify or ERPNext.

## Source of truth

| Data | Owner | Why |
|---|---|---|
| Products, SKUs, prices, stock | ERPNext | Item master, finance pricing, warehouse counts |
| Orders | Shopify | Orders originate in the store |

Rules in detail: [CLAUDE.md](CLAUDE.md#reconciliation-rules-v1-decided-2026-11-01) and
[docs/data-dictionary.md](docs/data-dictionary.md). Design: [docs/architecture.md](docs/architecture.md).

## Quick start (Linux / WSL2)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
cp .env.example .env   # then fill in your dev-store values
pytest
```

Run a reconciliation (read-only; writes `reports/latest.md`, `reports/latest.csv` and a
timestamped JSON snapshot, all git-ignored):

```bash
reconcile run                    # or: reconcile run --report out/r.md --csv out/r.csv
echo $?                          # 0 = all agree, 1 = discrepancies found, 2 = error
```

## Ask Claude (MCP server)

`reconcile-mcp` is a read-only MCP server (stdio) with three tools — `get_summary`,
`list_discrepancies` and `explain_sku`. They read the **newest snapshot** that `reconcile run`
saved in `reports/`, so every answer includes the snapshot time and its age.

Example exchange from the demo snapshot:

> **Q:** Which SKUs have a stock mismatch, and what should we do about them?
>
> **A:** Two SKUs don't match. DEMO-BOS-0002 shows 15 in Shopify but 18 in ERPNext. DEMO-BOS-0300
> shows 0 in Shopify but 11 in ERPNext. ERPNext is the source of truth here. Check the ERPNext
> warehouse count for each SKU, then set Shopify's on-hand stock to match it. Do DEMO-BOS-0300
> first: Shopify probably shows it as sold out while 11 are in stock, so you could be losing sales.
> These numbers come from the snapshot taken 30 minutes ago (03:19 UTC), so run `reconcile run`
> again before you change anything.

Other questions: _"Summarise the last reconciliation. How old is the data?"_ ·
_"Why is DEMO-BOS-0001 flagged, and which system is right?"_

Setup (replace `<you>` with your Ubuntu user name).

**Claude Code (in Ubuntu):**

```bash
claude mcp add reconciler -- \
  /home/<you>/projects/erp-shopify-reconciler/.venv/bin/reconcile-mcp \
  --reports-dir /home/<you>/projects/erp-shopify-reconciler/reports
claude mcp list                  # should show: reconciler ... Connected
```

**Claude Desktop (Windows host, server runs in WSL):** open *Settings → Developer → Edit Config*
(`%APPDATA%\Claude\claude_desktop_config.json`), add the server, then restart Claude Desktop.
Check the distribution name with `wsl -l -v` in PowerShell.

```json
{
  "mcpServers": {
    "reconciler": {
      "command": "wsl",
      "args": [
        "-d", "Ubuntu-24.04", "--",
        "/home/<you>/projects/erp-shopify-reconciler/.venv/bin/reconcile-mcp",
        "--reports-dir", "/home/<you>/projects/erp-shopify-reconciler/reports"
      ]
    }
  }
}
```

## How I built it

Built with Claude Code. Every task followed the same loop:

**rules in [CLAUDE.md](CLAUDE.md)** → **plan mode** → **I answer the business questions** →
**Claude implements** → **tests + ruff + gitleaks** → **smoke tests against the real systems** →
**PR** → **merge**.

My part: I wrote the rules (CLAUDE.md), made every business decision, reviewed and corrected each
plan before approving it, and verified the results with the test suite (506 tests, no network) and
with live runs against ERPNext and the Shopify dev store.

### Decisions I made

- **Source of truth:** ERPNext for catalogue, prices and stock; Shopify for orders.
- **No silent conversions:** if the store and price-list currencies differ, the tool stops with a
  configuration error instead of converting. Prices are compared only when both systems treat tax
  the same way.
- **A check that cannot run is reported as skipped, never as "all good"** — for example the order
  check while the ERPNext order-ID field is not configured.
- **Least privilege:** exactly three read scopes on Shopify, no `read_locations`; the configured
  location is confirmed from inventory data instead, and the tool refuses to run if it can't be.
- **Fail closed on bad data, not on odd data:** a malformed response stops the run, but an order
  without a financial status is recorded as `UNKNOWN` (never eligible) instead of failing everything.
- **Scope cut on purpose:** retrying after a mid-run token rejection was deferred — the token lives
  about 24 hours and a run takes seconds.
- **The MCP server only reads saved snapshots** — fast, no API rate limits, and no path to change data.

### Things found along the way

- **"GET only" was wrong for Shopify.** Shopify GraphQL reads are HTTP `POST`, so the original
  rule could not work; the guard now allows `POST` only for parsed, query-only GraphQL documents
  (`58d8e9f`).
- **A GET body could smuggle `run_method`.** Frappe builds request data from a JSON body even on
  `GET`, so ERPNext requests may not carry a body at all (`f439cd7`).
- **Price selection depends on the database.** ERPNext's price choice was reproduced from the
  installed v16.53 source; it relies on MariaDB's NULL ordering, so the database type is a required
  setting (`2658ae3`, `8730da5`).
- **Shopify's search index can lag**, so `ACTIVE` products are filtered in code, not via search.
- **The mcp 2.x SDK no longer ships FastMCP**; the server uses `MCPServer`.
- **Setting up the dev store:** the first app version had no scopes at all (caught before
  install); deactivating a location moved all stock to another location (verified 20/20 SKUs
  against the import file, then switched the configured location); the store currency was USD
  instead of CAD, which the startup currency check would have refused.

## Infrastructure built from scratch

| Layer | What I set up |
|---|---|
| Workstation | Windows 11 host, WSL2 Ubuntu 24.04, Python 3.12 virtual environment |
| Containers | Docker Desktop with WSL integration; fixed a Windows credential-helper setting that broke `docker pull` inside Ubuntu and diagnosed a WSL-integration crash |
| ERP | ERPNext v16 on the official `frappe_docker` stack (MariaDB, Redis, backend, workers, scheduler, websocket, frontend); company set up for Canada/CAD with warehouse and selling price list; a dedicated **Reconciler Read Only** role and API user ([guide](docs/erpnext-readonly-user.md)) |
| Storefront | Shopify development store set to CAD with tax-exclusive prices; one stock location; Dev Dashboard app with three read scopes and client-credentials auth |
| Test data | 300 synthetic products (`DEMO-BOS-*`) with prices and stock, loaded into both systems through their standard imports and checked against the import files |
| Source control and CI | GitHub with SSH key auth; pre-commit (gitleaks, ruff); GitHub Actions (secret scan of full history, lint, tests) with actions pinned to commit SHAs |
| AI tooling | Claude Code in Ubuntu with project rules in `CLAUDE.md`; read-only MCP server usable from Claude Code and Claude Desktop |

## What I would do in production

- **Scheduled runs** (cron or GitHub Actions) with an alert whenever the exit code is 1.
- **Order reconciliation:** add the Sales Order custom field for the Shopify order ID in ERPNext
  (by hand — the tool never creates it) and set `ERPNEXT_ORDER_ID_FIELD`.
- **`INCLUDE_TEST_ORDERS` for dev stores**, enabled only after the tool verifies it is connected to
  an approved development store (rule in CLAUDE.md; not implemented yet).
- **Snapshot history** to show trends: which mismatches recur and how fast they get fixed.
- **Load and pagination limits** tested for peak-season catalogue and order volumes.
- **Alerting on failures too**, not only on findings: exit code 2 (configuration or source error)
  should page someone, because a silent failure looks like "no discrepancies".

## License

MIT — see [LICENSE](LICENSE).
