# erp-shopify-reconciler

**One trusted answer for every number.** A read-only tool that finds where Shopify and ERPNext disagree —
prices, stock levels, missing products and missing orders — and tells you which system is right.
Includes a read-only MCP server so anyone can ask Claude _"which SKUs have a stock mismatch?"_

> Status: in development. Built with Claude Code; every change reviewed and tested by me. Time log: [HOURS.md](HOURS.md).

## Why

When a business runs a storefront and an ERP side by side, the two drift apart: a price changed in one place,
stock counted in the warehouse but not pushed to the store, an order that never reached the ERP.
Each mismatch is lost money or a disappointed customer. This tool finds them before customers do.

## Safety by design

- **Read-only** against both systems, enforced twice: by the credentials (Shopify read scopes only,
  checked at startup; ERPNext [read-only role](docs/erpnext-readonly-user.md)) and by the HTTP layer
  (ERPNext `GET` only; Shopify GraphQL queries only, mutations rejected).
- **No secrets in the repo** — environment variables only. Gitleaks scans staged changes on every commit
  (pre-commit hook) and the full history in CI after every push.
- **MCP tools return reports, never take actions** (stdio only, minimal structured data, no customer
  details); store data is treated as untrusted text (prompt-injection defence).
- **No real customer data** — test data is generated.

## Source of truth

| Data | Owner |
|---|---|
| Products, prices, stock | ERPNext |
| Orders | Shopify |

See [docs/data-dictionary.md](docs/data-dictionary.md).

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

`reconcile-mcp` is a read-only MCP server (stdio). Its three tools — `get_summary`,
`list_discrepancies` and `explain_sku` — read the **newest snapshot** in `reports/` that
`reconcile run` saved. They never contact Shopify or ERPNext and cannot change anything, so answers
are only as fresh as the last run: every answer includes the snapshot time and its age. Run
`reconcile run` again for current data.

Replace `<you>` with your Ubuntu user name in the paths below.

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

**Example questions:**

- _"Which SKUs have a stock mismatch?"_
- _"Summarise the last reconciliation. How old is the data?"_
- _"Why is SKU FAKE-PRICE-1 flagged, and which system is right?"_

## How I built it

_To be written: process (plan → Claude implements → I review the diff → tests → secret scan → merge),
what Claude got wrong and how I caught it, total hours._
