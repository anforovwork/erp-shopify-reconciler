"""Markdown and CSV report output.

Reports always show timestamps, warnings and skipped checks next to the discrepancies.
Keys (SKUs, order ids) come from external systems and are untrusted, so every text cell is
escaped: Markdown control characters in the Markdown report, spreadsheet formulas in the CSV.
"""

import csv
import io
from decimal import Decimal

from reconciler.models import (
    Discrepancy,
    DiscrepancyCategory,
    ReconciliationResult,
)

_MARKDOWN_SPECIAL = set("\\`*_[]<>|~#!")
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

CSV_COLUMNS = (
    "record_type",
    "type",
    "category",
    "key",
    "shopify_value",
    "erp_value",
    "source_of_truth",
    "suggested_action",
    "detail",
)


def format_amount(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")


# --- Markdown ------------------------------------------------------------------------------


def escape_markdown(text: str) -> str:
    flat = " ".join(text.splitlines())
    return "".join(f"\\{char}" if char in _MARKDOWN_SPECIAL else char for char in flat)


def render_markdown(result: ReconciliationResult) -> str:
    comparison = _by_category(result, DiscrepancyCategory.COMPARISON)
    data_quality = _by_category(result, DiscrepancyCategory.DATA_QUALITY)
    lines = [
        "# Reconciliation report",
        "",
        "| | |",
        "|---|---|",
        f"| Generated | {result.generated_at.isoformat()} |",
        f"| Shopify data fetched | {result.shopify_fetched_at.isoformat()} |",
        f"| ERPNext data fetched | {result.erpnext_fetched_at.isoformat()} |",
        f"| Comparison discrepancies | {len(comparison)} |",
        f"| Data-quality discrepancies | {len(data_quality)} |",
        f"| Skipped checks | {len(result.skipped)} |",
        f"| Warnings | {len(result.warnings)} |",
        "",
        "## Warnings",
        "",
        *(_bullets(result.warnings)),
        "",
        "## Comparison discrepancies",
        "",
        *_discrepancy_table(comparison),
        "",
        "## Data-quality discrepancies",
        "",
        *_discrepancy_table(data_quality),
        "",
        "## Skipped checks",
        "",
        *_skipped_table(result),
    ]
    return "\n".join(lines) + "\n"


def _by_category(result: ReconciliationResult, category: DiscrepancyCategory) -> list[Discrepancy]:
    return [d for d in result.discrepancies if d.category is category]


def _bullets(items: tuple[str, ...]) -> list[str]:
    return [f"- {escape_markdown(item)}" for item in items] or ["_None._"]


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(escape_markdown(cell) for cell in cells) + " |"


def _discrepancy_table(discrepancies: list[Discrepancy]) -> list[str]:
    if not discrepancies:
        return ["_None._"]
    header = ["Type", "Key", "Shopify", "ERPNext", "Source of truth", "Suggested action", "Detail"]
    rows = [
        _row(
            [
                d.type.value,
                d.key,
                format_amount(d.shopify_value),
                format_amount(d.erp_value),
                d.source_of_truth.value,
                d.suggested_action,
                d.detail,
            ]
        )
        for d in discrepancies
    ]
    return [_row(header), "|" + "---|" * len(header), *rows]


def _skipped_table(result: ReconciliationResult) -> list[str]:
    if not result.skipped:
        return ["_None._"]
    rows = [_row([s.check.value, s.key or "(all)", s.reason]) for s in result.skipped]
    return [_row(["Check", "Key", "Reason"]), "|---|---|---|", *rows]


# --- CSV -----------------------------------------------------------------------------------


def neutralize_formula(text: str) -> str:
    """Stop spreadsheets from running a cell as a formula (CSV injection)."""
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


def render_csv(result: ReconciliationResult) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in _csv_rows(result):
        safe = {
            column: value if column in ("shopify_value", "erp_value") else neutralize_formula(value)
            for column, value in row.items()
        }
        writer.writerow(safe)
    return buffer.getvalue()


def _csv_rows(result: ReconciliationResult) -> list[dict[str, str]]:
    timestamps = {
        "generated_at": result.generated_at,
        "shopify_fetched_at": result.shopify_fetched_at,
        "erpnext_fetched_at": result.erpnext_fetched_at,
    }
    rows = [_csv_row("meta", type=name, detail=ts.isoformat()) for name, ts in timestamps.items()]
    rows += [_csv_row("warning", detail=warning) for warning in result.warnings]
    rows += [
        _csv_row(
            "discrepancy",
            type=d.type.value,
            category=d.category.value,
            key=d.key,
            shopify_value=format_amount(d.shopify_value),
            erp_value=format_amount(d.erp_value),
            source_of_truth=d.source_of_truth.value,
            suggested_action=d.suggested_action,
            detail=d.detail,
        )
        for d in result.discrepancies
    ]
    rows += [
        _csv_row("skipped", type=s.check.value, key=s.key or "", detail=s.reason)
        for s in result.skipped
    ]
    return rows


def _csv_row(record_type: str, **values: str) -> dict[str, str]:
    row = dict.fromkeys(CSV_COLUMNS, "")
    row["record_type"] = record_type
    row.update(values)
    return row
