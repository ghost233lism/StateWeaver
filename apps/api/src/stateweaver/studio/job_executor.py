"""Execute persisted studio jobs without owning leases or mutating project state."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import math
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageDraw, ImageOps

from stateweaver.providers.audit import AuditedTransport
from stateweaver.providers.base import (
    AssetInput,
    ProviderContractError,
    ProviderExternalError,
    ProviderJobState,
    ProviderQueryUnavailable,
    provider_config_hash,
)
from stateweaver.providers.coordination import ProviderCapacityWait
from stateweaver.providers.registry import (
    APIRegistry,
    APIService,
    wire_identity,
)
from stateweaver.providers.studio_runtime import (
    ProviderRuntime,
    applied_latent_receipt,
    requested_latent_receipt,
)
from stateweaver.providers.video_http_config import (
    H3_PROTOCOL,
    HTTP_VIDEO_PROTOCOL,
    HTTP_VIDEO_PROVIDER,
    VideoHTTPConfig,
)
from stateweaver.storage.cas import CASIntegrityError, ContentAddressedStore
from stateweaver.studio_media import (
    H3Ref2VAAdapter,
    Image25Adapter,
    ImageRequest,
    Keyframe,
    Ref2VARequest,
    TimedReference,
    boundary_comparison,
    decode_video,
    extract_keyframes,
)
from stateweaver.studio_media.assets import view_prompt
from stateweaver.studio_media.evidence import SamplingInterval, VideoMetadata
from stateweaver.studio_media.excerpts import extract_media, extract_video_tail
from stateweaver.studio_media.h3 import H3_MODEL, H3CapabilityUnavailable
from stateweaver.studio_media.latent import ENDPOINT_ANCHOR, EndpointAnchor
from stateweaver.studio_media.reference_crop import (
    crop_reference,
    is_reference_board,
    validate_h3_image_aspect,
)
from stateweaver.studio_media.skills import load_media_skill
from stateweaver.studio_media.video_http import HTTPVideoAdapter

from .adjudication import actual_end_state
from .assembly import ordered_assembly_sets
from .contracts import Job, Media, StudioProject
from .v2_h3 import h3_shared_root
from .video_capacity_cache import VideoCapacityCache, VideoCapacityObservation


@dataclass(frozen=True, slots=True)
class JobOutcome:
    status: Literal["queued", "running", "complete", "failed"]
    provider_job_id: str | None = None
    media: list[Media] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)


class CompletedVideoDeliveryError(ProviderContractError):
    """The original H3 job finished, but its delivery does not meet the contract."""

    def __init__(self, detail: str, *, raw_media: Media | None = None) -> None:
        super().__init__(detail)
        self.raw_media = raw_media


class ProviderResultPublicationError(ProviderExternalError):
    """Keep a returned business result available for the independent local journal."""

    def __init__(self, outcome: JobOutcome) -> None:
        self.outcome = outcome
        super().__init__(
            "Returned API result requires durable journal reconciliation; do not resubmit"
        )


logger = logging.getLogger(__name__)


def require_video_media_owner(project_id: str, job: Job, media: Media) -> None:
    expected_id = hashlib.sha256(f"{job.id}:output:{media.sha256}".encode()).hexdigest()[:32]
    if (
        media.id != expected_id
        or media.owner_actor_id != job.actor_id
        or media.source_candidate_id != job.target_id
        or media.mime_type != "video/mp4"
        or media.url != f"/api/v1/studio/projects/{project_id}/media/{expected_id}"
    ):
        raise ProviderQueryUnavailable("Saved H3 original media ownership does not match")


def require_video_outcome(project_id: str, job: Job, outcome: JobOutcome) -> None:
    for media in outcome.media:
        require_video_media_owner(project_id, job, media)
    if outcome.status != "complete":
        return
    if not outcome.provider_job_id or job.provider_job_id not in {None, outcome.provider_job_id}:
        raise ProviderQueryUnavailable("Saved H3 outcome belongs to a different task")
    media_id = outcome.result.get("media_id")
    if not any(media.id == media_id for media in outcome.media):
        raise ProviderQueryUnavailable("Saved H3 delivery does not identify its output media")
    provenance = outcome.result.get("provenance", {})
    if not isinstance(provenance, dict):
        raise ProviderQueryUnavailable("Saved H3 delivery provenance is malformed")
    requested_latent = job.request.get("latent_continuation")
    try:
        if "requested_latent" in provenance and provenance[
            "requested_latent"
        ] != requested_latent_receipt(requested_latent):
            raise ProviderQueryUnavailable(
                "Saved H3 requested latent proof differs from its recipe"
            )
        latent_error = _video_latent_delivery_error(
            job, provenance.get("applied_latent_continuation"), invalid=False
        )
    except (KeyError, ProviderContractError):
        raise ProviderQueryUnavailable("Saved H3 latent recipe is malformed") from None
    if latent_error:
        raise ProviderQueryUnavailable(latent_error)
    if (
        "latent_continuation" in provenance
        and provenance["latent_continuation"] != requested_latent
    ):
        raise ProviderQueryUnavailable("Saved H3 requested latent proof differs from its recipe")


def require_same_video_outcome(local: JobOutcome, shared: JobOutcome) -> None:
    """Reject mismatched terminal receipts before restoring or adopting media."""
    if (
        local.status != shared.status
        or local.provider_job_id != shared.provider_job_id
        or local.result.get("media_id") != shared.result.get("media_id")
        or {media.id: media for media in local.media} != {media.id: media for media in shared.media}
    ):
        raise ProviderQueryUnavailable("Local and shared H3 terminal receipts differ")
    if local.status == "complete" and applied_latent_receipt(
        local.result.get("provenance", {}).get("applied_latent_continuation")
    ) != applied_latent_receipt(
        shared.result.get("provenance", {}).get("applied_latent_continuation")
    ):
        raise ProviderQueryUnavailable("Local and shared H3 terminal proofs differ")


def _video_latent_delivery_error(job: Job, observed: Any, *, invalid: bool) -> str | None:
    if invalid:
        return "H3 completed with an invalid latent receipt; preserve the original task ID"
    latent = job.request.get("latent_continuation", {"mode": "off"})
    if not isinstance(latent, dict):
        return "H3 saved latent recipe is malformed"
    proof = applied_latent_receipt(observed)
    if latent.get("mode") == ENDPOINT_ANCHOR:
        expected = EndpointAnchor.from_payload(latent).payload()
        if (
            not proof
            or proof.get("applied") is not True
            # Full-tail requests omit the binding; endpoint-only is a different mode.
            or proof.get("binding") != expected.get("binding")
            or proof.get("tail_frame_count", 72) != latent.get("tail_frame_count", 72)
            or any(proof.get(key) != value for key, value in expected.items())
        ):
            return (
                "H3 completed without the requested latent anchor receipt; "
                "preserve the original task ID"
            )
    elif (
        "latent_continuation" in job.request
        and latent.get("mode") == "off"
        and isinstance(observed, dict)
        and observed.get("applied") is True
    ):
        return "H3 applied latent anchoring despite the saved off request"
    elif observed and not proof:
        return "H3 completed with an invalid latent receipt; preserve the original task ID"
    return None


class JobExecutor:
    def __init__(
        self,
        cas: ContentAddressedStore,
        work_root: Path,
        config: Mapping[str, str],
        *,
        image_adapter: Image25Adapter | None = None,
        video_adapter: H3Ref2VAAdapter | None = None,
        media_scratch_root: Path | None = None,
        registry: APIRegistry | None = None,
        provider_runtime: ProviderRuntime | None = None,
    ) -> None:
        self.cas = cas
        self.work_root = work_root.resolve()
        self.media_scratch_root = media_scratch_root.resolve() if media_scratch_root else None
        self.config = dict(config)
        self.registry = registry or APIRegistry.load(config)
        self.provider_runtime = provider_runtime
        self.on_video_remote_completion: Callable[[str, Job], None] | None = None
        self._image_adapter = image_adapter
        self._video_adapter = video_adapter
        self._injected_video_adapter = video_adapter is not None
        # Explicit transport injection for legacy recovery/tests. Normal routes
        # are constructed per call so credential rotation is not cached.
        self._route_adapters: dict[str, H3Ref2VAAdapter | HTTPVideoAdapter] = {}
        self._route_profiles: dict[str, dict[str, str | int]] = {}
        self._capacity_by_binding: dict[str, int] = {}
        self.video_capacity_routes: dict[str, dict[str, Any]] = {}
        self.video_capacity_services: dict[str, APIService] = {}
        cache_root = (
            provider_runtime.coordinator.path.parent
            / (provider_runtime.coordinator.path.name + ".video-capacity")
            if provider_runtime
            else self.work_root / "video-capacity"
        )
        self._video_capacity_cache = VideoCapacityCache(cache_root)
        self.video_capacity_observations: dict[str, VideoCapacityObservation] = {}

    def execute(
        self, project: StudioProject, job: Job, *, local_video_outcome: JobOutcome | None = None
    ) -> JobOutcome:
        if job.kind in {"image", "video"}:
            try:
                if job.kind == "image":
                    outcome = self._image(project, job)
                elif local_video_outcome is not None:
                    outcome = self._video(project, job, local_outcome=local_video_outcome)
                else:
                    outcome = self._video(project, job)
            except CompletedVideoDeliveryError as error:
                if not self.provider_runtime:
                    raise
                outcome = JobOutcome(
                    "failed",
                    provider_job_id=job.provider_job_id,
                    media=[error.raw_media] if error.raw_media else [],
                    result={
                        "error": str(error),
                        "delivery_failure": {
                            "remote_state": "complete",
                            "category": "validation",
                            **({"raw_media_id": error.raw_media.id} if error.raw_media else {}),
                        },
                    },
                )
            except Exception as error:
                if self.provider_runtime:
                    try:
                        self.provider_runtime.failure(job, error)
                    except Exception as journal_error:
                        logger.error(
                            "API failure journal deferred job=%s type=%s",
                            job.id,
                            type(journal_error).__name__,
                        )
                raise
            if self.provider_runtime:
                try:
                    self.provider_runtime.outcome(job, outcome)
                except Exception as error:
                    raise ProviderResultPublicationError(outcome) from error
            return outcome
        if job.kind == "analysis":
            return self._analyze(project, job)
        if job.kind == "assembly":
            return self._assemble(project, job)
        if job.kind == "reference":
            return self._prepare_reference(project, job)
        raise ProviderContractError("unknown studio job kind")

    def _images(self, project: StudioProject, job: Job) -> Image25Adapter:
        if self._image_adapter is not None:
            return self._image_adapter
        service: APIService | None
        if binding := job.request.get("api_binding"):
            service = APIService.model_validate({k: v for k, v in binding.items() if k != "model"})
            model = binding["model"]
        else:
            model = project.config.image_model
            service = self.registry.fixed_media("image", model)
            if service is None:
                raise ProviderExternalError("Configure the fixed image service in the API registry")
        from stateweaver.studio_media.gemini_images import GeminiImageAdapter

        adapter_type = (
            GeminiImageAdapter if service.protocol == "images-gemini-native-v1" else Image25Adapter
        )
        return adapter_type(
            service.base_url,
            self.registry.credential(service),
            model=model,
            timeout_seconds=service.timeout_seconds,
        )

    def _route_adapter(
        self, route: dict[str, Any], *, timeout_limit: float | None = None
    ) -> H3Ref2VAAdapter | HTTPVideoAdapter:
        identifier = str(route["id"])
        if identifier in self._route_adapters:
            return self._route_adapters[identifier]
        timeout = float(route.get("timeout_seconds", 120))
        if timeout_limit is not None:
            timeout = min(timeout, timeout_limit)
        protocol = route.get("protocol", H3_PROTOCOL)
        if protocol == HTTP_VIDEO_PROTOCOL:
            return HTTPVideoAdapter(
                str(route["base_url"]),
                self.registry.credential_ref(str(route["credential_ref"]), optional=True),
                model=str(route["model"]),
                endpoint_id=identifier,
                timeout_seconds=timeout,
                config=VideoHTTPConfig.model_validate(route["video_http"]),
            )
        if protocol != H3_PROTOCOL:
            raise ProviderContractError("Unknown video provider protocol")
        return H3Ref2VAAdapter(
            str(route["base_url"]),
            self.registry.credential_ref(
                str(route.get("credential_ref", "H3_REF_API_KEY")), optional=True
            ),
            model=str(route["model"]),
            endpoint_id=identifier,
            timeout_seconds=timeout,
            # An accepted job may still carry an older binding without the mount.
            # Polling and collection use its exact task ID and do not send new media.
            shared_media_root=h3_shared_root(route)
            if route.get("shared_media_root") is not None
            else None,
            capacity_limit=int(route["capacity_limit"]),
            sglang_ref2va=route.get("wire_profile") == "sglang-ref2va-v1",
            ref2va_verified_job=route.get("ref2va_verified_job"),
        )

    def _video_route(self, job: Job) -> dict[str, Any] | None:
        route = job.request.get("provider_route") or job.result.get("provider_route")
        if route:
            if job.provider_job_id and "model" not in route and "protocol" not in route:
                # The original environment-only route used the adapter's H3 model.
                return {**route, "model": H3_MODEL}
            return dict(route)
        if not job.provider_job_id or self._injected_video_adapter:
            return None
        if binding := job.request.get("api_binding"):
            original = APIService.model_validate({k: v for k, v in binding.items() if k != "model"})
            return original.video_route(binding["model"])
        service = self.registry.fixed_media("video")
        if service is not None and service.protocol == H3_PROTOCOL:
            route = service.video_route(service.models[0])
            adapter = self._route_adapter(route)
            if isinstance(adapter, H3Ref2VAAdapter) and adapter.lookup(job.provider_job_id):
                return {**route, "reconciled_by": "exact_provider_job_id"}
        raise ProviderExternalError(
            "Original video route is unresolved; preserve the known task ID. "
            "No generation was resubmitted."
        )

    def _videos(self, project: StudioProject | None = None) -> H3Ref2VAAdapter | HTTPVideoAdapter:
        if self._video_adapter is not None:
            return self._video_adapter
        routes = self.video_routes(project)
        if not routes:
            raise ProviderExternalError("Configure the fixed video service in the API registry")
        return self._route_adapter(next(iter(routes.values())))

    @staticmethod
    def _video_capacity_binding(route: dict[str, Any]) -> str:
        return provider_config_hash(
            {
                "wire": wire_identity(
                    kind="video",
                    protocol=str(route.get("protocol", H3_PROTOCOL)),
                    base_url=str(route["base_url"]),
                ),
                "version": route.get("registry_version", "unknown"),
                "resource_group": route.get("resource_group"),
                "credential_ref": route.get("credential_ref"),
                "model": route["model"],
                "capacity_limit": route["capacity_limit"],
                "shared_media_root": (
                    str(h3_shared_root(route))
                    if route.get("shared_media_root") is not None
                    else None
                ),
                "wire_profile": route.get("wire_profile"),
                "ref2va_verified_job": route.get("ref2va_verified_job"),
                **({"video_http": route["video_http"]} if "video_http" in route else {}),
            }
        )

    def video_routes(self, project: StudioProject | None = None) -> dict[str, dict[str, Any]]:
        service = self.registry.fixed_media("video")
        if service is None:
            return {}
        if project is not None:
            if project.config.video_model != service.models[0]:
                return {}
            pinned = (
                self.registry.pinned(project.api_config_snapshot).fixed_media("video")
                if project.api_config_snapshot
                else None
            )
            if pinned != service and (pinned is not None or service.protocol != H3_PROTOCOL):
                return {}
        route = service.video_route(service.models[0])
        route.update(self._route_profiles.get(self._video_capacity_binding(route), {}))
        return {route["id"]: route}

    def video_capacities_for(
        self, routes: dict[str, dict[str, Any]], default: dict[str, int]
    ) -> dict[str, int]:
        if not self._capacity_by_binding:
            return default
        return {
            identifier: self._capacity_by_binding.get(self._video_capacity_binding(route), 0)
            for identifier, route in routes.items()
        }

    def verified_video_capacities(self) -> dict[str, int]:
        routes = self.video_routes()
        self.video_capacity_observations = {}
        self._route_profiles = {}
        self._capacity_by_binding = {}
        self.video_capacity_routes = routes
        service = self.registry.fixed_media("video")
        if service is None:
            self.video_capacity_services = {}
            if self._video_adapter is not None:
                return {self._video_adapter.endpoint_id: self._video_adapter.verified_capacity()}
            return {}
        route = next(iter(routes.values()))
        identifier = route["id"]
        self.video_capacity_services = {identifier: service}
        key = self._video_capacity_binding(route)

        def probe() -> tuple[int, dict[str, str | int]]:
            adapter = self._route_adapter(route, timeout_limit=10)
            if isinstance(adapter, HTTPVideoAdapter):
                adapter.probe()
                return int(route["capacity_limit"]), {
                    "revision": str(route["registry_version"]),
                    "topology": "external",
                }
            caps = adapter.capabilities()
            return caps.workers_ready, {
                "revision": caps.revision,
                "topology": caps.topology,
                "latent_modes": ",".join(caps.latent_continuation_modes),
                "latent_endpoint_only": caps.latent_endpoint_only,
                "latent_tail_frame_counts": ",".join(map(str, caps.latent_tail_frame_counts)),
            }

        observation = self._video_capacity_cache.observe(key, probe)
        self.video_capacity_observations[identifier] = observation
        self._capacity_by_binding = {key: observation.capacity}
        if observation.profile:
            self._route_profiles = {key: observation.profile}
            route.update(observation.profile)
        return {identifier: observation.capacity}

    def _directory(self, project: StudioProject, job: Job) -> Path:
        # IDs are domain identifiers, not filesystem paths supplied by model text.
        path = self.work_root / hashlib.sha256(project.id.encode()).hexdigest()[:24]
        path = path / hashlib.sha256(job.id.encode()).hexdigest()[:24]
        path.mkdir(parents=True, exist_ok=True)
        return path

    @contextmanager
    def _scratch_directory(self) -> Iterator[Path]:
        """Keep disposable frame/codec files off durable shared storage.

        Every execution has a distinct directory, including concurrent jobs and
        retries. Published media lives in CAS; resumable observations stay in the
        original durable job directory, never in this temporary workspace.
        """
        if self.media_scratch_root:
            self.media_scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="agentvideo-media-", dir=self.media_scratch_root
        ) as directory:
            yield Path(directory)

    def _bytes(self, project: StudioProject, media_id: str) -> bytes:
        media = project.media.get(media_id)
        if media is None:
            raise ProviderContractError("job references unknown media")
        return self.cas.read_bytes(media.sha256)

    def _image_input(self, project: StudioProject, media_id: str) -> AssetInput:
        media = project.media.get(media_id)
        if media is None or not media.mime_type.startswith("image/"):
            raise ProviderContractError("reference is not an image")
        return AssetInput(media.id + ".png", self._bytes(project, media_id), media.mime_type)

    def _image_uri(self, project: StudioProject, media_id: str) -> str:
        image = self._image_input(project, media_id)
        with Image.open(BytesIO(image.content)) as decoded:
            decoded.load()
            try:
                validate_h3_image_aspect(*decoded.size)
            except ProviderContractError as error:
                raise ProviderContractError(f"{error}；media_id={media_id}") from None
        return str(self.cas.path_for(project.media[media_id].sha256).resolve())

    def _media(
        self,
        project: StudioProject,
        job: Job,
        content: bytes,
        mime_type: str,
        *,
        label: str = "output",
        width: int | None = None,
        height: int | None = None,
        duration: float | None = None,
    ) -> Media:
        stored = self.cas.put_bytes(content)
        identifier = hashlib.sha256(f"{job.id}:{label}:{stored.sha256}".encode()).hexdigest()[:32]
        revision_id = job.target_id if job.kind == "image" else job.request.get("revision_id")
        revision = project.references.get(str(revision_id))
        asset_id = (
            project.states[revision.state_id].asset_id if revision else job.request.get("asset_id")
        )
        return Media(
            id=identifier,
            sha256=stored.sha256,
            mime_type=mime_type,
            byte_size=stored.byte_size,
            width=width,
            height=height,
            duration=duration,
            url=f"/api/v1/studio/projects/{project.id}/media/{identifier}",
            owner_actor_id=job.actor_id,
            source_candidate_id=job.target_id
            if job.kind in {"video", "analysis"}
            else job.request.get("source_candidate_id"),
            source_revision_id=revision.id if revision else None,
            asset_id=asset_id,
        )

    @staticmethod
    def _skill_hash(name: str) -> str:
        return load_media_skill(name).sha256

    def verified_video_capacity(self) -> int:
        return sum(self.verified_video_capacities().values())

    def _image(self, project: StudioProject, job: Job) -> JobOutcome:
        revision = project.references[job.target_id]
        state = project.states[revision.state_id]
        asset = project.assets[state.asset_id]
        waited = project.references.get(
            str(job.result.get("waiting_for_reference_revision_id", ""))
        )
        if waited is not None and waited.status == "failed":
            return JobOutcome("failed", result={"error": "Reference anchor generation failed"})
        snapshot = job.request.get("image_recipe")
        if not snapshot or "anchor_revision_id" not in job.request:
            raise ProviderContractError("Image job lacks its pinned recipe and anchor")
        recipe = snapshot["recipe"]
        anchor_id = job.request["anchor_revision_id"]
        parent_id = job.request.get("parent_id")
        source_revision = project.references.get(str(anchor_id)) if anchor_id else None
        if source_revision is not None and source_revision.state_id != revision.state_id:
            raise ProviderContractError("image parent revision belongs to a different story state")
        if anchor_id and source_revision is None:
            raise ProviderContractError("pinned image anchor revision is missing")
        if source_revision is not None and not source_revision.media_id:
            if source_revision.status == "pending":
                return JobOutcome(
                    "queued", result={"waiting_for_reference_revision_id": source_revision.id}
                )
            raise ProviderContractError("pinned image anchor generation failed")
        adapter = self._images(project, job)
        if self.provider_runtime and (receipt_id := self.provider_runtime.receipt_id(job)):
            adapter = replace(
                adapter,
                transport=AuditedTransport(
                    adapter.transport,
                    self.provider_runtime.coordinator,
                    receipt_id,
                ),
            )
        prompt = view_prompt(
            str(
                job.request.get("asset_description")
                or (
                    f"{asset.name}. {asset.description}. "
                    f"State: {state.label}. {state.description}. "
                )
            )
            + str(job.request["prompt"]),
            str(job.request["view"]),
            recipe=recipe,
        )
        sources = [source_revision] if source_revision else []
        for identifier in job.request.get("source_revision_ids", []):
            source = project.references.get(str(identifier))
            if source is None or not source.media_id:
                raise ProviderContractError("image source revision has no decoded reference")
            if source.status not in {"candidate", "selected"}:
                raise ProviderContractError("image source revision is not usable")
            if all(item.id != source.id for item in sources):
                sources.append(source)
        source_bindings = []
        for index, source in enumerate(sources, start=1):
            source_state = project.states[source.state_id]
            source_asset = project.assets[source_state.asset_id]
            source_bindings.append(
                {
                    "image_index": index,
                    "role": "target_asset" if source_asset.id == asset.id else "depicted_asset",
                    "asset_id": source_asset.id,
                    "asset_name": source_asset.name,
                    "state_id": source_state.id,
                    "state_label": source_state.label,
                    "story_time": source_state.story_time,
                    "revision_id": source.id,
                    "media_id": source.media_id,
                    "view": source.view,
                }
            )
        observed_inputs = job.request.get("source_media_inputs", [])
        for source in observed_inputs:
            self._reference_bytes(project, source)
            observed_asset = project.assets[str(source.get("approved_asset_id") or asset.id)]
            observed_state = state
            if observed_asset.id != asset.id:
                observed_state = next(
                    project.states[reference.state_id]
                    for reference in sources
                    if reference.media_id == source["media_id"]
                    and reference.state_id in source["approved_state_ids"]
                    and project.states[reference.state_id].asset_id == observed_asset.id
                )
            source_bindings.append(
                {
                    "image_index": len(source_bindings) + 1,
                    "role": "approved_partial_observation",
                    "asset_id": observed_asset.id,
                    "asset_name": observed_asset.name,
                    "state_id": observed_state.id,
                    "state_label": observed_state.label,
                    "story_time": observed_state.story_time,
                    "revision_id": None,
                    "media_id": source["media_id"],
                    "view": "observed_detail",
                    "approved_scope": source["approved_scope"],
                    "observation_id": source["observation_id"],
                }
            )
        if snapshot["version"] >= 3:
            instructions = job.request.get("image_instructions", {})
            roles = instructions.get("reference_roles", {})
            if set(roles) - (
                {source.id for source in sources} | {s["media_id"] for s in observed_inputs}
            ):
                raise ProviderContractError("image reference role does not match a supplied source")
            for binding in source_bindings:
                binding["purpose"] = roles.get(binding["revision_id"] or binding["media_id"]) or (
                    "Use ONLY the explicitly approved detail: " + str(binding["approved_scope"])
                    if binding["role"] == "approved_partial_observation"
                    else "Depicted asset identity and story state; exclude source layout"
                    if binding["role"] == "depicted_asset"
                    else "Stable identity only; apply the explicitly requested target story state"
                    if binding["state_id"] != state.id
                    else "Same-state edit target and unchanged composition"
                    if binding["revision_id"] == parent_id
                    else f"Same-state identity and {binding['view']} appearance, not board layout"
                )
            operation = (
                "composite"
                if any(b["role"] == "depicted_asset" for b in source_bindings)
                else "state_transition"
                if any(source.state_id != state.id for source in sources)
                else "same_state_correction"
                if parent_id and source_revision and source_revision.view == revision.view
                else "derived_view"
                if sources
                else "initial_reference"
            )
            prompt += "\n" + recipe["edit_template"].format(
                operation=operation,
                change=instructions.get("change") or job.request["prompt"],
                preserve="; ".join(instructions.get("preserve", []))
                or (
                    "Confirmed identity, target story state, and all attributes unaffected by "
                    "the requested change/view"
                ),
                composition=instructions.get("composition")
                or ("Use the requested view; preserve existing composition for a local correction"),
            )
            if source_bindings:
                prompt += "\nReference images in supplied order:\n" + json.dumps(
                    source_bindings, ensure_ascii=False
                )
        if any(binding["role"] == "depicted_asset" for binding in source_bindings):
            prompt += "\n" + recipe["composition_suffix"]
            if snapshot["version"] < 3:
                prompt += "\nReference images in supplied order:\n" + json.dumps(
                    source_bindings, ensure_ascii=False
                )
        if any(
            item.state_id != state.id and project.states[item.state_id].asset_id == asset.id
            for item in sources
        ):
            prompt += "\n" + recipe["state_transition_suffix"]
        request = ImageRequest(prompt, size=snapshot["size"], quality=snapshot["quality"])
        if self.provider_runtime:
            recovered = self.provider_runtime.start(job)
            if recovered:
                return self.provider_runtime.recovered(recovered)
        if sources or observed_inputs:
            result = adapter.edit(
                request,
                (
                    *tuple(self._image_input(project, str(source.media_id)) for source in sources),
                    *tuple(
                        self._image_input(project, str(source["media_id"]))
                        for source in observed_inputs
                    ),
                ),
            )
        else:
            result = adapter.generate(request)
        material = adapter.materialize(result.assets[0])
        with Image.open(BytesIO(material.content)) as image:
            width, height = image.size
        media = self._media(
            project, job, material.content, material.media_type, width=width, height=height
        )
        return JobOutcome(
            "complete",
            media=[media],
            result={
                "media_id": media.id,
                "source_revision_ids": [source.id for source in sources],
                "provenance": {
                    "provider": result.provider,
                    "source_revision_ids": [source.id for source in sources],
                    "source_media_ids": [source.media_id for source in sources],
                    "source_bindings": source_bindings,
                    "model": result.model,
                    "actual_model": result.observed_model or result.model,
                    "actual_model_source": (
                        "provider_response" if result.observed_model else "effective_binding"
                    ),
                    "requested_model": project.config.image_model,
                    "effective_model": result.model,
                    "image_delivery": job.result.get("image_delivery", {}),
                    "provider_request_id": result.request_id,
                    "source_revision_id": source_revision.id if source_revision else None,
                    "source_media_id": source_revision.media_id if source_revision else None,
                    "state_id": state.id,
                    "asset_id": asset.id,
                    "view": revision.view,
                    "skill_sha256": snapshot["skill_sha256"],
                    "requested_size": request.size,
                    "requested_quality": request.quality,
                    "actual_width": width,
                    "actual_height": height,
                    "usage": dict(result.usage),
                    **dict(result.metadata),
                },
            },
        )

    def _reference_bytes(self, project: StudioProject, source: dict[str, Any]) -> bytes:
        media = project.media.get(source["media_id"])
        if media is None or media.sha256 != source["sha256"]:
            raise ProviderContractError("pinned reference image is missing or changed")
        return self._image_input(project, media.id).content

    def _prepare_reference(self, project: StudioProject, job: Job) -> JobOutcome:
        if job.request.get("operation") == "media_excerpt":
            return self._prepare_media_excerpt(project, job)
        source = job.request["source"]
        from .external_review import blocked_quarantine
        from .reference_inputs import available_alternatives

        if blocked_quarantine(
            project,
            source["media_id"],
            "reference_use",
            actor_id=job.actor_id,
            clip_id=job.request.get("clip_id"),
        ):
            raise ProviderContractError(
                "隔离图片不能准备或裁剪："
                f"revision_id={source['revision_id']}, media_id={source['media_id']}"
            )
        alternatives, newly_excluded = available_alternatives(project, job.request["alternatives"])
        excluded_alternatives = [
            *job.request.get("excluded_alternatives", []),
            *newly_excluded,
        ]
        chosen = source
        requested_crop = job.request.get("crop")
        explicit_board = job.request.get("full_board_reason")
        preferred = job.request.get("preferred_view")
        crop = None
        fallback_reason = ""
        if job.request.get("region_approval_id"):
            # Invalid geometry fails locally; a rejected whole image must never be fallback.
            from .reference_regions import approved_region

            approval = approved_region(
                project,
                source["revision_id"],
                job.request["region_approval_id"],
                job.request.get("clip_id"),
            )
            if list(approval.crop) != list(requested_crop or []):
                raise ProviderContractError("局部批准与固定裁剪区域不一致")
            crop = crop_reference(self._reference_bytes(project, source), approval.crop)
        elif not explicit_board:
            match = next((item for item in alternatives if item["view"] == preferred), None)
            if match is not None:
                chosen = match
                fallback_reason = "使用已选的同资产、同剧情状态所需单视图"
            elif requested_crop is not None:
                content = self._reference_bytes(project, source)
                try:
                    crop = crop_reference(content, tuple(requested_crop))
                except ProviderContractError as error:
                    fallback_reason = str(error)
                    if alternatives:
                        chosen = alternatives[0]
                        fallback_reason += "；回退到已选的同状态单视图"
                    else:
                        fallback_reason += "；无合适单视图，使用完整参考板并明确用途"
            elif is_reference_board(source["view"]):
                if alternatives:
                    chosen = alternatives[0]
                    fallback_reason = "未指定可靠裁剪，使用已选的同资产、同状态单视图"
                else:
                    fallback_reason = "无已选单视图及可靠裁剪区域，使用完整参考板并明确用途"
        media_items = []
        if crop is not None:
            media = self._media(
                project,
                job,
                crop.content,
                "image/png",
                label="reference-crop",
                width=crop.size[0],
                height=crop.size[1],
            )
            media_items.append(media)
            selection = "crop"
        else:
            content = self._reference_bytes(project, chosen)
            with Image.open(BytesIO(content)) as decoded:
                decoded.load()
                validate_h3_image_aspect(*decoded.size)
            media = project.media[chosen["media_id"]]
            selection = "full_board" if is_reference_board(chosen["view"]) else "single"
        binding = {key: chosen[key] for key in ("asset_id", "state_id", "revision_id")}
        return JobOutcome(
            "complete",
            media=media_items,
            result={
                "media_id": media.id,
                "binding": binding,
                "prepared_reference_id": job.id,
                "purpose": job.request["purpose"],
                "provenance": {
                    "selection": selection,
                    "requested_reference": source,
                    "resolved_reference": chosen,
                    "excluded_alternatives": excluded_alternatives,
                    "purpose": job.request["purpose"],
                    "fallback_reason": fallback_reason,
                    "full_board_reason": explicit_board,
                    "output_sha256": media.sha256,
                    **(
                        {"region_approval": job.request["region_approval"]}
                        if job.request.get("region_approval")
                        else {}
                    ),
                    "crop": {
                        "normalized": requested_crop,
                        "pixels": list(crop.box),
                        "source_size": list(crop.source_size),
                        "output_size": list(crop.size),
                    }
                    if crop
                    else None,
                },
            },
        )

    def _latent_anchor(self, project: StudioProject, job: Job) -> EndpointAnchor | None:
        binding = job.request.get("latent_continuation", {"mode": "off"})
        if not isinstance(binding, dict):
            raise ProviderContractError("Invalid pinned latent descriptor")
        if binding.get("mode") == "off":
            return None
        previous = job.request.get("previous_end_state") or {}
        videos = job.request.get("video_inputs", [])
        candidate = project.candidates[job.target_id]
        source = project.candidates.get(str(binding.get("source_candidate_id")))
        endpoint_only = binding.get("binding") == "endpoint-only-v1"
        if (
            binding.get("mode") != ENDPOINT_ANCHOR
            or binding.get("image_index") != 1
            or not endpoint_only
            and binding.get("video_index") != 1
            or job.request.get("workflow_version") != "set-v1"
            or source is None
            or candidate.previous_candidate_id != source.id
            or source.clip_id != binding.get("source_clip_id")
            or source.clip_id == candidate.clip_id
            or project.clips[source.clip_id].set_id != project.clips[candidate.clip_id].set_id
            or project.clips[source.clip_id].order + 1 != project.clips[candidate.clip_id].order
            or previous.get("frame_media_id") != binding.get("endpoint_media_id")
            or not endpoint_only
            and (
                not videos
                or videos[0].get("role") != "continuation"
                or videos[0].get("source_candidate_id") != source.id
                or videos[0].get("media_id") != binding.get("tail_media_id")
                or videos[0].get("sha256") != binding.get("tail_sha256")
            )
            or endpoint_only
            and (
                job.request.get("harness_version") != "adaptive-v2"
                or (job.request.get("repair_plan") or {}).get("mode") != "edit"
                or len(videos) != 1
                or videos[0].get("role") != "edit_source"
            )
        ):
            raise ProviderContractError("Pinned latent source does not match this continuation job")
        self._reference_bytes(
            project,
            {"media_id": binding["endpoint_media_id"], "sha256": binding["endpoint_sha256"]},
        )
        return EndpointAnchor.from_payload(binding)

    def _video(
        self, project: StudioProject, job: Job, *, local_outcome: JobOutcome | None = None
    ) -> JobOutcome:
        if local_outcome is not None and (
            not job.provider_job_id or local_outcome.provider_job_id != job.provider_job_id
        ):
            raise ProviderQueryUnavailable("Saved H3 outcome belongs to a different task")
        try:
            route = self._video_route(job)
        except (ProviderExternalError, ProviderContractError) as error:
            if job.provider_job_id:
                raise ProviderQueryUnavailable(str(error)) from None
            raise
        adapter = self._route_adapter(route) if route else self._videos(project)
        custom = isinstance(adapter, HTTPVideoAdapter)
        provider = HTTP_VIDEO_PROVIDER if custom else "h3-ref2va"
        mode = HTTP_VIDEO_PROTOCOL if custom else "ref2va"
        if self.provider_runtime and (receipt_id := self.provider_runtime.receipt_id(job)):
            adapter = replace(
                adapter,
                transport=AuditedTransport(
                    adapter.transport,
                    self.provider_runtime.coordinator,
                    receipt_id,
                ),
            )
        if job.provider_job_id is None:
            references = []
            prepared_inputs = job.request.get("reference_inputs")
            if prepared_inputs is not None and len(prepared_inputs) != len(job.request["bindings"]):
                raise ProviderContractError("pinned image input count does not match bindings")
            for index, binding in enumerate(job.request["bindings"]):
                revision = project.references[binding["revision_id"]]
                if revision.state_id != binding["state_id"] or not revision.media_id:
                    raise ProviderContractError("video reference does not match the pinned state")
                state = project.states[revision.state_id]
                if state.asset_id != binding["asset_id"]:
                    raise ProviderContractError("video reference does not match the pinned asset")
                if prepared_inputs is not None:
                    source = prepared_inputs[index]
                    if any(
                        source[key] != binding[key]
                        for key in ("asset_id", "state_id", "revision_id")
                    ):
                        raise ProviderContractError(
                            "prepared reference no longer matches the binding"
                        )
                    self._reference_bytes(project, source)
                    if source.get("endpoint_coverage_alias"):
                        from .contracts import ReferenceBinding
                        from .endpoint_coverage import validate_pinned_alias
                        from .store import StudioError

                        if source["endpoint_coverage_alias"].get("endpoint_media_id") != (
                            job.request.get("previous_end_state") or {}
                        ).get("frame_media_id"):
                            raise ProviderContractError(
                                "Pinned endpoint alias differs from actual Picture 1"
                            )
                        try:
                            validate_pinned_alias(
                                project, ReferenceBinding.model_validate(binding), source
                            )
                        except StudioError as error:
                            raise ProviderContractError(str(error)) from None
                    else:
                        references.append(self._image_uri(project, source["media_id"]))
                else:
                    references.append(self._image_uri(project, revision.media_id))
            previous = job.request.get("previous_end_state")
            end_frame = self._image_uri(project, previous["frame_media_id"]) if previous else None
            prompt = str(job.request["provider_prompt"] if custom else job.request["prompt"])
            if (
                job.request.get("harness_version") == "adaptive-v2"
                and (repair := job.request.get("repair_plan") or {}).get("mode") == "edit"
            ):
                videos = job.request.get("video_inputs", [])
                source = project.candidates.get(str(repair.get("source_candidate_id")))
                if (
                    len(videos) != 1
                    or not videos[0].get("full_source")
                    or not source
                    or source.clip_id != project.candidates[job.target_id].clip_id
                    or videos[0].get("source_candidate_id") != source.id
                    or job.request.get("video_continuation")
                ):
                    raise ProviderContractError(
                        "Editing requires exactly the pinned full current candidate"
                    )
            video_settings = job.request.get("video_settings") or {
                "short_edge": int(self.config.get("H3_SHORT_EDGE", "768")),
                "aspect_ratio": self.config.get("H3_ASPECT_RATIO", "16:9"),
            }
            request = Ref2VARequest(
                prompt,
                tuple(references),
                float(job.request["duration"]),
                previous_end_frame=end_frame,
                short_edge=int(video_settings["short_edge"]),
                aspect_ratio=str(video_settings["aspect_ratio"]),
                seed=job.request.get("seed"),
                reference_videos=tuple(
                    self._timed_reference(project, item, "video")
                    for item in job.request.get("video_inputs", [])
                ),
                reference_audio=tuple(
                    self._timed_reference(project, item, "audio")
                    for item in job.request.get("audio_inputs", [])
                ),
                video_continuation=bool(job.request.get("video_continuation")),
                latent_continuation=self._latent_anchor(project, job),
                previous_frame_is_soft_reference=bool(job.request.get("continuation_decision"))
                and not job.request["continuation_decision"]["enabled"],
                video_editing=job.request.get("harness_version") == "adaptive-v2"
                and (job.request.get("repair_plan") or {}).get("mode") == "edit",
            )
            if self.provider_runtime:
                try:
                    prepared = adapter.prepare_submission(request, idempotency_key=job.id)
                except H3CapabilityUnavailable:
                    raise ProviderCapacityWait(
                        time.time() + 2, "h3_preflight_capability_unavailable"
                    ) from None
                except ProviderExternalError as error:
                    retry_after = getattr(error, "retry_after_seconds", None) or 2
                    raise ProviderCapacityWait(
                        time.time() + retry_after, "h3_preflight_unavailable"
                    ) from None
                recovered = self.provider_runtime.start(job)
                if recovered:
                    outcome = self.provider_runtime.recovered(recovered)
                    require_video_outcome(project.id, job, outcome)
                    return outcome
                submitted = adapter.submit_prepared(prepared)
            else:
                submitted = adapter.submit(request, idempotency_key=job.id)
            return JobOutcome(
                "running",
                provider_job_id=submitted.task_id,
                result={
                    "mode": mode,
                    "model": adapter.model,
                    "actual_model": submitted.metrics.get("actual_model"),
                    "provider_route": route,
                    "skill_sha256": job.request.get("h3_skill_sha256")
                    or self._skill_hash("h3-ref2va-set-v2"),
                },
            )
        receipt = None
        if self.provider_runtime and self.provider_runtime.receipt_id(job):
            try:
                receipt = self.provider_runtime.matching_receipt(project, job)
            except ProviderContractError as error:
                raise ProviderQueryUnavailable(str(error)) from None
        if receipt and receipt["provider_job_id"] != job.provider_job_id:
            raise ProviderQueryUnavailable(
                "Video collection requires the exact original provider task"
            )
        if local_outcome is not None:
            require_video_outcome(project.id, job, local_outcome)
        if receipt and receipt["state"] in {"complete", "failed"}:
            assert self.provider_runtime is not None
            outcome = self.provider_runtime.recovered(receipt)
            require_video_outcome(project.id, job, outcome)
            if local_outcome is not None:
                require_same_video_outcome(local_outcome, outcome)
            local_outcome = local_outcome or outcome
        if local_outcome is not None:
            for media in local_outcome.media:
                self._saved_raw_video_path(adapter, job.provider_job_id, media)
            return local_outcome
        saved = self._saved_video_delivery(project, job, receipt)
        status = None
        raw_media: Media | None = None
        if saved:
            raw_media, metrics = saved
        else:
            try:
                status = adapter.poll(job.provider_job_id)
            except (ProviderExternalError, ProviderContractError) as error:
                raise ProviderQueryUnavailable(str(error)) from None
            if status.state in {ProviderJobState.FAILED, ProviderJobState.CANCELLED}:
                return JobOutcome(
                    "failed",
                    provider_job_id=job.provider_job_id,
                    result={
                        "error": status.failure.message
                        if status.failure
                        else "Video generation failed",
                        "blocked_external": bool(status.failure and status.failure.retryable),
                        "provider_route": route,
                    },
                )
            if status.state != ProviderJobState.SUCCEEDED:
                return JobOutcome(
                    "running", provider_job_id=job.provider_job_id, result={"provider_route": route}
                )
            metrics = status.metrics
        latent = job.request.get("latent_continuation", {"mode": "off"})
        resolved = metrics.get("resolved")
        applied_latent = resolved.get("latent_continuation") if isinstance(resolved, dict) else None
        latent_error = _video_latent_delivery_error(
            job, applied_latent, invalid=metrics.get("invalid_latent_receipt") is True
        )
        collection_started = time.monotonic()
        if receipt and self.provider_runtime:
            if status is not None:
                try:
                    self.provider_runtime.record_video_completion(
                        job, status, applied_latent, invalid_latent=latent_error is not None
                    )
                except ProviderContractError as error:
                    raise ProviderQueryUnavailable(str(error)) from None
            if (status is not None or self.provider_runtime.video_completion(receipt)) and (
                self.on_video_remote_completion
                and job.result.get("video_remote_completion_id") != job.provider_job_id
            ):
                self.on_video_remote_completion(project.id, job)
        if latent_error:
            raise CompletedVideoDeliveryError(latent_error)
        if raw_media is None:
            try:
                generated = adapter.download(job.provider_job_id)
            except ProviderContractError as error:
                raise CompletedVideoDeliveryError(str(error)) from None
            if generated.content is None:
                raise CompletedVideoDeliveryError("Completed video job did not return bytes")
            raw_media = self._media(project, job, generated.content, "video/mp4")
            raw_path = self._check_video_cas(raw_media)
            if receipt and self.provider_runtime:
                self.provider_runtime.save_video_raw(job, raw_media)
        else:
            raw_path = self._saved_raw_video_path(adapter, job.provider_job_id, raw_media)
        try:
            metadata = decode_video(raw_path, verify_audio=True)
            raw_media = raw_media.model_copy(
                update={
                    "width": metadata.width,
                    "height": metadata.height,
                    "duration": metadata.duration_seconds,
                }
            )
            self._require_video_duration(float(job.request["duration"]), metadata, resolved)
        except OSError as error:
            if error.errno != errno.ENOSPC:
                raise
            return JobOutcome(
                "running",
                provider_job_id=job.provider_job_id,
                media=[raw_media],
                result={
                    "provider_route": route,
                    "video_delivery_wait": {
                        "reason": "storage_full",
                        "provider_job_id": job.provider_job_id,
                        "raw_media_id": raw_media.id,
                        "raw_sha256": raw_media.sha256,
                    },
                    "provenance": {
                        "actual_model": metrics.get("actual_model"),
                        "applied_latent_continuation": applied_latent,
                    },
                },
            )
        except subprocess.CalledProcessError:
            raise CompletedVideoDeliveryError(
                "Video delivery media processing failed", raw_media=raw_media
            ) from None
        except (ProviderContractError, ValueError) as error:
            raise CompletedVideoDeliveryError(str(error), raw_media=raw_media) from None
        return JobOutcome(
            "complete",
            provider_job_id=job.provider_job_id,
            media=[raw_media],
            result={
                "media_id": raw_media.id,
                "provider_route": route,
                "timings": {
                    "collection_seconds": time.monotonic() - collection_started,
                    "provider_metrics": metrics,
                },
                "provenance": {
                    **({"raw_media_reused": True} if saved and saved[0] else {}),
                    "provider": provider,
                    "model": adapter.model,
                    "actual_model": metrics.get("actual_model"),
                    "mode": mode,
                    "provider_job_id": job.provider_job_id,
                    "provider_route": route,
                    "video_inputs": job.request.get("video_inputs", []),
                    "audio_inputs": job.request.get("audio_inputs", []),
                    "bindings": job.request["bindings"],
                    **(
                        {"reference_inputs": job.request["reference_inputs"]}
                        if "reference_inputs" in job.request
                        else {}
                    ),
                    "previous_end_state": job.request.get("previous_end_state"),
                    **(
                        {
                            "latent_continuation": latent,
                            "applied_latent_continuation": applied_latent,
                        }
                        if "latent_continuation" in job.request
                        else {}
                    ),
                    "requested_duration": job.request["duration"],
                    "requested_seed": job.request.get("seed"),
                    **(
                        {"video_settings": job.request["video_settings"]}
                        if job.request.get("video_settings")
                        else {}
                    ),
                    "actual_duration": metadata.duration_seconds,
                    "audio_streams": metadata.audio_streams,
                    "skill_sha256": job.request.get("h3_skill_sha256")
                    or self._skill_hash("h3-ref2va-set-v2"),
                    "prompt_format": job.request.get("prompt_format"),
                    **(
                        {
                            "submitted_prompt": job.request.get(
                                "provider_prompt", job.request["prompt"]
                            )
                        }
                        if custom
                        or job.request.get("prompt_format") == "h3-ref2va-six-section-set-v1"
                        else {}
                    ),
                },
            },
        )

    def _check_video_cas(self, media: Media) -> Path:
        with self.cas.open(media.sha256) as raw:
            if raw.seek(0, 2) != media.byte_size:
                raise CASIntegrityError("H3 original CAS size does not match the saved result")
        return self.cas.path_for(media.sha256)

    def _saved_raw_video_path(
        self, adapter: H3Ref2VAAdapter | HTTPVideoAdapter, provider_job_id: str, media: Media
    ) -> Path:
        try:
            return self._check_video_cas(media)
        except (FileNotFoundError, CASIntegrityError):
            pass
        try:
            content = adapter.download(provider_job_id).content
            if content is None:
                raise ProviderContractError("Accepted video task did not return content")
            self.cas.restore_bytes(content, sha256=media.sha256, byte_size=media.byte_size)
        except (ProviderContractError, CASIntegrityError):
            raise ProviderQueryUnavailable(
                "Original video bytes are unavailable or do not match the saved media hash and size"
            ) from None
        return self._check_video_cas(media)

    def _saved_video_delivery(
        self, project: StudioProject, job: Job, receipt: dict[str, Any] | None
    ) -> tuple[Media | None, dict[str, Any]] | None:
        saved = (
            self.provider_runtime.pending_video_delivery(receipt)
            if self.provider_runtime and receipt
            else None
        )
        if not saved and (pending := job.result.get("video_delivery_wait")):
            media = project.media.get(str(pending["raw_media_id"]))
            if (
                media is None
                or pending["provider_job_id"] != job.provider_job_id
                or media.sha256 != pending["raw_sha256"]
            ):
                raise ProviderQueryUnavailable("Saved H3 original media requires reconciliation")
            provenance = job.result.get("provenance", {})
            saved = (
                media,
                {
                    "actual_model": provenance.get("actual_model"),
                    "resolved": {
                        "latent_continuation": provenance.get("applied_latent_continuation")
                    },
                },
            )
        if saved and saved[0]:
            require_video_media_owner(project.id, job, saved[0])
        return saved

    @staticmethod
    def _require_video_duration(requested: float, actual: VideoMetadata, resolved: Any) -> None:
        if not math.isfinite(requested) or requested <= 0:
            raise ProviderContractError("Original Clip duration is invalid")
        frame_seconds = 1 / actual.fps
        # H3 can pad to a native frame grid. Retain those real frames, but a
        # truncated take cannot satisfy the requested playback window.
        if (
            min(actual.duration_seconds, actual.frame_count / actual.fps) + frame_seconds + 1e-6
            < requested
        ):
            raise ProviderContractError(
                f"Clip is shorter than requested ({actual.duration_seconds:g}s vs {requested:g}s)"
            )
        if (
            isinstance(resolved, dict)
            and "num_frames" in resolved
            and (type(frames := resolved["num_frames"]) is not int or frames != actual.frame_count)
        ):
            raise ProviderContractError(
                "H3 Clip decoded frame count differs from the resolved output"
            )

    def _prepare_media_excerpt(self, project: StudioProject, job: Job) -> JobOutcome:
        with self._scratch_directory() as directory:
            return self._prepare_media_excerpt_files(project, job, directory)

    def _prepare_media_excerpt_files(
        self, project: StudioProject, job: Job, directory: Path
    ) -> JobOutcome:
        source = job.request["source"]
        media = project.media.get(source["media_id"])
        if media is None or media.sha256 != source["sha256"]:
            raise ProviderContractError("Excerpt source no longer matches the pinned media")
        content = self._bytes(project, media.id)
        if hashlib.sha256(content).hexdigest() != media.sha256:
            raise ProviderContractError("Excerpt source bytes failed hash verification")
        source_path = directory / "source.mp4"
        source_path.write_bytes(content)
        if job.request.get("tail_frame_count") is not None:
            if job.request["kind"] != "video" or not job.request.get("automatic_continuation"):
                raise ProviderContractError(
                    "Frame-tail extraction requires a pinned continuation job"
                )
            excerpt = extract_video_tail(
                source_path,
                directory,
                frame_count=job.request["tail_frame_count"],
                fps=job.request["tail_fps"],
            )
            if (
                excerpt.source_end is None
                or abs(excerpt.source_start - float(job.request["start_seconds"])) > 0.001
                or abs(excerpt.source_end - float(job.request["end_seconds"])) > 0.001
            ):
                raise ProviderContractError(
                    "Real tail frames differ from the pinned source interval"
                )
        else:
            excerpt = extract_media(
                source_path,
                directory,
                kind=job.request["kind"],
                start_seconds=float(job.request["start_seconds"]),
                end_seconds=job.request.get("end_seconds"),
            )
        output = self._media(
            project,
            job,
            excerpt.path.read_bytes(),
            excerpt.mime_type,
            duration=excerpt.duration,
            width=excerpt.width,
            height=excerpt.height,
        )
        return JobOutcome(
            "complete",
            media=[output],
            result={
                "media_id": output.id,
                "duration": output.duration,
                "provenance": {
                    "source": source,
                    "source_candidate_id": job.request["source_candidate_id"],
                    "start_seconds": excerpt.source_start,
                    "end_seconds": excerpt.source_end,
                    "purpose": job.request["purpose"],
                    "output_sha256": output.sha256,
                    "kind": job.request["kind"],
                    "audio_copied_into_video_reference": False,
                    **(
                        {"continuation_policy": job.request["continuation_policy"]}
                        if "continuation_policy" in job.request
                        else {}
                    ),
                    **(
                        {
                            "frame_count": excerpt.frame_count,
                            "fps": excerpt.fps,
                            "source_start_frame": excerpt.source_start_frame,
                            "source_end_frame": excerpt.source_end_frame,
                        }
                        if excerpt.frame_count is not None
                        else {}
                    ),
                },
            },
        )

    def _timed_reference(
        self, project: StudioProject, item: dict[str, Any], kind: str
    ) -> TimedReference:
        media = project.media.get(str(item.get("media_id")))
        if media is None or not media.mime_type.startswith(kind + "/"):
            raise ProviderContractError("Pinned reference has the wrong media type")
        if media.sha256 != item.get("sha256"):
            raise ProviderContractError("Pinned video/audio reference content changed")
        if item.get("full_source"):
            source = project.candidates.get(str(item.get("source_candidate_id")))
            if (
                kind != "video"
                or item.get("role") != "edit_source"
                or not source
                or source.media_id != media.id
                or source.job_id != item.get("source_job_id")
                or not source.review
                or project.jobs[source.job_id].status != "complete"
                or item.get("start_seconds") != 0
                or media.duration != item.get("duration")
                or item.get("end_seconds") != media.duration
            ):
                raise ProviderContractError(
                    "Full edit source provenance or complete duration changed"
                )
        # H3 reads the shared path. Validate the immutable CAS object in one
        # streaming pass without reading a second copy into process memory.
        with self.cas.open(media.sha256):
            pass
        return TimedReference(
            str(self.cas.path_for(media.sha256).resolve()), float(item["duration"])
        )

    def _analyze(self, project: StudioProject, job: Job) -> JobOutcome:
        with self._scratch_directory() as directory:
            return self._director_evidence(project, job, directory)

    def _director_evidence(self, project: StudioProject, job: Job, directory: Path) -> JobOutcome:
        """Prepare inspectable footage without a model call or invented observations."""
        from .harness import CANDIDATE_REVIEW, QUAD_EVIDENCE
        from .workflow import SELF_REVIEW

        candidate = project.candidates[job.target_id]
        if not candidate.media_id:
            raise ProviderContractError("candidate has no generated video to inspect")
        path = directory / "candidate.mp4"
        path.write_bytes(self._bytes(project, candidate.media_id))
        sampling = job.request.get("sampling", {})
        frames = extract_keyframes(
            path,
            directory / "frames",
            fps=float(sampling.get("fps", 2)),
            boundary_fps=float(sampling.get("boundary_fps", 8)),
            focus_intervals=tuple(
                SamplingInterval.model_validate(value)
                for value in sampling.get("focus_intervals", [])
            ),
        )
        with Image.open(frames[-1].path) as image:
            width, height = image.size
        quad = job.request.get("evidence_layout") == QUAD_EVIDENCE
        per_sheet = 4 if quad else 9
        scale = min(1.0, 1536 / max(width, height))
        tile_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        prior = (
            candidate.analysis
            if quad
            and candidate.analysis.get("evidence_layout", {}).get("version") == QUAD_EVIDENCE
            and job.request.get("prior_analysis_job_ids")
            else {}
        )
        if prior:
            end_frame = project.media[prior["end_frame_media_id"]]
        else:
            end_frame = self._media(
                project,
                job,
                frames[-1].path.read_bytes(),
                "image/png",
                label="end_frame",
                width=width,
                height=height,
            )
        evidence_media = [] if prior else [end_frame]
        pages: list[dict[str, Any]] = [dict(page) for page in prior.get("evidence_pages", [])]

        def sheets(
            values: tuple[Keyframe, ...], kind: str, source_id: str = candidate.id
        ) -> list[str]:
            identifiers = []
            source_clip = project.clips[project.candidates[source_id].clip_id]
            prefix = "Previous accepted" if kind == "previous_tail" else "Current candidate"
            for start in range(0, len(values), per_sheet):
                part = values[start : start + per_sheet]
                content, sheet_width, sheet_height = self._sampled_frames_sheet(
                    part,
                    columns=2 if quad else 3,
                    tile_size=tile_size if quad else (480, 270),
                    source_label=f"{prefix} Clip {source_clip.order + 1:02d}" if quad else "",
                )
                media = self._media(
                    project,
                    job,
                    content,
                    "image/png",
                    label=f"{kind}_{start // per_sheet + 1}",
                    width=sheet_width,
                    height=sheet_height,
                )
                evidence_media.append(media)
                identifiers.append(media.id)
                pages.append(
                    {
                        "media_id": media.id,
                        "kind": kind,
                        "timestamps": [f.timestamp_seconds for f in part],
                        "frame_indices": [f.frame_index for f in part],
                        **({"source_candidate_id": source_id} if quad else {}),
                    }
                )
            return identifiers

        known_indices = set(prior.get("sampled_frame_indices", []))
        new_frames = tuple(frame for frame in frames if frame.frame_index not in known_indices)
        new_sampled_ids = sheets(new_frames, "focused_frames" if prior else "sampled_frames")
        sampled_ids = [
            *prior.get("sampled_frames_media_ids", []),
            *new_sampled_ids,
        ]
        analysis: dict[str, Any] = {
            **prior,
            "check_version": SELF_REVIEW,
            "review_mode": "director",
            "skill_sha256": self._skill_hash("video-self-review-v1"),
            "request_id": job.id,
            "end_frame_media_id": end_frame.id,
            "sampled_frames_media_id": sampled_ids[0],
            "sampled_frames_media_ids": sampled_ids,
            "sampled_frame_indices": [f.frame_index for f in frames],
            "sampled_frame_timestamps": [f.timestamp_seconds for f in frames],
            "sampling": {
                "version": "dense-v1",
                "fps": 2,
                "boundary_fps": 8,
                **sampling,
            },
            "prior_analysis_job_ids": job.request.get("prior_analysis_job_ids", []),
            "limitations": [
                "证据为全片抽帧和边界密集抽帧；帧间的细微运动需要定点加密检查。",
                "本次未听取或评估音频，不能声称音频质量或声画同步已通过。",
                "结论由片场导演根据实际查看的证据给出，未调用独立视频理解模型。",
            ],
        }
        if quad:
            analysis.update(
                review_context_version=prior.get("review_context_version", CANDIDATE_REVIEW),
                evidence_layout={
                    "version": QUAD_EVIDENCE,
                    "frames_per_sheet": per_sheet,
                    "columns": 2,
                    "tile_width": tile_size[0],
                    "tile_height": tile_size[1],
                    "caption_height": 32,
                },
            )
        if candidate.previous_candidate_id and (
            not prior or not any(page.get("kind") == "seam" for page in pages)
        ):
            previous = project.candidates[candidate.previous_candidate_id]
            state = actual_end_state(previous)
            if (
                previous.status != "accepted"
                or project.clips[previous.clip_id].accepted_candidate_id != previous.id
                or not state
                or not previous.media_id
            ):
                raise ProviderContractError("continuation requires accepted preceding evidence")
            if prior and (
                prior.get("previous_boundary_sha256") != project.media[state.frame_media_id].sha256
                or any(
                    page.get("source_candidate_id") != previous.id
                    for page in pages
                    if page.get("kind") == "previous_tail"
                )
            ):
                raise ProviderContractError(
                    "continuation evidence no longer matches selected predecessor"
                )
            from .image_input_recovery import quarantine, retained_previous_tail

            retained = retained_previous_tail(project, previous)
            if retained is not None:
                # The previous review's original pages keep their exclusions.
                # A new collage/seam would reintroduce the refused image content.
                known_pages = {page["media_id"] for page in pages}
                pages.extend(page for page in retained if page["media_id"] not in known_pages)
                analysis["previous_tail_media_ids"] = [page["media_id"] for page in retained]
                analysis["previous_boundary_sha256"] = project.media[state.frame_media_id].sha256
                analysis["limitations"].append(
                    "前段存在隔离画面；接续审核复用原有尾部证据，未重新抽帧或制作接缝拼图。"
                    "隔离范围仍未审，接缝效果不能视为已验证。"
                )
            else:
                if quarantine(project, state.frame_media_id):
                    raise ProviderContractError(
                        "quarantined preceding endpoint cannot be recomposed"
                    )
                previous_path = directory / "previous-end.png"
                source = self._image_input(project, state.frame_media_id)
                previous_path.write_bytes(source.content)
                previous_frame = Keyframe(
                    -1, 0, previous_path, hashlib.sha256(source.content).hexdigest()
                )
                comparison = boundary_comparison(
                    previous_frame,
                    frames[0],
                    tile_size=tile_size if quad else (768, 432),
                )
                seam = self._media(project, job, comparison.content, "image/png", label="seam")
                evidence_media.append(seam)
                analysis["seam_media_id"] = seam.id
                analysis["boundary_comparison_media_id"] = seam.id
                analysis["previous_boundary_sha256"] = previous_frame.sha256
                if not prior:
                    previous_video = directory / "previous.mp4"
                    previous_video.write_bytes(self._bytes(project, previous.media_id))
                    previous_frames = extract_keyframes(
                        previous_video,
                        directory / "previous-frames",
                        fps=float(sampling.get("fps", 2)),
                        boundary_fps=float(sampling.get("boundary_fps", 8)),
                    )
                    tail = tuple(
                        f
                        for f in previous_frames
                        if f.timestamp_seconds >= previous_frames[-1].timestamp_seconds - 3
                    )
                    analysis["previous_tail_media_ids"] = sheets(tail, "previous_tail", previous.id)
                    analysis["previous_tail_frame_timestamps"] = [f.timestamp_seconds for f in tail]
                pages.append({"media_id": seam.id, "kind": "seam", "timestamps": [0.0]})
        if not prior:
            pages.append(
                {
                    "media_id": end_frame.id,
                    "kind": "end_frame",
                    "timestamps": [frames[-1].timestamp_seconds],
                    "frame_indices": [frames[-1].frame_index],
                }
            )
        if quad:
            order = {
                "seam": 0,
                "previous_tail": 1,
                "sampled_frames": 2,
                "focused_frames": 3,
                "end_frame": 4,
            }
            pages.sort(key=lambda page: order[page["kind"]])
        analysis["evidence_pages"] = pages
        analysis["required_review_media_ids"] = [page["media_id"] for page in pages]
        return JobOutcome("complete", media=evidence_media, result={"analysis": analysis})

    @staticmethod
    def _sampled_frames_sheet(
        frames: tuple[Keyframe, ...],
        *,
        columns: int = 3,
        tile_size: tuple[int, int] = (480, 270),
        source_label: str = "",
    ) -> tuple[bytes, int, int]:
        """Render the sampled review frames without cropping."""
        columns = min(columns, len(frames))
        rows = (len(frames) + columns - 1) // columns
        tile_width, tile_height = tile_size
        caption_height = 32
        size = (columns * tile_width, rows * (tile_height + caption_height))
        sheet = Image.new("RGB", size, "white")
        drawing = ImageDraw.Draw(sheet)
        for index, frame in enumerate(frames):
            x = (index % columns) * tile_width
            y = (index // columns) * (tile_height + caption_height)
            drawing.text(
                (x + 12, y + 7),
                f"{source_label + ' | ' if source_label else ''}"
                f"Frame {frame.frame_index} | {frame.timestamp_seconds:.3f} s",
                fill="black",
                font_size=18,
            )
            with Image.open(frame.path) as source:
                thumbnail = ImageOps.contain(source.convert("RGB"), (tile_width, tile_height))
            sheet.paste(
                thumbnail,
                (
                    x + (tile_width - thumbnail.width) // 2,
                    y + caption_height + (tile_height - thumbnail.height) // 2,
                ),
            )
        output = BytesIO()
        sheet.save(output, "PNG")
        return output.getvalue(), sheet.width, sheet.height

    def _assemble(self, project: StudioProject, job: Job) -> JobOutcome:
        with self._scratch_directory() as directory:
            return self._assemble_files(project, job, directory)

    def _assemble_files(self, project: StudioProject, job: Job, directory: Path) -> JobOutcome:
        paths: list[Path] = []
        segments: list[dict[str, Any]] = []
        for production_set, clips in ordered_assembly_sets(project):
            for clip in clips:
                if clip.status != "accepted" or not clip.accepted_candidate_id:
                    raise ProviderContractError("assembly contains an unaccepted Clip")
                candidate = project.candidates[clip.accepted_candidate_id]
                if candidate.status != "accepted" or not candidate.media_id:
                    raise ProviderContractError("assembly contains an unaccepted candidate")
                path = directory / f"source-{len(paths):04d}.mp4"
                path.write_bytes(self._bytes(project, candidate.media_id))
                paths.append(path)
                source_job = project.jobs[candidate.job_id]
                segments.append(
                    {
                        "set_id": production_set.id,
                        "film_order": production_set.order,
                        "story_time": production_set.story_time,
                        "clip_id": clip.id,
                        "clip_order": clip.order,
                        "clip_story_time": source_job.request.get(
                            "story_time", production_set.story_time
                        ),
                        "clip_story_end_time": source_job.request.get("story_end_time"),
                        "asset_plan": source_job.request.get("asset_plan", []),
                        "observed_asset_states": candidate.observed_asset_states,
                        "candidate_id": candidate.id,
                        "media_id": candidate.media_id,
                        "sha256": project.media[candidate.media_id].sha256,
                        "job_id": source_job.id,
                        "provider_job_id": source_job.provider_job_id,
                        "bindings": [binding.model_dump() for binding in candidate.bindings],
                        "selection": candidate.selection.model_dump(mode="json")
                        if candidate.selection
                        else None,
                        "quality_episode": clip.quality_episode.model_dump(mode="json")
                        if clip.quality_episode
                        else None,
                        "provenance": {
                            **source_job.result.get("provenance", {}),
                            "analysis": candidate.analysis,
                            "director_review": candidate.review.model_dump(mode="json")
                            if candidate.review
                            else None,
                            "director_review_history": [
                                review.model_dump(mode="json")
                                for review in candidate.review_history
                            ],
                            "repair_plan": candidate.repair_plan.model_dump(mode="json")
                            if candidate.repair_plan
                            else None,
                        },
                    }
                )
        if not paths:
            raise ProviderContractError("assembly has no accepted Clips")
        output = directory / "assembled.mp4"
        source_audio_streams = self._concat(paths, output)
        for segment, streams in zip(segments, source_audio_streams, strict=True):
            segment["source_audio_streams"] = streams
            segment["audio_delivery"] = "source" if streams else "silent_fill"
        metadata = decode_video(output, verify_audio=True)
        media = self._media(
            project,
            job,
            output.read_bytes(),
            "video/mp4",
            width=metadata.width,
            height=metadata.height,
            duration=metadata.duration_seconds,
        )
        manifest: dict[str, Any] = {
            "project_id": project.id,
            "screenplay_hash": project.screenplay.confirmed_hash,
            "model_config": project.config.model_dump(),
            "segments": segments,
            "actors": [
                {"id": a.id, "role": a.role, "skill_sha256": a.skill_hash}
                for a in project.actors.values()
            ],
            "audio_streams": metadata.audio_streams,
            "output_sha256": media.sha256,
        }
        return JobOutcome(
            "complete",
            media=[media],
            result={"media_id": media.id, "manifest": manifest},
        )

    @staticmethod
    def _concat(paths: list[Path], destination: Path, *, audio: bool = True) -> list[int]:
        metadata = [decode_video(path, verify_audio=audio) for path in paths]
        source_audio_streams = [item.audio_streams for item in metadata]
        source_duration = sum(item.duration_seconds for item in metadata)
        first = metadata[0]
        stream_layout = (
            first.width,
            first.height,
            first.fps,
            first.video_codec,
            first.video_time_base,
        )
        if (
            not audio
            and first.video_time_base
            and all(
                (
                    source.width,
                    source.height,
                    source.fps,
                    source.video_codec,
                    source.video_time_base,
                )
                == stream_layout
                for source in metadata
            )
        ):
            silent_segments = []
            for index, path in enumerate(paths):
                out = destination.parent / f"silent-{index:04d}.mp4"
                JobExecutor._ffmpeg(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        str(path),
                        "-map",
                        "0:v:0",
                        "-an",
                        "-c:v",
                        "copy",
                        str(out),
                    ]
                )
                silent_segments.append(out)
            listing = destination.parent / "silent-concat.txt"
            listing.write_text("".join(f"file '{path.name}'\n" for path in silent_segments))
            JobExecutor._ffmpeg(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "1",
                    "-i",
                    str(listing),
                    "-map",
                    "0:v:0",
                    "-an",
                    "-c:v",
                    "copy",
                    str(destination),
                ]
            )
            result = decode_video(destination)
            if (
                result.audio_streams
                or result.frame_count != sum(item.frame_count for item in metadata)
                or abs(result.duration_seconds - source_duration) > 0.2
            ):
                raise ProviderContractError("Silent assembly changed delivered tracks or duration")
            return source_audio_streams
        fps = Fraction(first.fps).limit_denominator(100_000)
        if audio and any(item.audio_streams > 1 for item in metadata):
            raise ProviderContractError(
                "multi-track source audio requires an explicit export layout"
            )
        # Normalize each segment to one video/audio layout so flashback film order
        # never shifts or drops native audio when clips differ in duration/layout.
        normalized = []
        elapsed = Fraction(0)
        previous_end = 0
        for index, (path, source) in enumerate(zip(paths, metadata, strict=True)):
            # Quantize cut positions on the final timeline. Rounding each Clip
            # independently compounds frame loss/duplication over long films.
            elapsed += Fraction(str(source.duration_seconds))
            end = round(elapsed * fps)
            frames = end - previous_end
            if frames <= 0:
                raise ProviderContractError("A source Clip is too short for the output frame grid")
            previous_end = end
            seconds = float(frames / fps)
            out = destination.parent / f"normalized-{index:04d}.mp4"
            command = [
                "ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-y",
                "-i",
                str(path),
            ]
            if audio and not source.audio_streams:
                command += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            command += [
                "-map",
                "0:v:0",
                "-vf",
                f"scale={first.width}:{first.height}:force_original_aspect_ratio=decrease,"
                f"pad={first.width}:{first.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                f"setpts=PTS-STARTPTS,fps={fps},"
                f"tpad=stop_mode=clone:stop=1,trim=end_frame={frames}",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
            ]
            if audio:
                audio_filter = "apad"
                if source.audio_streams and source.video_start_offset_seconds:
                    # Match the new visual origin, preserving source A/V alignment.
                    offset = source.video_start_offset_seconds
                    audio_filter = f"asetpts=PTS-({offset:.9f})/TB,aresample=48000:first_pts=0,apad"
                command += [
                    "-map",
                    "0:a:0" if source.audio_streams else "1:a:0",
                    "-c:a",
                    "aac",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-af",
                    audio_filter,
                ]
            else:
                command.append("-an")
            command += ["-t", str(seconds), str(out)]
            JobExecutor._ffmpeg(command)
            normalized.append((out, seconds))
        listing = destination.parent / "concat.txt"
        listing.write_text(
            "".join(f"file '{path.name}'\nduration {seconds:.9f}\n" for path, seconds in normalized)
        )
        JobExecutor._ffmpeg(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(listing),
                "-map",
                "0:v:0",
                *(["-map", "0:a:0"] if audio else ["-an"]),
                "-c",
                "copy",
                str(destination),
            ]
        )
        result = decode_video(destination, verify_audio=audio)
        if (
            result.audio_streams != int(audio)
            or result.frame_count != previous_end
            or abs(result.duration_seconds - source_duration) > 0.2
        ):
            raise ProviderContractError("assembled media lost audio or source duration")
        return source_audio_streams

    @staticmethod
    def _ffmpeg(command: list[str]) -> None:
        try:
            result = subprocess.run(command, capture_output=True, timeout=600, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            raise ProviderContractError("assembly decoder is unavailable or timed out") from None
        if result.returncode:
            raise ProviderContractError("assembly media processing failed")
