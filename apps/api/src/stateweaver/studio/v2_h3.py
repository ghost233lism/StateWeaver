"""Validate the explicit H3 service binding and its shared media root."""

from pathlib import Path

from stateweaver.providers.base import ProviderContractError
from stateweaver.providers.registry import APIRegistry
from stateweaver.providers.video_http_config import H3_PROTOCOL

from .settings import StudioSettings


def h3_shared_root(route: dict[str, object]) -> Path:
    value = route.get("shared_media_root")
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ProviderContractError(
            "Configure the H3 registry capability shared_media_root as an absolute directory"
        )
    return Path(value)


def require_h3_shared_cas(settings: StudioSettings, registry: APIRegistry) -> None:
    video = registry.fixed_media("video")
    if video is None or video.protocol != H3_PROTOCOL:
        return
    root = h3_shared_root(video.capabilities).resolve()
    if not settings.cas_root.resolve().is_relative_to(root):
        raise ValueError(f"H3 requires the Studio CAS on the shared {root} mount")
