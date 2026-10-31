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

- **Read-only** against both systems (Shopify read scopes only; ERPNext read-only role).
- **No secrets in the repo** — environment variables only, gitleaks on every commit and in CI.
- **MCP tools return reports, never take actions**; store data is treated as untrusted text (prompt-injection defence).
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

## How I built it

_To be written: process (plan → Claude implements → I review the diff → tests → secret scan → merge),
what Claude got wrong and how I caught it, total hours._
