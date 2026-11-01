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

## Applicable ERPNext Item Price row

| Item Price field | Rule |
|---|---|
| `price_list` | equals `ERPNEXT_PRICE_LIST` |
| `customer` | empty (no customer-specific prices) |
| `batch_no` | empty (no batch-specific prices) |
| `uom` | equals the item's `stock_uom`. Empty UOM → `PRICE_UOM_MISSING` (ERPNext v16 requires it; its own lookup treats empty as "any UOM") |
| `packing_unit` | 0 or 1 (larger values only apply to multiples of the pack size) |
| `valid_from` / `valid_upto` | run date inside the range, inclusive; empty = open-ended |

## Eligible Shopify orders

| Shopify field | Rule |
|---|---|
| `createdAt` | within the last `ORDER_LOOKBACK_DAYS` days (default 30) |
| `displayFinancialStatus` | `PAID` |
| `cancelledAt` | empty |
| `test` | false |
| Draft orders | never fetched (separate `DraftOrder` object) |
