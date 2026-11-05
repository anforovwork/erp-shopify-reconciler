# Time log

Time spent by phase, from start to a working, verified demo. Everything below was built from
scratch on my own machine and accounts.

| Phase | Hours | What was done |
|---|---|---|
| Rules | 0.2 | Project rules for Claude Code (`CLAUDE.md`) and repository skeleton |
| Infrastructure | 5.0 | WSL2 Ubuntu on Windows 11, Docker Desktop (WSL integration and credential-helper fixes), ERPNext v16 in Docker, company/warehouse/price list set up, GitHub with SSH key, pre-commit and CI, Claude Code in Ubuntu |
| Business rules and core | 2.5 | Answered the 20 open questions on matching, scope, prices, stock and orders; data models, reconciliation rules, reports, settings, read-only HTTP guard, ERPNext client |
| Test data | 3.5 | 300 synthetic products with prices and stock, imported into ERPNext and Shopify and checked against the import files |
| Shopify setup | 1.5 | Development store and Dev Dashboard app: least-privilege scopes, install, location, currency (CAD) and tax settings |
| Shopify client, CLI and MCP | 2.5 | Client-credentials auth, Shopify client, `reconcile run`, read-only MCP server, live runs against both systems |
| Documentation | 1.5 | README, architecture, sample report, data dictionary |

**Total: about 17 hours.**
