# Data dictionary

What every field means, where it comes from, and which system is the source of truth.

| Field | Shopify | ERPNext | Source of truth | Notes |
|---|---|---|---|---|
| SKU | variant `sku` | Item `item_code` | ERPNext | Matching key; trimmed, case-sensitive |
| Price | variant `price` | Item Price (selling price list) | ERPNext | Compared as Decimal, tolerance 0.01 |
| Stock | inventory level `available` | Bin `actual_qty` (warehouse) | ERPNext | |
| Order | order `id` / `name` | Sales Order (reference to Shopify id) | Shopify | |
