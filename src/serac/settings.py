"""Runtime settings read from the environment and an optional `.env` file.

Every variable is documented in `docs/CREDENTIALS.md` and mirrored in `.env.example`.
No setting is required for the offline test suite.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SeracSettings(BaseSettings):
    """Process-wide configuration. Secrets are `SecretStr` so they never print."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    earthdata_token: SecretStr | None = None
    earthdata_username: SecretStr | None = None
    earthdata_password: SecretStr | None = None
    cdse_client_id: SecretStr | None = None
    cdse_client_secret: SecretStr | None = None
    cdsapi_url: str = "https://cds.climate.copernicus.eu/api"
    cdsapi_key: SecretStr | None = None
    gacos_email: str | None = None

    serac_redis_url: str = "redis://localhost:6379/0"
    serac_seedlink_server: str = "geofon.gfz.de:18000"

    dvc_remote_url: str | None = None

    serac_data_dir: Path = Path("data")
    serac_reports_dir: Path = Path("reports")
    serac_online: bool = False


@lru_cache(maxsize=1)
def get_settings() -> SeracSettings:
    """Return the cached settings instance."""
    return SeracSettings()
