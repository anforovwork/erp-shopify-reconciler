"""`reconcile run` end to end with fake fetchers. No HTTP request is made in this file."""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from builders import AS_OF, item, price, stock_bin, variant
from test_config import FAKE_CLIENT_SECRET, FAKE_SECRET, VALID_ENV

from reconciler import cli
from reconciler.config import Settings
from reconciler.models import ErpData, ShopifyData, ShopifyShop
from reconciler.reconcile import ConfigurationError
from reconciler.report import render_csv, render_markdown
from reconciler.snapshot import load_snapshot, snapshot_path
from reconciler.sources.http import IncompleteResponse, ReadOnlyViolation

SNAPSHOT_NAME = "snapshot-20261101T120000Z.json"
SECRETS = (FAKE_CLIENT_SECRET, FAKE_SECRET, VALID_ENV["ERPNEXT_API_KEY"])


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name, value in VALID_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ERPNEXT_PRICES_INCLUDE_TAX", "false")
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _shopify(*variants: object, currency: str = "CAD") -> ShopifyData:
    return ShopifyData(
        fetched_at=AS_OF - timedelta(minutes=2),
        shop=ShopifyShop(currency=currency, taxes_included=False),
        variants=tuple(variants) or (variant("OK-1"),),
        orders=(),
    )


def _erp() -> ErpData:
    return ErpData(
        fetched_at=AS_OF - timedelta(minutes=1),
        price_list_currency="CAD",
        items=(item("OK-1"),),
        prices=(price("OK-1"),),
        bins=(stock_bin("OK-1"),),
        warehouse_confirmed=True,
        bins_complete=True,
        sales_orders=None,
    )


def _run(settings: Settings, tmp_path: Path, shopify: ShopifyData, erp_fetcher=None) -> int:
    return cli.run_command(
        settings,
        fetch_erp=erp_fetcher or (lambda _s, _t: _erp()),
        fetch_shopify=lambda _s, _t: shopify,
        report_path=tmp_path / "reports" / "latest.md",
        csv_path=tmp_path / "reports" / "latest.csv",
        now=lambda: AS_OF,
    )


def _written(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in tmp_path.rglob("*") if p.is_file())


def test_clean_run_exits_0_and_writes_all_outputs(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(settings, tmp_path, _shopify()) == cli.EXIT_OK

    assert _written(tmp_path) == ["latest.csv", "latest.md", SNAPSHOT_NAME]
    modes = {p.stat().st_mode & 0o777 for p in (tmp_path / "reports").iterdir()}
    assert modes == {0o600}  # store data: owner only
    out = capsys.readouterr().out
    assert "discrepancies:        0" in out
    assert "skipped checks:       1" in out  # order check: mapping field unavailable


def test_discrepancies_exit_1_with_counts_but_no_store_text(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shopify = _shopify(
        variant("OK-1", 1, price=Decimal("12.00"), on_hand=9),
        variant("SECRET-LOOKING-SKU", 2),
    )

    assert _run(settings, tmp_path, shopify) == cli.EXIT_DISCREPANCIES

    out = capsys.readouterr().out
    assert "PRICE_MISMATCH: 1" in out
    assert "STOCK_MISMATCH: 1" in out
    assert "MISSING_IN_ERP: 1" in out
    assert "comparison: 3" in out
    assert "data quality: 0" in out
    assert "OK-1" not in out
    assert "SECRET-LOOKING-SKU" not in out
    assert "SECRET-LOOKING-SKU" in (tmp_path / "reports" / "latest.md").read_text()


@pytest.mark.parametrize(
    "error",
    [
        IncompleteResponse("ERPNext Bin response invalid in ['actual_qty']"),
        ConfigurationError("warehouse not found"),
        ReadOnlyViolation("blocked"),
        PermissionError(13, "Permission denied", "reports"),
    ],
)
def test_operational_errors_exit_2_and_write_nothing(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    def failing(_s: Settings, _t: datetime) -> ErpData:
        raise error

    assert _run(settings, tmp_path, _shopify(), failing) == cli.EXIT_ERROR

    assert _written(tmp_path) == []
    assert capsys.readouterr().err.startswith(f"ERROR: {type(error).__name__}: ")


def test_currency_mismatch_exits_2_and_writes_nothing(settings: Settings, tmp_path: Path) -> None:
    assert _run(settings, tmp_path, _shopify(currency="USD")) == cli.EXIT_ERROR
    assert _written(tmp_path) == []


def test_snapshot_round_trip_renders_the_same_reports(settings: Settings, tmp_path: Path) -> None:
    shopify = _shopify(variant("OK-1", price=Decimal("10.015")))
    _run(settings, tmp_path, shopify)

    snapshot = load_snapshot(tmp_path / "reports" / SNAPSHOT_NAME)

    assert snapshot.shopify == shopify
    assert snapshot.erpnext == _erp()
    assert snapshot.options == settings.to_options(AS_OF)
    assert snapshot.shopify.variants[0].price == Decimal("10.015")
    reports = tmp_path / "reports"
    assert render_markdown(snapshot.result) == (reports / "latest.md").read_text()
    assert render_csv(snapshot.result) == (reports / "latest.csv").read_text()


def test_existing_snapshot_is_never_overwritten(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(settings, tmp_path, _shopify()) == cli.EXIT_OK
    snapshot = tmp_path / "reports" / SNAPSHOT_NAME
    before = snapshot.read_bytes()
    report_before = (tmp_path / "reports" / "latest.md").read_bytes()

    second = _shopify(variant("OK-1", price=Decimal("99.00")))
    assert _run(settings, tmp_path, second) == cli.EXIT_ERROR

    assert snapshot.read_bytes() == before
    assert (tmp_path / "reports" / "latest.md").read_bytes() == report_before
    assert "FileExistsError" in capsys.readouterr().err
    assert _written(tmp_path) == ["latest.csv", "latest.md", SNAPSHOT_NAME]  # no temp files


def test_snapshot_name_is_utc_timestamp(tmp_path: Path) -> None:
    edmonton = datetime.fromisoformat("2026-11-01T05:00:00-07:00")
    assert snapshot_path(tmp_path, edmonton).name == SNAPSHOT_NAME


def test_no_secret_in_terminal_or_files(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(settings, tmp_path, _shopify(variant("OK-1", price=Decimal("1"))))

    captured = capsys.readouterr()
    texts = [captured.out, captured.err] + [
        p.read_text() for p in tmp_path.rglob("*") if p.is_file()
    ]
    for secret in SECRETS:
        assert all(secret not in text for text in texts)


def test_main_with_invalid_settings_exits_2_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    for name in VALID_ENV:
        monkeypatch.delenv(name, raising=False)

    def no_network(*_args: object) -> None:
        raise AssertionError("must not fetch with invalid settings")

    monkeypatch.setattr(cli, "fetch_erp_live", no_network)
    monkeypatch.setattr(cli, "fetch_shopify_live", no_network)

    assert cli.main(["run"]) == cli.EXIT_ERROR
    assert "ERROR: ConfigurationError: Invalid configuration" in capsys.readouterr().err
    assert _written(tmp_path) == []


def test_main_without_command_prints_help_and_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main([]) == cli.EXIT_ERROR
    assert "run" in capsys.readouterr().err
