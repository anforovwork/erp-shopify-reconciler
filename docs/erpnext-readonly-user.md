# Read-only ERPNext API user

The reconciler talks to ERPNext with the API key of a dedicated user that can **only read**.
The code also refuses to send anything but `GET`, so a mistake has to get past two independent guards.

All steps are done by hand, by an ERPNext administrator, in the ERPNext web UI. The reconciler never
creates users, roles or fields.

## 1. Create a role

*Role → New*

- Role name: `Reconciler Read Only`
- Desk Access: on (needed for the REST API)

## 2. Give the role read permission only

*Role Permissions Manager* — for each DocType below, add a rule for `Reconciler Read Only`,
level 0, with **Read** ticked (and Report, optional). Leave Write, Create, Delete, Submit, Cancel,
Amend, Import, Export, Print, Email and Share **unticked**.

| DocType | Why the reconciler reads it |
|---|---|
| Item | item master, scope filters, stock UOM |
| Item Price | selling prices |
| Price List | price list currency |
| Bin | stock per warehouse |
| Warehouse | confirm the configured warehouse exists before a missing Bin counts as zero |
| Sales Order | order matching (custom GID field, `docstatus`) |

## 3. Create the user

*User → New*

- A system user with a non-personal email, e.g. `reconciler@your-test-domain.example`.
- Roles: **only** `Reconciler Read Only`. Remove every role ERPNext adds by default.

## 4. Generate API keys

Open the user → *Settings → API Access → Generate Keys*. ERPNext shows the API secret **once**.
Put the key and secret in `.env` as `ERPNEXT_API_KEY` / `ERPNEXT_API_SECRET`. Never commit them,
paste them into chat, or put them in an issue.

## 5. Check it by hand

Log in to the UI as this user (set a password, or use *Login As* as an administrator). Open an Item and a
Sales Order: there should be no **Save**, **New** or **Submit** buttons. Don't test write access through the
API. The reconciler never sends write requests, not even to prove they fail.

## Order mapping field (optional, manual)

Order reconciliation needs the Shopify order GID stored on each Sales Order. An administrator adds it
with *Customize Form → Sales Order*: a **Data** field, e.g. label `Shopify Order GID`, ideally unique and
read-only. Frappe v15 automatically prefixes custom field names with `custom_`, so check the final field name
in Customize Form and put it in `.env` as `ERPNEXT_ORDER_ID_FIELD` (e.g. `custom_shopify_order_gid`).

Whatever creates Sales Orders from Shopify (an integration or a person) must fill that field. If the
setting is empty or the field doesn't exist, the reconciler skips the order check with a warning.
It never creates the field.
