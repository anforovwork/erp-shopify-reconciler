from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from reconciler.config import Settings, load_settings
from reconciler.reconcile import ConfigurationError

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"
FAKE_TOKEN = "shpat_FAKE0000000000000000000000"  # noqa: S105 - fake test value
FAKE_SECRET = "FAKE-ERP-SECRET-0000"  # noqa: S105 - fake test value

VALID_ENV = {
    "SHOPIFY_STORE_DOMAIN": "test-store.myshopify.com",
    "SHOPIFY_ADMIN_TOKEN": FAKE_TOKEN,
    "SHOPIFY_API_VERSION": "2026-07",
    "SHOPIFY_LOCATION_ID": "gid://shopify/Location/123",
    "ERPNEXT_URL": "http://localhost:8080",
    "ERPNEXT_API_KEY": "FAKE-ERP-KEY",
    "ERPNEXT_API_SECRET": FAKE_SECRET,
    "ERPNEXT_PRICE_LIST": "Test Selling",
    "ERPNEXT_WAREHOUSE": "Test Stores - TC",
    "ERPNEXT_ITEM_GROUPS": "Test Products, Test Gifts ,",
    "ERPNEXT_UNIT_UOMS": "Nos",
    "CURRENCY": "CAD",
    "ERPNEXT_TIMEZONE": "America/Edmonton",
    "ERPNEXT_DATABASE": "mariadb",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Start from a clean environment so a developer's real variables never leak in."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for name, value in VALID_ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_valid_environment_loads(env: pytest.MonkeyPatch) -> None:
    settings = load_settings(env_file=None)

    assert settings.erpnext_item_groups == frozenset({"Test Products", "Test Gifts"})
    assert settings.erpnext_unit_uoms == frozenset({"Nos"})
    assert settings.erpnext_prices_include_tax is None
    assert settings.erpnext_order_id_field is None
    assert settings.order_lookback_days == 30
    assert isinstance(settings.shopify_admin_token, SecretStr)


def test_secrets_never_appear_in_repr_or_dump(env: pytest.MonkeyPatch) -> None:
    settings = load_settings(env_file=None)

    for text in (repr(settings), str(settings), settings.model_dump_json()):
        assert FAKE_TOKEN not in text
        assert FAKE_SECRET not in text


@pytest.mark.parametrize(
    ("name", "bad_value"),
    [
        ("SHOPIFY_STORE_DOMAIN", "https://test-store.myshopify.com"),
        ("SHOPIFY_API_VERSION", "2026-08"),
        ("SHOPIFY_LOCATION_ID", "123"),
        ("ERPNEXT_URL", "localhost"),
        ("ERPNEXT_ITEM_GROUPS", " , "),
        ("ERPNEXT_ORDER_ID_FIELD", "x; drop"),
        ("CURRENCY", "cad"),
        ("ORDER_LOOKBACK_DAYS", "0"),
        ("ORDER_LOOKBACK_DAYS", "61"),
        ("ERPNEXT_TIMEZONE", "Mars/Olympus_Mons"),
        ("ERPNEXT_TIMEZONE", ""),
        ("ERPNEXT_DATABASE", "postgres"),
        ("ERPNEXT_DATABASE", ""),
    ],
)
def test_invalid_values_are_configuration_errors(
    env: pytest.MonkeyPatch, name: str, bad_value: str
) -> None:
    env.setenv(name, bad_value)

    with pytest.raises(ConfigurationError, match=name):
        load_settings(env_file=None)


def test_postgresql_is_refused_with_a_reason(env: pytest.MonkeyPatch) -> None:
    env.setenv("ERPNEXT_DATABASE", "postgres")

    with pytest.raises(ConfigurationError, match="differs on PostgreSQL"):
        load_settings(env_file=None)


def test_database_setting_is_required(env: pytest.MonkeyPatch) -> None:
    env.delenv("ERPNEXT_DATABASE")

    with pytest.raises(ConfigurationError, match="ERPNEXT_DATABASE: Field required"):
        load_settings(env_file=None)


def test_missing_required_value_is_named(env: pytest.MonkeyPatch) -> None:
    env.delenv("SHOPIFY_ADMIN_TOKEN")

    with pytest.raises(ConfigurationError, match="SHOPIFY_ADMIN_TOKEN: Field required"):
        load_settings(env_file=None)


def test_error_message_never_contains_input_values(env: pytest.MonkeyPatch) -> None:
    env.setenv("SHOPIFY_STORE_DOMAIN", f"{FAKE_TOKEN}.example.com")  # secret pasted by mistake

    with pytest.raises(ConfigurationError) as error:
        load_settings(env_file=None)

    assert FAKE_TOKEN not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False), ("", None)])
def test_tax_flag(env: pytest.MonkeyPatch, raw: str, expected: bool | None) -> None:
    env.setenv("ERPNEXT_PRICES_INCLUDE_TAX", raw)

    assert load_settings(env_file=None).erpnext_prices_include_tax is expected


def test_order_field_name(env: pytest.MonkeyPatch) -> None:
    env.setenv("ERPNEXT_ORDER_ID_FIELD", "custom_shopify_order_gid")

    assert load_settings(env_file=None).erpnext_order_id_field == "custom_shopify_order_gid"


def test_settings_become_reconcile_options(env: pytest.MonkeyPatch) -> None:
    env.setenv("ERPNEXT_PRICES_INCLUDE_TAX", "false")
    as_of = datetime(2026, 11, 1, tzinfo=UTC)

    opts = load_settings(env_file=None).to_options(as_of)

    assert opts.as_of == as_of
    assert opts.item_groups == frozenset({"Test Products", "Test Gifts"})
    assert opts.price_list == "Test Selling"
    assert opts.warehouse == "Test Stores - TC"
    assert opts.erp_prices_include_tax is False
    assert opts.order_lookback_days == 30
    assert opts.price_timezone == "America/Edmonton"


def test_env_example_is_complete_and_loads(env: pytest.MonkeyPatch) -> None:
    """.env.example must name every setting and contain only valid (fake) placeholders."""
    for name in Settings.model_fields:
        env.delenv(name.upper(), raising=False)

    settings = load_settings(env_file=ENV_EXAMPLE)

    assert set(Settings.model_fields) <= _example_names()
    assert settings.shopify_store_domain == "your-dev-store.myshopify.com"


def _example_names() -> set[str]:
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    return {
        line.split("=", 1)[0].strip().lower()
        for line in lines
        if "=" in line and not line.lstrip().startswith("#")
    }
