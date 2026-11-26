"""Versioned API bindings. Public snapshots contain references, never credentials.

Protocols describe real wire formats, not an interchangeable model fallback. A
batch can pin a snapshot while credentials rotate through their existing keys.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .audit import structural
from .base import ProviderContractError, ProviderExternalError, provider_config_hash
from .video_http_config import H3_PROTOCOL, HTTP_VIDEO_PROTOCOL, VideoHTTPConfig

APIKind = Literal["text", "image", "video"]
Protocol = Literal[
    "native-responses-v1",
    "images-json-v1",
    "images-gemini-native-v1",
    "h3-ref2va-v1",
    "video-http-v1",
]
PROTOCOL_KINDS: dict[str, str] = {
    "native-responses-v1": "text",
    "images-json-v1": "image",
    "images-gemini-native-v1": "image",
    H3_PROTOCOL: "video",
    HTTP_VIDEO_PROTOCOL: "video",
}


def normalized_url(value: str) -> str:
    parts = urlsplit(value if "://" in value else "http://" + value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ProviderContractError("API base URL must be HTTP(S), without embedded secrets")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        raise ProviderContractError("API base URL has an invalid port") from None
    netloc = host + (
        f":{port}" if port and port != {"http": 80, "https": 443}[parts.scheme] else ""
    )
    return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), "", ""))


def wire_identity(*, kind: str, protocol: str, base_url: str) -> str:
    return provider_config_hash(
        {"kind": kind, "protocol": protocol, "base_url": normalized_url(base_url)}
    )


def route_physical_identity(route: Mapping[str, Any]) -> tuple[str, str, str]:
    """Model/route aliases never identify an additional physical GPU pool."""
    wire = route.get("wire_hash") or wire_identity(
        kind="video",
        protocol=str(route.get("protocol", H3_PROTOCOL)),
        base_url=str(route.get("base_url", "http://unresolved.invalid")),
    )
    deployment = route.get("deployment_hash") or provider_config_hash(
        {
            "registry_version": route.get("registry_version", "unknown"),
            "revision": route.get("revision", "unknown"),
            "topology": route.get("topology", "unknown"),
        }
    )
    return (
        str(route.get("resource_group", route.get("service_id", route.get("id", "unresolved")))),
        str(wire),
        str(deployment),
    )


class APIService(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(min_length=1)
    kind: APIKind
    protocol: Protocol
    base_url: str = Field(repr=False)
    credential_ref: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    models: list[str] = Field(min_length=1)
    resource_group: str = ""
    capacity: int | None = Field(default=None, ge=1)
    version: str = "unknown"
    capabilities: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=600, gt=0)
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def default_protocol(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("kind") == "video" and "protocol" not in value:
            return {**value, "protocol": H3_PROTOCOL}
        return value

    @model_validator(mode="after")
    def valid_protocol(self) -> Self:
        if PROTOCOL_KINDS[self.protocol] != self.kind:
            raise ValueError("API kind and protocol do not match")
        if any(not m.strip() for m in self.models) or len(set(self.models)) != len(self.models):
            raise ValueError("API models must be unique nonempty exact names")
        if self.kind == "video" and self.capacity is None:
            raise ValueError("Video services require an explicit concurrency limit")
        # Capabilities are structural; credentials, private URIs and wire bodies
        # must stay outside registry snapshots.
        reserved = {
            "id",
            "label",
            "kind",
            "protocol",
            "base_url",
            "credential_ref",
            "models",
            "model",
            "capacity",
            "capacity_limit",
            "resource_group",
            "registry_version",
            "service_id",
            "timeout_seconds",
        }
        if reserved.intersection(self.capabilities):
            raise ValueError("Capabilities cannot override the service binding")
        structural(self.capabilities)
        if self.protocol == HTTP_VIDEO_PROTOCOL:
            VideoHTTPConfig.model_validate(self.capabilities.get("video_http"))
            if self.capabilities.keys() - {"video_http", "prior_seconds_per_second"}:
                raise ValueError("Video HTTP capabilities cannot override the configured contract")
        elif "video_http" in self.capabilities:
            raise ValueError("video_http is only valid for video-http-v1")
        object.__setattr__(self, "base_url", normalized_url(self.base_url))
        if not self.resource_group:
            object.__setattr__(
                self,
                "resource_group",
                "api-" + provider_config_hash({"kind": self.kind, "url": self.base_url})[:20],
            )
        return self

    def binding(self, model: str) -> dict[str, Any]:
        if model not in self.models:
            raise ProviderContractError("Requested model is not registered on this service")
        return {**self.model_dump(mode="json"), "model": model}

    @property
    def video_http(self) -> VideoHTTPConfig | None:
        if self.protocol != HTTP_VIDEO_PROTOCOL:
            return None
        return VideoHTTPConfig.model_validate(self.capabilities["video_http"])

    def video_route(self, model: str) -> dict[str, Any]:
        if self.kind != "video":
            raise ProviderContractError("Only a video binding is a video route")
        self.binding(model)
        return {
            **self.capabilities,
            **({"protocol": self.protocol} if self.protocol != H3_PROTOCOL else {}),
            "id": self.route_id(model),
            "service_id": self.id,
            "label": self.label,
            "base_url": self.base_url,
            "capacity_limit": self.capacity,
            "resource_group": self.resource_group,
            "model": model,
            "credential_ref": self.credential_ref,
            "registry_version": self.version,
            "timeout_seconds": self.timeout_seconds,
        }

    def h3_route(self, model: str) -> dict[str, Any]:
        if self.protocol != H3_PROTOCOL:
            raise ProviderContractError("Only an H3 binding is an H3 video route")
        return self.video_route(model)

    def route_id(self, model: str) -> str:
        # Preserve legacy primary-model route IDs. Extra models use separate
        # capability/sample identities, but never add physical capacity.
        return (
            self.id
            if model == self.models[0]
            else self.id + "-" + hashlib.sha256(model.encode()).hexdigest()[:12]
        )


class APIRegistrySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["agentvideo-api-registry-v1"] = "agentvideo-api-registry-v1"
    services: list[APIService] = Field(default_factory=list)


class APIRegistry:
    def __init__(self, spec: APIRegistrySpec, credentials: Mapping[str, str]) -> None:
        self.credentials = dict(credentials)
        services: dict[str, APIService] = {}
        aliases: dict[str, str] = {}
        wires: dict[tuple[str, str, str], str] = {}
        for service in spec.services:
            if service.id in aliases:
                raise ProviderContractError("Duplicate API service identifier")
            wire = service.kind, service.protocol, service.base_url
            previous_id = wires.get(wire)
            if previous_id:
                previous = services[previous_id]
                if previous.resource_group != service.resource_group:
                    raise ProviderContractError("Aliases of one API must use one resource group")
                if previous.credential_ref != service.credential_ref:
                    raise ProviderContractError("API aliases cannot have ambiguous credential refs")
                if (
                    previous.version != service.version
                    or previous.capabilities != service.capabilities
                    or previous.enabled != service.enabled
                    or previous.timeout_seconds != service.timeout_seconds
                ):
                    raise ProviderContractError("API aliases disagree on version/capabilities")
                capacity = [c for c in (previous.capacity, service.capacity) if c is not None]
                services[previous_id] = previous.model_copy(
                    update={
                        "capacity": min(capacity) if capacity else None,
                        "models": list(dict.fromkeys([*previous.models, *service.models])),
                    }
                )
                aliases[service.id] = previous_id
            else:
                services[service.id] = service
                aliases[service.id] = service.id
                wires[wire] = service.id
        self.services, self.aliases = services, aliases

    @classmethod
    def load(cls, config: Mapping[str, str], path: Path | None = None) -> APIRegistry:
        configured_path = path or (
            Path(config["AGENTVIDEO_API_REGISTRY_PATH"])
            if config.get("AGENTVIDEO_API_REGISTRY_PATH")
            else None
        )
        try:
            if configured_path:
                spec = APIRegistrySpec.model_validate_json(configured_path.read_bytes())
            elif config.get("AGENTVIDEO_API_REGISTRY_JSON"):
                spec = APIRegistrySpec.model_validate_json(config["AGENTVIDEO_API_REGISTRY_JSON"])
            else:
                spec = APIRegistrySpec()
        except (OSError, ValidationError, json.JSONDecodeError):
            # ValidationError can embed the submitted configuration: never log it.
            raise ProviderContractError(
                "API registry cannot be read or violates its schema"
            ) from None
        return cls(spec, config)

    def snapshot(self, groups: list[str] | None = None) -> dict[str, Any]:
        selected = [
            s.model_dump(mode="json")
            for s in self.services.values()
            if s.enabled and (groups is None or s.resource_group in groups)
        ]
        spec = {"version": "agentvideo-api-registry-v1", "services": selected}
        return {**spec, "sha256": provider_config_hash(spec)}

    def pinned(self, snapshot: Mapping[str, Any]) -> APIRegistry:
        raw = {k: v for k, v in snapshot.items() if k != "sha256"}
        if snapshot.get("sha256") != provider_config_hash(raw):
            raise ProviderContractError("Pinned API snapshot hash does not match")
        try:
            spec = APIRegistrySpec.model_validate(raw)
        except ValidationError:
            raise ProviderContractError("Pinned API snapshot has invalid bindings") from None
        return APIRegistry(spec, self.credentials)

    def fixed_service(self, kind: APIKind, *, required: bool = False) -> APIService | None:
        """Select the sole explicitly enabled service of this kind."""
        services = [s for s in self.services.values() if s.enabled and s.kind == kind]
        if len(services) > 1 or (required and not services):
            raise ProviderContractError(f"Configure exactly one enabled {kind} service")
        return services[0] if services else None

    def fixed_media(
        self, kind: Literal["image", "video"], model: str | None = None, *, required: bool = False
    ) -> APIService | None:
        service = self.fixed_service(kind, required=required)
        if service is None:
            return None
        if len(service.models) != 1:
            raise ProviderContractError(f"Configure exactly one model for the {kind} service")
        if model is not None and model != service.models[0]:
            raise ProviderContractError(f"The project model differs from the fixed {kind} model")
        return service

    def candidates(self, kind: APIKind, model: str) -> list[APIService]:
        return [
            s for s in self.services.values() if s.enabled and s.kind == kind and model in s.models
        ]

    def resolve(self, kind: APIKind, model: str, service_id: str | None = None) -> APIService:
        candidates = self.candidates(kind, model)
        if service_id:
            canonical = self.aliases.get(service_id, service_id)
            candidates = [s for s in candidates if s.id == canonical]
        if not candidates:
            raise ProviderContractError(
                "No configured protocol binding for the exact requested model"
            )
        return candidates[0]

    @staticmethod
    def credential_optional(service: APIService) -> bool:
        config = service.video_http
        return service.kind == "video" and (config is None or not config.auth_required)

    def credential(self, service: APIService) -> str:
        return self.credential_ref(
            service.credential_ref, optional=self.credential_optional(service)
        )

    def credential_ref(self, reference: str, *, optional: bool = False) -> str:
        value = os.environ.get(reference) or self.credentials.get(reference)
        if not value and not optional:
            raise ProviderExternalError("Configured API credential reference is unavailable")
        return value or ""

    def validate_models(
        self, models: Mapping[str, str], *, additional_text_models: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        """Configuration checks only. These are not online readiness/probe results."""
        results = []
        requested = dict.fromkeys([*models.items(), *(("text", m) for m in additional_text_models)])
        for kind, model in requested:
            candidates = self.candidates(kind, model)  # type: ignore[arg-type]
            results.append(
                {
                    "kind": kind,
                    "model": model,
                    "configured": bool(candidates),
                    "credential_available": any(
                        self.credential_optional(s)
                        or bool(
                            os.environ.get(s.credential_ref)
                            or self.credentials.get(s.credential_ref)
                        )
                        for s in candidates
                    ),
                    "service_ids": [s.id for s in candidates],
                    "live_verified": False,
                }
            )
        return results
