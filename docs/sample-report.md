# Sample report (demo run)

Produced from synthetic `DEMO-BOS-*` data after edits in the Shopify dev store only (ERPNext
unchanged): the prices of DEMO-BOS-0001 and DEMO-BOS-0100 were changed, on-hand stock was lowered
for DEMO-BOS-0002 and DEMO-BOS-0300, DEMO-BOS-0150 was taken out of the active catalogue, and a
new product with SKU DEMO-BOS-9999 was added. Below is `reports/latest.md` from that run, unedited
except that its headings are one level lower. Exit code: 1.

## Reconciliation report

| | |
|---|---|
| Generated | 2026-11-05T03:19:24.583019+00:00 |
| Shopify data fetched | 2026-11-05T03:19:24.882041+00:00 |
| ERPNext data fetched | 2026-11-05T03:19:24.653321+00:00 |
| Comparison discrepancies | 6 |
| Data-quality discrepancies | 0 |
| Skipped checks | 1 |
| Warnings | 1 |

### Warnings

- Order reconciliation disabled: ERPNEXT\_ORDER\_ID\_FIELD is not configured or the field does not exist on ERPNext Sales Order. The tool does not create it.

### Comparison discrepancies

| Type | Key | Shopify | ERPNext | Source of truth | Suggested action | Detail |
|---|---|---|---|---|---|---|
| MISSING\_IN\_SHOPIFY | DEMO-BOS-0150 |  |  | erpnext | Create the product in Shopify, or move the ERPNext item out of the configured item groups if it is not sold online. |  |
| MISSING\_IN\_ERP | DEMO-BOS-9999 |  |  | erpnext | No ERPNext item has this code: create the item, or correct the Shopify SKU. |  |
| PRICE\_MISMATCH | DEMO-BOS-0001 | 479.99 | 499.99 | erpnext | Update the Shopify price to the ERPNext price. |  |
| PRICE\_MISMATCH | DEMO-BOS-0100 | 22.99 | 21.99 | erpnext | Update the Shopify price to the ERPNext price. |  |
| STOCK\_MISMATCH | DEMO-BOS-0002 | 15 | 18.0 | erpnext | Confirm the ERPNext warehouse count, then set Shopify on-hand inventory to match it. |  |
| STOCK\_MISMATCH | DEMO-BOS-0300 | 0 | 11.0 | erpnext | Confirm the ERPNext warehouse count, then set Shopify on-hand inventory to match it. |  |

### Data-quality discrepancies

_None._

### Skipped checks

| Check | Key | Reason |
|---|---|---|
| order | (all) | Order reconciliation disabled: ERPNEXT\_ORDER\_ID\_FIELD is not configured or the field does not exist on ERPNext Sales Order. The tool does not create it. |
