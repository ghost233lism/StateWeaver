"""Schema for a configurable asynchronous JSON video API."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .base import ProviderJobState

H3_PROTOCOL: Final = "h3-ref2va-v1"
HTTP_VIDEO_PROTOCOL: Final = "video-http-v1"
HTTP_VIDEO_PROVIDER: Final = "video-http"
INPUTS = frozenset(
    {
        "prompt",
        "model",
        "duration_seconds",
        "aspect_ratio",
        "short_edge",
        "seed",
        "images",
        "videos",
        "audio",
        "job_key",
    }
)
MEDIA_INPUTS = frozenset({"images", "videos", "audio"})
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")
_FIELD_PATH = r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$"
_ROUTE_PATH = re.compile(r"/[A-Za-z0-9_./{}-]*")


def template_values(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from template_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from template_values(item)


def render_template(value: Any, inputs: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: render_template(item, inputs) for key, item in value.items()}
    if isinstance(value, list):
        return [render_template(item, inputs) for item in value]
    if isinstance(value, str) and (match := _PLACEHOLDER.fullmatch(value)):
        return inputs[match[1]]
    return value


class VideoHTTPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    submit_path: str
    status_path: str
    content_path: str | None = None
    output_url_field: str | None = Field(default=None, pattern=_FIELD_PATH)
    download_hosts: list[str] = Field(default_factory=list)
    health_path: str | None = None
    edit_submit_path: str | None = None
    submit_id_field: str = Field(default="id", pattern=_FIELD_PATH)
    status_id_field: str | None = Field(default=None, pattern=_FIELD_PATH)
    state_field: str = Field(default="status", pattern=_FIELD_PATH)
    states: dict[str, ProviderJobState]
    request_body: dict[str, Any]
    reference_policy: Literal["references", "text-only"] = "references"
    auth_header: str = Field(default="Authorization", pattern=r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
    auth_prefix: str = Field(default="Bearer ", pattern=r"^[A-Za-z0-9_ -]{0,32}$")
    auth_required: bool = False

    @property
    def inputs(self) -> frozenset[str]:
        return frozenset(
            match[1]
            for value in template_values(self.request_body)
            if (match := _PLACEHOLDER.fullmatch(value))
        )

    @model_validator(mode="after")
    def valid_contract(self) -> Self:
        for path, needs_id in (
            (self.submit_path, False),
            (self.status_path, True),
            (self.content_path, True),
            (self.health_path, False),
            (self.edit_submit_path, False),
        ):
            if path is None:
                continue
            plain = path.replace("{id}", "")
            if (
                not _ROUTE_PATH.fullmatch(path)
                or path.startswith("//")
                or path.count("{id}") != int(needs_id)
                or "{" in plain
                or "}" in plain
                or any(part in {".", ".."} for part in path.split("/"))
            ):
                raise ValueError("Video HTTP endpoint paths must stay below the API base URL")
        if (self.content_path is None) == (self.output_url_field is None):
            raise ValueError("Configure exactly one content_path or output_url_field")
        if self.download_hosts and not self.output_url_field:
            raise ValueError("Download hosts are only used with an output URL field")
        try:
            for host in self.download_hosts:
                parts = urlsplit("https://" + host)
                if (
                    not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", host)
                    or not parts.hostname
                    or (parts.port is not None and parts.port <= 0)
                ):
                    raise ValueError()
        except ValueError:
            raise ValueError("Video download hosts must be DNS names with optional ports") from None
        if self.auth_header.lower() in {
            "host",
            "accept",
            "content-type",
            "content-length",
            "transfer-encoding",
            "connection",
            "idempotency-key",
            "proxy-authorization",
        }:
            raise ValueError("Video credential header conflicts with transport headers")
        if (
            "prompt" not in self.inputs
            or self.inputs - INPUTS
            or any(
                ("{{" in value or "}}" in value) and not _PLACEHOLDER.fullmatch(value)
                for value in template_values(self.request_body)
            )
        ):
            raise ValueError("Use complete, supported video template placeholders including prompt")
        if self.reference_policy == "text-only":
            if (self.inputs & MEDIA_INPUTS) or self.edit_submit_path:
                raise ValueError("Text-only mode cannot configure media inputs or video editing")
        elif "images" not in self.inputs:
            raise ValueError("Configure images for reference mode or explicitly select text-only")
        if self.edit_submit_path and "videos" not in self.inputs:
            raise ValueError("Video editing requires the videos placeholder")
        if not self.states or ProviderJobState.SUCCEEDED not in self.states.values():
            raise ValueError("Video HTTP state mapping must include a success state")
        return self
