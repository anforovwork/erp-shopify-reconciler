"""MCP server tests. They read snapshot fixtures only; no HTTP request is made in this file.

The fixture snapshot (tests/fixtures/snapshots/) was generated from tests/builders.py fake data.
"""

import ast
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from reconciler import mcp_server
from reconciler.mcp_server import (
    MAX_SKU_LENGTH,
    build_server,
    explain_sku,
    get_summary,
    latest_snapshot_path,
    list_discrepancies,
)
from reconciler.models import DiscrepancyType
from reconciler.snapshot import load_snapshot, save_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "snapshots" / "snapshot-20261101T120000Z.json"
GENERATED_AT = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
NOW = GENERATED_AT + timedelta(hours=2)


@pytest.fixture
def reports(tmp_path: Path) -> Path:
    directory = tmp_path / "reports"
    directory.mkdir()
    shutil.copy(FIXTURE, directory / FIXTURE.name)
    return directory


# --- Snapshot discovery ----------------------------------------------------------------------


def test_no_snapshot_tells_user_to_run(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="Run `reconcile run` first"):
        get_summary(tmp_path, NOW)


def test_missing_directory_tells_user_to_run(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="reconcile run"):
        list_discrepancies(tmp_path / "nope", NOW)


def test_newest_snapshot_wins_and_other_names_are_ignored(reports: Path) -> None:
    newer = load_snapshot(FIXTURE)
    newer_result = newer.result.model_copy(
        update={"generated_at": GENERATED_AT + timedelta(days=1), "discrepancies": ()}
    )
    save_snapshot(
        newer.model_copy(update={"result": newer_result}),
        reports / "snapshot-20261102T120000Z.json",
    )
    (reports / "snapshot-29991231T000000Z.json.tmp").write_text("x")
    (reports / "snapshot-latest.json").write_text("x")

    assert latest_snapshot_path(reports) == reports / "snapshot-20261102T120000Z.json"
    summary = get_summary(reports, NOW)
    assert summary.snapshot.snapshot_file == "snapshot-20261102T120000Z.json"
    assert sum(summary.counts.values()) == 0


def test_corrupt_snapshot_gives_clear_error_without_content(reports: Path) -> None:
    (reports / "snapshot-20261103T000000Z.json").write_text('{"secret-looking": "IGNORE ME"}')
    with pytest.raises(ToolError) as error:
        get_summary(reports, NOW)
    assert "snapshot-20261103T000000Z.json" in str(error.value)
    assert "IGNORE ME" not in str(error.value)


# --- get_summary -----------------------------------------------------------------------------


def test_summary_counts_and_timestamps(reports: Path) -> None:
    summary = get_summary(reports, NOW)

    assert summary.snapshot.generated_at == GENERATED_AT
    assert summary.snapshot.shopify_fetched_at == GENERATED_AT - timedelta(minutes=2)
    assert summary.snapshot.erpnext_fetched_at == GENERATED_AT - timedelta(minutes=1)
    assert summary.snapshot.age_minutes == 120
    assert "may be stale" in summary.snapshot.note
    assert set(summary.counts) == {t.value for t in DiscrepancyType}
    assert summary.counts["PRICE_MISMATCH"] == 1
    assert summary.counts["STOCK_MISMATCH"] == 1
    assert summary.counts["PRICE_MISSING"] == 0
    assert summary.comparison_total == 4
    assert summary.data_quality_total == 1
    assert summary.skipped_count == 1
    assert summary.warnings_count == 0


# --- list_discrepancies ----------------------------------------------------------------------


def test_list_all_discrepancies(reports: Path) -> None:
    listing = list_discrepancies(reports, NOW)
    assert listing.total == 5
    assert listing.truncated is False
    assert [d.type for d in listing.discrepancies] == [
        DiscrepancyType.MISSING_IN_ERP,
        DiscrepancyType.PRICE_MISMATCH,
        DiscrepancyType.STOCK_MISMATCH,
        DiscrepancyType.ORDER_NOT_IN_ERP,
        DiscrepancyType.SKU_DUPLICATE,
    ]


@pytest.mark.parametrize("wanted", ["STOCK_MISMATCH", "stock_mismatch", " Stock_Mismatch "])
def test_filter_by_type(reports: Path, wanted: str) -> None:
    listing = list_discrepancies(reports, NOW, wanted)
    assert listing.type_filter is DiscrepancyType.STOCK_MISMATCH
    assert [(d.key, d.shopify_value, d.erp_value) for d in listing.discrepancies] == [
        ("FAKE-STOCK-1", "3", "5")
    ]


def test_filter_with_no_matches_is_empty(reports: Path) -> None:
    listing = list_discrepancies(reports, NOW, "PRICE_AMBIGUOUS")
    assert listing.total == 0
    assert listing.discrepancies == []


def test_unknown_type_lists_valid_types_without_echoing_input(reports: Path) -> None:
    with pytest.raises(ToolError) as error:
        list_discrepancies(reports, NOW, "DROP_TABLES")
    message = str(error.value)
    assert all(t.value in message for t in DiscrepancyType)
    assert "DROP_TABLES" not in message


def test_list_is_truncated(reports: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "MAX_RESULTS", 2)
    listing = list_discrepancies(reports, NOW)
    assert listing.total == 5
    assert listing.truncated is True
    assert len(listing.discrepancies) == 2


def test_output_has_only_whitelisted_fields(reports: Path) -> None:
    item = list_discrepancies(reports, NOW, "SKU_DUPLICATE").discrepancies[0]
    assert set(item.model_dump()) == {
        "type",
        "category",
        "key",
        "shopify_value",
        "erp_value",
        "source_of_truth",
        "suggested_action",
    }
    assert "appears" not in item.model_dump_json()  # the `detail` text is not returned


# --- explain_sku -----------------------------------------------------------------------------


def test_explain_sku_with_issue(reports: Path) -> None:
    explanation = explain_sku(reports, NOW, "  FAKE-PRICE-1 ")
    assert explanation.sku == "FAKE-PRICE-1"
    assert explanation.status == "issues_found"
    [found] = explanation.discrepancies
    assert (found.type, found.shopify_value, found.erp_value) == (
        DiscrepancyType.PRICE_MISMATCH,
        "12.50",
        "10.00",
    )
    assert found.source_of_truth == "erpnext"
    assert explanation.skipped == []


def test_explain_sku_with_skipped_check(reports: Path) -> None:
    explanation = explain_sku(reports, NOW, "FAKE-UNTRACKED-1")
    assert explanation.status == "issues_found"
    assert explanation.discrepancies == []
    [skipped] = explanation.skipped
    assert skipped.check == "stock"
    assert "not tracked" in skipped.reason


def test_explain_sku_without_issues(reports: Path) -> None:
    explanation = explain_sku(reports, NOW, "FAKE-OK-1")
    assert explanation.status == "no_issues_found"
    assert "No issues found" in explanation.message


def test_explain_sku_is_case_sensitive(reports: Path) -> None:
    assert explain_sku(reports, NOW, "fake-price-1").status == "no_issues_found"


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "x" * (MAX_SKU_LENGTH + 1), "FAKE\n1", "FAKE\x001", "FAKE​1"],
)
def test_explain_sku_rejects_invalid_input(reports: Path, bad: str) -> None:
    with pytest.raises(ToolError, match="SKU must"):
        explain_sku(reports, NOW, bad)


def test_sku_of_maximum_length_is_accepted(reports: Path) -> None:
    sku = "x" * MAX_SKU_LENGTH
    assert explain_sku(reports, NOW, sku).status == "no_issues_found"


# --- Over the MCP protocol (in-process client, no network) -----------------------------------


@pytest.mark.anyio
async def test_tools_are_listed_read_only(reports: Path) -> None:
    async with Client(build_server(reports, now=lambda: NOW)) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == {"get_summary", "list_discrepancies", "explain_sku"}
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.open_world_hint is False
        assert "may be stale" in (tool.description or "")
        assert "untrusted" in (tool.description or "")


@pytest.mark.anyio
async def test_tool_call_returns_structured_content(reports: Path) -> None:
    async with Client(build_server(reports, now=lambda: NOW)) as client:
        result = await client.call_tool("list_discrepancies", {"type": "PRICE_MISMATCH"})
    assert result.is_error is False
    assert result.structured_content is not None
    [found] = result.structured_content["discrepancies"]
    assert found["key"] == "FAKE-PRICE-1"
    assert found["shopify_value"] == "12.50"


@pytest.mark.anyio
async def test_tool_error_reaches_client(tmp_path: Path) -> None:
    async with Client(build_server(tmp_path, now=lambda: NOW)) as client:
        result = await client.call_tool("get_summary", {})
    assert result.is_error is True
    text = json.dumps([c.model_dump() for c in result.content])
    assert "reconcile run" in text


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- Static safety ---------------------------------------------------------------------------


def test_server_cannot_reach_the_network_or_credentials() -> None:
    tree = ast.parse(Path(mcp_server.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = ("reconciler.sources", "reconciler.config", "reconciler.cli", "httpx")
    assert not [m for m in imported if m.startswith(forbidden)]
