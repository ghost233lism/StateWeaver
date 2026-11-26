"""Read provider configuration from the workspace dotenv and explicit credentials."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from stateweaver.providers.base import ProviderContractError
from stateweaver.providers.credentials import load_secret_config

_REPOSITORY_ENV_PATH = Path(__file__).resolve().parents[5] / ".env"


def load_media_config_with_diagnostics(
    env_path: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> tuple[dict[str, str], str | None]:
    """Process variables override dotenv, which overrides the explicitly selected file."""
    env_path = (env_path or _REPOSITORY_ENV_PATH).resolve()
    config = load_secret_config((env_path,), environ=environ)
    secret_path = config.get("AGENTVIDEO_SECRET_CONFIG_PATH")
    if not secret_path:
        return config, None
    secret = Path(secret_path)
    if not secret.is_absolute():
        secret = env_path.parent / secret
    try:
        explicit = load_secret_config((secret,), environ={}, required=True)
    except (OSError, ValueError):
        return config, "AGENTVIDEO_SECRET_CONFIG_PATH must name a readable UTF-8 file"
    return {**explicit, **config}, None


def load_media_config(
    env_path: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    config, error = load_media_config_with_diagnostics(env_path, environ=environ)
    if error:
        raise ProviderContractError(error)
    return config
