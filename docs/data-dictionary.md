# Data dictionary

What every field means, where it comes from, and which system is the source of truth.
The full v1 rules are in [CLAUDE.md](../CLAUDE.md#reconciliation-rules-v1-decided-2026-11-01).

## Compared fields

| Field | Shopify (GraphQL Admin API) | ERPNext (REST) | Source of truth | Notes |
|---|---|---|---|---|
| SKU | `ProductVariant.sku` | `Item.item_code` | ERPNext | Matching key; surrounding whitespace trimmed, case-sensitive. Empty / duplicate → data-quality discrepancy |
| Price | `ProductVariant.price` | `Item Price.price_list_rate` | ERPNext | `Decimal`; mismatch when difference > 0.01. Only applicable rows (see below) |
| Currency | `Shop.currencyCode` | `Price List.currency` | — | Must both equal `CURRENCY`, else configuration error. No conversion |
| Tax treatment | `Shop.taxesIncluded` | `ERPNEXT_PRICES_INCLUDE_TAX` (config) | — | Prices compared only when both are known and equal |
| Stock | `InventoryLevel.quantities(names: ["on_hand"])` at `SHOPIFY_LOCATION_ID` | `Bin.actual_qty` in `ERPNEXT_WAREHOUSE` | ERPNext | Physical quantity. No Bin row = 0 only if the warehouse was confirmed and all Bin pages were retrieved; otherwise skipped. Fractional ERPNext qty → skipped |
| Inventory tracked | `InventoryItem.tracked` | — | — | Untracked variants → stock check skipped |
| Order id | `Order.id` (GID, `gid://shopify/Order/…`) | Sales Order custom field named by `ERPNEXT_ORDER_ID_FIELD` | Shopify | Only submitted Sales Orders (`docstatus = 1`) match |

## ERPNext item scope

| ERPNext field | Required value |
|---|---|
| `disabled` | 0 |
| `is_sales_item` | 1 |
| `is_stock_item` | 1 (excludes services) |
| `has_variants` | 0 (excludes variant templates) |
| `item_group` | one of `ERPNEXT_ITEM_GROUPS` |

## ERPNext price selection

Reproduces ERPNext v16.53.0's selling-price lookup for one unit in the stock UOM, without customer
or batch, on the run date (calendar day in `ERPNEXT_TIMEZONE`). Details in
[CLAUDE.md](../CLAUDE.md#reconciliation-rules-v1-decided-2026-11-01).

| Item Price field | Candidate rule | Precedence |
|---|---|---|
| `price_list` | equals `ERPNEXT_PRICE_LIST` | — |
| `customer`, `supplier`, `batch_no` | all empty | — |
| `uom` | the item's `stock_uom`, or empty (empty also → `PRICE_UOM_MISSING`) | 2nd: stock UOM > `''` > NULL |
| `packing_unit` | fits one unit: 0 or 1 | — |
| `valid_from` | empty or ≤ run date | 1st: newest wins; empty ranks last |
| `valid_upto` | empty or ≥ run date | not a tie-breaker |
| `creation`, `name`, `price_list_rate` | — | not tie-breakers → a tie is `PRICE_AMBIGUOUS` |

Precedence follows **MariaDB** NULL ordering; PostgreSQL is unsupported (`ERPNEXT_DATABASE` must be `mariadb`).
| (variant) | no own candidate → the template item's rows (`Item.variant_of`) | same rules |

## Shopify variants in scope

| Shopify field | Rule |
|---|---|
| `Product.status` | `ACTIVE` only (`DRAFT`, `ARCHIVED`, `UNLISTED` excluded in v1). Filtered in code, not with the `product_status` search filter (the search index can lag) |
| `InventoryItem.inventoryLevel(locationId)` | null → not stocked at the location (stock check skipped). At least one variant, of any status, must be stocked there, or the run is refused (location not confirmed) |
| `InventoryLevel.quantities(names: ["on_hand"])` | exactly one integer `on_hand` value, else the response is incomplete |

## Eligible Shopify orders

| Shopify field | Rule |
|---|---|
| `createdAt` | within the last `ORDER_LOOKBACK_DAYS` days (default 30, max 60) |
| `displayFinancialStatus` | `PAID` (null from Shopify is recorded as `UNKNOWN`, so never eligible) |
| `cancelledAt` | empty |
| `test` | false (unless `INCLUDE_TEST_ORDERS=true` on a verified, approved development store) |
| Draft orders | never fetched (separate `DraftOrder` object) |

Orders are fetched with the search `created_at:>='<lookback start, UTC>'`. The other rules are applied
by `reconcile.is_eligible_order`. `INCLUDE_TEST_ORDERS` is not implemented yet, so test orders are
always excluded.

## Shopify app scopes

| Scope | Used for |
|---|---|
| `read_products` | product variants: SKU, price, product status |
| `read_inventory` | inventory tracked, `on_hand` at `SHOPIFY_LOCATION_ID`, confirming the location |
| `read_orders` | orders of the last 60 days at most |

Exactly these three are required. Any other scope (above all `write_*`) stops the tool.
