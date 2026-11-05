"""Settings loaded from environment variables (.env).

Secrets are SecretStr, so they print as '**********' in reprs, logs and dumps. Validation
errors are re-raised without the offending input values, so a mistyped secret never ends up
in an error message either.
"""

from datetime import datetime
from pathlib import Path
from typing import Annotated

from pydantic import Field, HttpUrl, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from reconciler.models import TimezoneName
from reconciler.reconcile import (
    MAX_ORDER_LOOKBACK_DAYS,
    ConfigurationError,
    ReconcileOptions,
)

CommaSeparated = Annotated[frozenset[str], NoDecode]
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Shopify
    shopify_store_domain: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
    shopify_admin_token: SecretStr
    shopify_api_version: str = Field(pattern=r"^\d{4}-(01|04|07|10)$")
    shopify_location_id: str = Field(pattern=r"^gid://shopify/Location/\d+$")

    # ERPNext
    erpnext_url: HttpUrl
    erpnext_api_key: SecretStr
    erpnext_api_secret: SecretStr
    erpnext_price_list: str = Field(min_length=1)
    erpnext_warehouse: str = Field(min_length=1)
    erpnext_item_groups: CommaSeparated = Field(min_length=1)
    erpnext_unit_uoms: CommaSeparated = Field(min_length=1)
    erpnext_prices_include_tax: bool | None = None
    erpnext_order_id_field: str | None = Field(default=None, pattern=r"^[a-z_][a-z0-9_]*$")
    erpnext_timezone: TimezoneName
    """Must match the ERPNext site timezone (System Settings)."""
    erpnext_database: str
    """Database behind the ERPNext site. Only MariaDB is supported (see _mariadb_only)."""

    # Reconciliation
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    order_lookback_days: int = Field(default=30, gt=0, le=MAX_ORDER_LOOKBACK_DAYS)

    @field_validator("erpnext_item_groups", "erpnext_unit_uoms", mode="before")
    @classmethod
    def _split_commas(cls, value: object) -> object:
        if isinstance(value, str):
            return frozenset(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("erpnext_url")
    @classmethod
    def _https_unless_local(cls, url: HttpUrl) -> HttpUrl:
        """API keys travel in a header on every request, so plain HTTP is only for this machine."""
        if url.scheme != "https" and url.host not in LOCAL_HOSTS:
            raise ValueError("must use https:// unless the host is localhost")
        return url

    @field_validator("erpnext_database")
    @classmethod
    def _mariadb_only(cls, value: str) -> str:
        """Price precedence is reproduced for MariaDB, which sorts NULL last in DESC order.
        PostgreSQL sorts NULL first, so ERPNext would pick different prices there; rather than
        silently comparing against the wrong price, any other database is refused. The read-only
        API cannot reveal the database type, so the operator must state it."""
        if value.strip().lower() != "mariadb":
            raise ValueError(
                "only 'mariadb' is supported; ERPNext price selection differs on PostgreSQL"
            )
        return "mariadb"

    @field_validator("erpnext_prices_include_tax", "erpnext_order_id_field", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    def to_options(self, as_of: datetime) -> ReconcileOptions:
        return ReconcileOptions(
            as_of=as_of,
            price_timezone=self.erpnext_timezone,
            item_groups=self.erpnext_item_groups,
            price_list=self.erpnext_price_list,
            currency=self.currency,
            erp_prices_include_tax=self.erpnext_prices_include_tax,
            warehouse=self.erpnext_warehouse,
            unit_uoms=self.erpnext_unit_uoms,
            order_lookback_days=self.order_lookback_days,
        )


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    """Load settings, turning validation errors into a ConfigurationError without values."""
    try:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as error:
        problems = [
            f"{'.'.join(str(part) for part in issue['loc']).upper()}: {issue['msg']}"
            for issue in error.errors(include_input=False, include_url=False)
        ]
        raise ConfigurationError("Invalid configuration:\n  " + "\n  ".join(problems)) from None
