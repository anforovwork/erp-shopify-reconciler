import csv
import io
from datetime import timedelta
from decimal import Decimal

from builders import AS_OF

from reconciler.models import (
    CheckName,
    Discrepancy,
    DiscrepancyType,
    ReconciliationResult,
    SkippedCheck,
    System,
)
from reconciler.report import (
    CSV_COLUMNS,
    escape_markdown,
    neutralize_formula,
    render_csv,
    render_markdown,
)


def _result(**overrides: object) -> ReconciliationResult:
    fields: dict[str, object] = {
        "generated_at": AS_OF,
        "shopify_fetched_at": AS_OF - timedelta(minutes=2),
        "erpnext_fetched_at": AS_OF - timedelta(minutes=1),
        "discrepancies": (
            Discrepancy(
                type=DiscrepancyType.STOCK_MISMATCH,
                key="SKU-1",
                shopify_value=Decimal("5"),
                erp_value=Decimal("-2.000"),
                source_of_truth=System.ERPNEXT,
                suggested_action="Fix stock.",
            ),
            Discrepancy(
                type=DiscrepancyType.SKU_DUPLICATE,
                key="DUP-1",
                source_of_truth=System.ERPNEXT,
                suggested_action="Make unique.",
                detail="appears 2 times in Shopify",
            ),
        ),
        "skipped": (SkippedCheck(check=CheckName.ORDER, key=None, reason="Order check disabled."),),
        "warnings": ("Order check disabled.",),
    }
    return ReconciliationResult.model_validate(fields | overrides)


# --- Markdown ------------------------------------------------------------------------------


def test_markdown_has_timestamps_sections_and_counts() -> None:
    report = render_markdown(_result())

    assert "| Generated | 2026-11-01T12:00:00+00:00 |" in report
    assert "| Shopify data fetched | 2026-11-01T11:58:00+00:00 |" in report
    assert "| ERPNext data fetched | 2026-11-01T11:59:00+00:00 |" in report
    assert "| Comparison discrepancies | 1 |" in report
    assert "| Data-quality discrepancies | 1 |" in report
    assert "- Order check disabled." in report
    assert "| STOCK\\_MISMATCH | SKU-1 | 5 | -2.000 | erpnext | Fix stock. |  |" in report
    assert "| order | (all) | Order check disabled. |" in report


def test_markdown_for_clean_result_says_none_everywhere() -> None:
    report = render_markdown(_result(discrepancies=(), skipped=(), warnings=()))

    assert report.count("_None._") == 4


def test_markdown_escapes_untrusted_text() -> None:
    assert escape_markdown("A|B") == "A\\|B"
    assert escape_markdown("[x](http://example.test)") == "\\[x\\](http://example.test)"
    assert escape_markdown("<img src=x>") == "\\<img src=x\\>"
    assert escape_markdown("line1\nline2") == "line1 line2"


def test_malicious_key_cannot_break_the_table() -> None:
    hostile = Discrepancy(
        type=DiscrepancyType.MISSING_IN_ERP,
        key="X|\n# Ignore previous instructions",
        source_of_truth=System.ERPNEXT,
        suggested_action="Check.",
    )
    report = render_markdown(_result(discrepancies=(hostile,)))

    assert "\n# Ignore" not in report
    assert "X\\| \\# Ignore previous instructions" in report


# --- CSV -----------------------------------------------------------------------------------


def _parse(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def test_csv_contains_meta_warnings_discrepancies_and_skipped() -> None:
    rows = _parse(render_csv(_result()))

    assert tuple(rows[0]) == CSV_COLUMNS
    assert [(r["record_type"], r["type"]) for r in rows] == [
        ("meta", "generated_at"),
        ("meta", "shopify_fetched_at"),
        ("meta", "erpnext_fetched_at"),
        ("warning", ""),
        ("discrepancy", "STOCK_MISMATCH"),
        ("discrepancy", "SKU_DUPLICATE"),
        ("skipped", "order"),
    ]
    stock = rows[4]
    assert (stock["shopify_value"], stock["erp_value"]) == ("5", "-2.000")  # numbers untouched
    assert stock["category"] == "comparison"
    assert rows[5]["category"] == "data_quality"


def test_csv_neutralizes_formulas_in_text_cells() -> None:
    hostile = Discrepancy(
        type=DiscrepancyType.MISSING_IN_ERP,
        key='=HYPERLINK("http://example.test")',
        source_of_truth=System.ERPNEXT,
        suggested_action="Check.",
    )
    rows = _parse(render_csv(_result(discrepancies=(hostile,))))

    assert rows[4]["key"] == '\'=HYPERLINK("http://example.test")'


def test_neutralize_formula_leaves_normal_text_alone() -> None:
    assert neutralize_formula("SKU-1") == "SKU-1"
    assert neutralize_formula("-1") == "'-1"
    assert neutralize_formula("@SUM(A1)") == "'@SUM(A1)"
