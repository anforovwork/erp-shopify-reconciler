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

from reconciler.reconcile import ConfigurationError, ReconcileOptions

CommaSeparated = Annotated[frozenset[str], NoDecode]


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

    # Reconciliation
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    order_lookback_days: int = Field(default=30, gt=0)

    @field_validator("erpnext_item_groups", "erpnext_unit_uoms", mode="before")
    @classmethod
    def _split_commas(cls, value: object) -> object:
        if isinstance(value, str):
            return frozenset(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("erpnext_prices_include_tax", "erpnext_order_id_field", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    def to_options(self, as_of: datetime) -> ReconcileOptions:
        return ReconcileOptions(
            as_of=as_of,
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
