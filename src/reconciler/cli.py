"""Command-line entry point.

`reconcile run` fetches live data (read-only), reconciles it, writes the Markdown and CSV
reports and saves a JSON snapshot next to them. The terminal summary shows only counts,
timestamps and paths: SKUs, order ids and other text from the stores appear only in the
escaped reports.

Exit codes: 0 no discrepancies, 1 discrepancies found, 2 operational or configuration error.
"""

import argparse
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reconciler import __version__
from reconciler.config import Settings, load_settings
from reconciler.models import (
    DiscrepancyCategory,
    DiscrepancyType,
    ErpData,
    ReconciliationResult,
    ShopifyData,
)
from reconciler.reconcile import ConfigurationError, reconcile
from reconciler.report import render_csv, render_markdown
from reconciler.snapshot import Snapshot, save_snapshot, snapshot_path, write_text_atomic
from reconciler.sources.erpnext import ErpNextScope, fetch_erp_data
from reconciler.sources.http import ReadOnlyViolation, SourceError, erpnext_client, shopify_client
from reconciler.sources.shopify import ShopifyScope, fetch_shopify_data

EXIT_OK = 0
EXIT_DISCREPANCIES = 1
EXIT_ERROR = 2

DEFAULT_REPORT = Path("reports/latest.md")
DEFAULT_CSV = Path("reports/latest.csv")

ErpFetcher = Callable[[Settings, datetime], ErpData]
ShopifyFetcher = Callable[[Settings, datetime], ShopifyData]
OPERATIONAL_ERRORS = (ConfigurationError, SourceError, ReadOnlyViolation, OSError)


# --- Live fetchers (the only code here that talks to the network) ----------------------------


def fetch_erp_live(settings: Settings, as_of: datetime) -> ErpData:
    scope = ErpNextScope(
        price_list=settings.erpnext_price_list,
        warehouse=settings.erpnext_warehouse,
        order_id_field=settings.erpnext_order_id_field,
    )
    client = erpnext_client(
        str(settings.erpnext_url), settings.erpnext_api_key, settings.erpnext_api_secret
    )
    with client:
        return fetch_erp_data(client, scope)


def fetch_shopify_live(settings: Settings, as_of: datetime) -> ShopifyData:
    scope = ShopifyScope(
        location_id=settings.shopify_location_id,
        order_since=as_of - timedelta(days=settings.order_lookback_days),
    )
    graphql = shopify_client(
        settings.shopify_store_domain,
        settings.shopify_client_id,
        settings.shopify_client_secret,
        settings.shopify_api_version,
    )
    with graphql:
        return fetch_shopify_data(graphql, scope)


# --- reconcile run ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputPaths:
    report: Path
    csv: Path
    snapshot: Path


def run_command(
    settings: Settings,
    *,
    fetch_erp: ErpFetcher,
    fetch_shopify: ShopifyFetcher,
    report_path: Path,
    csv_path: Path,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Fetch, reconcile, write outputs. Nothing is written unless every step before succeeds."""
    try:
        as_of = now()
        options = settings.to_options(as_of)
        erp = fetch_erp(settings, as_of)
        shopify = fetch_shopify(settings, as_of)
        result = reconcile(shopify, erp, options)
        snapshot = Snapshot(options=options, shopify=shopify, erpnext=erp, result=result)
        paths = OutputPaths(report_path, csv_path, snapshot_path(report_path.parent, as_of))
        write_outputs(snapshot, paths)
    except OPERATIONAL_ERRORS as error:
        print_error(error)
        return EXIT_ERROR
    print_summary(result, paths)
    return EXIT_DISCREPANCIES if result.has_discrepancies else EXIT_OK


def write_outputs(snapshot: Snapshot, paths: OutputPaths) -> None:
    # The snapshot goes first: it refuses to overwrite, so a clash stops the run before the
    # latest.* reports are replaced.
    save_snapshot(snapshot, paths.snapshot)
    write_text_atomic(paths.report, render_markdown(snapshot.result), overwrite=True)
    write_text_atomic(paths.csv, render_csv(snapshot.result), overwrite=True)


def print_error(error: Exception) -> None:
    """Client and config errors name fields, never values; OSError names a path and a reason."""
    print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)


def print_summary(result: ReconciliationResult, paths: OutputPaths) -> None:
    counts = Counter(d.type for d in result.discrepancies)
    lines = [
        "Reconciliation finished",
        f"  generated at:         {result.generated_at.isoformat()}",
        f"  Shopify fetched at:   {result.shopify_fetched_at.isoformat()}",
        f"  ERPNext fetched at:   {result.erpnext_fetched_at.isoformat()}",
        f"  discrepancies:        {len(result.discrepancies)}",
        *_category_lines(counts, DiscrepancyCategory.COMPARISON, "comparison"),
        *_category_lines(counts, DiscrepancyCategory.DATA_QUALITY, "data quality"),
        f"  skipped checks:       {len(result.skipped)}",
        f"  warnings:             {len(result.warnings)}",
        f"  report:               {paths.report}",
        f"  csv:                  {paths.csv}",
        f"  snapshot:             {paths.snapshot}",
    ]
    print("\n".join(lines))


def _category_lines(
    counts: Counter[DiscrepancyType], category: DiscrepancyCategory, label: str
) -> list[str]:
    types = [t for t in DiscrepancyType if t.category is category and counts[t]]
    total = sum(counts[t] for t in types)
    return [f"    {label}: {total}", *(f"      {t.value}: {counts[t]}" for t in types)]


# --- Entry point -----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reconcile",
        description="Read-only reconciliation between Shopify and ERPNext.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="fetch live data, reconcile, write reports + snapshot")
    run.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="Markdown report path")
    run.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="CSV report path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help(sys.stderr)
        return EXIT_ERROR
    try:
        settings = load_settings()
    except ConfigurationError as error:
        print_error(error)
        return EXIT_ERROR
    return run_command(
        settings,
        fetch_erp=fetch_erp_live,
        fetch_shopify=fetch_shopify_live,
        report_path=args.report,
        csv_path=args.csv,
    )


if __name__ == "__main__":
    sys.exit(main())
