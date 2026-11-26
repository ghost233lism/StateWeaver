"""Connect Studio jobs to the shared API journal."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Literal

from .audit import atomic_id, record_event
from .base import (
    ProviderContractError,
    ProviderJob,
    ProviderJobState,
    provider_config_hash,
)
from .coordination import (
    ProviderCapacityWait,
    ProviderCoordinator,
    ProviderSubmissionUnknown,
    _validate_result,
    classify_error,
)
from .registry import APIKind, APIRegistry, APIService, route_physical_identity
from .video_http_config import H3_PROTOCOL, HTTP_VIDEO_PROTOCOL, HTTP_VIDEO_PROVIDER

if TYPE_CHECKING:
    from stateweaver.studio.contracts import Job, Media, StudioProject
    from stateweaver.studio.job_executor import JobOutcome


def requested_latent_receipt(value: Any) -> Any:
    """Publish transport provenance; keep Studio continuity IDs in the original job."""
    local_ids = {"source_clip_id", "endpoint_media_id", "tail_media_id"}
    if not isinstance(value, dict):
        return value
    for key in local_ids.intersection(value):
        if value[key] is not None and atomic_id(value[key]) is None:
            raise ProviderContractError("Invalid Studio continuity identity")
    return {key: item for key, item in value.items() if key not in local_ids}


def applied_latent_receipt(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    proof = {
        key: item
        for key, item in value.items()
        if key
        in {
            "mode",
            "binding",
            "image_index",
            "video_index",
            "source_candidate_id",
            "endpoint_sha256",
            "tail_sha256",
            "tail_frame_count",
            "applied",
        }
    }
    try:
        _validate_result({"applied_latent": proof})
    except ProviderContractError:
        return None
    return proof or None


class ProviderRuntime:
    def __init__(
        self, registry: APIRegistry, coordinator: ProviderCoordinator, workspace: str
    ) -> None:
        self.registry, self.coordinator, self.workspace = (
            registry,
            coordinator,
            workspace,
        )
        # Only a direct journal needs the one-time WAL/schema check here. The
        # remote owner already prepared its journal before publishing its marker;
        # repeating that read RPC can strand a native lifespan behind cold reads.
        # Actual remote operations still authenticate and validate at the owner.
        if not self.coordinator.is_remote:
            self.coordinator.prepare()

    def for_project(self, project: StudioProject) -> APIRegistry:
        return (
            self.registry.pinned(project.api_config_snapshot)
            if project.api_config_snapshot
            else self.registry
        )

    @staticmethod
    def semantic_request(job: Job) -> dict[str, Any]:
        return {k: v for k, v in job.request.items() if k not in {"api_receipt_id", "api_binding"}}

    def bind(
        self,
        project: StudioProject,
        job: Job,
        *,
        owner: str,
        capacity: int | None = None,
    ) -> dict[str, Any]:
        registry = self.for_project(project)
        kind: Literal["image", "video"] = "image" if job.kind == "image" else "video"
        model = project.config.image_model if kind == "image" else project.config.video_model
        if job.request.get("api_receipt_id"):
            receipt = self.coordinator.get(job.request["api_receipt_id"])
            binding = APIService.model_validate(
                {k: v for k, v in job.request["api_binding"].items() if k != "model"}
            )
            if receipt["request_hash"] != provider_config_hash(self.semantic_request(job)):
                raise ProviderContractError("Submitted API request recipe has changed")
            if receipt["service_id"] != binding.id:
                raise ProviderContractError("Submitted API request route has changed")
            saved_model = job.request["api_binding"]["model"]
            if receipt["model"] != saved_model or (
                receipt["context"].get("binding_hash")
                and receipt["context"]["binding_hash"]
                != provider_config_hash(binding.binding(saved_model))
            ):
                raise ProviderContractError("Submitted API protocol binding has changed")
            if receipt["state"] in {"reserved", "failed", "cancelled"} and not receipt.get(
                "provider_job_id"
            ):
                service = self.registry.fixed_media(kind, model)
                if service is None or binding.binding(saved_model) != service.binding(model):
                    raise ProviderContractError(
                        "Unsubmitted request differs from the configured media binding"
                    )
                renewed = self.coordinator.reserve(
                    binding,
                    saved_model,
                    receipt["context"],
                    receipt["request_hash"],
                    receipt["config_hash"],
                    owner=owner,
                    capacity=capacity,
                    writer_timeout_seconds=0.25,
                )
                job.result["api_receipt_generation"] = renewed["generation"]
                return renewed
            job.result["api_receipt_generation"] = receipt["generation"]
            return receipt
        route = job.request.get("provider_route") or {}
        service = self.registry.fixed_media(kind, model)
        pinned = registry.fixed_media(kind, model)
        if (
            service is None
            or pinned != service
            or (
                route
                and (
                    service.id != route.get("service_id", route.get("id"))
                    or service.base_url != route.get("base_url")
                    or (kind == "video" and route.get("protocol", H3_PROTOCOL) != service.protocol)
                    or (
                        service.protocol == HTTP_VIDEO_PROTOCOL
                        and any(
                            route.get(key) != value
                            for key, value in service.video_route(model).items()
                        )
                    )
                    or ("model" in route and model != route["model"])
                )
            )
        ):
            raise ProviderContractError("Pinned project lacks the fixed media binding")
        registry.credential(service)
        actor = project.actors[job.actor_id]
        recipe_hash = provider_config_hash(self.semantic_request(job))
        context: dict[str, Any] = {
            "workspace": self.workspace,
            "project_id": project.id,
            "actor_id": actor.id,
            "task_id": actor.delegate_task_id or actor.visual_task_id,
            "job_id": job.id,
            "operation": kind + "_generation",
            "attempt": 1,
            "recipe_hash": recipe_hash,
            "binding_hash": provider_config_hash(service.binding(model)),
        }
        context["workload"] = workload_request(job.request) if kind == "video" else {}
        if kind == "video":
            context["deployment_hash"] = route_physical_identity(
                route or service.video_route(model)
            )[2]
        receipt = self.coordinator.reserve(
            service,
            model,
            context,
            recipe_hash,
            registry.snapshot()["sha256"],
            owner=owner,
            capacity=capacity,
            writer_timeout_seconds=0.25,
        )
        job.request["api_binding"] = service.binding(model)
        job.request["api_receipt_id"] = receipt["id"]
        job.result["api_receipt_generation"] = receipt["generation"]
        return receipt

    def adopt(self, project: StudioProject, job: Job) -> None:
        if self.receipt_id(job) or job.kind not in {"image", "video"}:
            return
        registry = self.for_project(project)
        kind: APIKind = "image" if job.kind == "image" else "video"
        model = project.config.image_model if kind == "image" else project.config.video_model
        route = job.request.get("provider_route") or job.result.get("provider_route") or {}
        protocol = route.get("protocol", H3_PROTOCOL) if kind == "video" else None
        candidates = [
            item
            for item in registry.candidates(kind, model)
            if protocol is None or item.protocol == protocol
        ]
        service = (
            next((s for s in candidates if s.base_url == route.get("base_url")), None)
            if route
            else (candidates[0] if len(candidates) == 1 else None)
        )
        if not service:
            # No trustworthy route: account the uncertainty, never guess that a
            # known/unknown old task freed its GPUs or belonged to a new endpoint.
            service = APIService(
                id="legacy-unresolved-" + kind,
                label="Unresolved original submission",
                kind=kind,
                protocol=(
                    HTTP_VIDEO_PROTOCOL
                    if protocol == HTTP_VIDEO_PROTOCOL
                    else H3_PROTOCOL
                    if kind == "video"
                    else "images-json-v1"
                ),
                capabilities={"video_http": route["video_http"]}
                if protocol == HTTP_VIDEO_PROTOCOL
                else {},
                base_url=route.get("base_url") or "http://unresolved.invalid",
                credential_ref=route.get("credential_ref")
                or ("H3_REF_API_KEY" if kind == "video" else "GPT_IMAGE_KEY"),
                models=[model],
                resource_group=route.get("resource_group") or "unresolved-" + kind,
                capacity=int(route.get("capacity_limit") or 1),
            )
        receipt = self.coordinator.adopt(
            service,
            model,
            {
                "workspace": self.workspace,
                "project_id": project.id,
                "actor_id": job.actor_id,
                "job_id": job.id,
                "operation": kind + "_generation",
                "recipe_hash": provider_config_hash(job.request),
                "workload": workload_request(job.request) if kind == "video" else {},
                **(
                    {
                        "deployment_hash": route_physical_identity(
                            route or service.video_route(model)
                        )[2]
                    }
                    if kind == "video"
                    else {}
                ),
            },
            provider_config_hash(job.request),
            registry.snapshot()["sha256"],
            provider_job_id=job.provider_job_id,
            writer_timeout_seconds=0.25,
        )
        # Sidecar only: do not rewrite any already submitted historical recipe.
        job.result["api_receipt_id"] = receipt["id"]

    @staticmethod
    def receipt_id(job: Job) -> str | None:
        return job.request.get("api_receipt_id") or job.result.get("api_receipt_id")

    def start(self, job: Job) -> dict[str, Any] | None:
        identifier = self.receipt_id(job)
        if not identifier:
            return None
        from stateweaver.studio.job_receipts import request_hash

        receipt = self.coordinator.begin(
            identifier,
            owner=job.lease_owner or "",
            generation=job.result.get("api_receipt_generation"),
            execution_generation=job.lease_generation,
            local_request_hash=request_hash(job),
            writer_timeout_seconds=0.25,
        )
        if receipt.get("may_submit"):
            return None
        if receipt.get("provider_job_id") or receipt["state"] == "complete":
            return receipt
        raise ProviderSubmissionUnknown(identifier)

    def matching_receipt(self, project: StudioProject, job: Job) -> dict[str, Any]:
        """Reconcile only the original semantic recipe, owner, model and execution."""
        from stateweaver.studio.job_receipts import request_hash

        identifier = self.receipt_id(job)
        if not identifier:
            raise ProviderContractError("Job has no shared API receipt")
        receipt = self.coordinator.get(identifier)
        context = receipt["context"]
        if any(
            context.get(key) != value
            for key, value in {
                "workspace": self.workspace,
                "project_id": project.id,
                "actor_id": job.actor_id,
                "job_id": job.id,
            }.items()
        ):
            raise ProviderContractError("Shared receipt is not owned by this original Studio job")
        binding = job.request.get("api_binding")
        expected_model = (
            binding.get("model")
            if binding
            else project.config.image_model
            if job.kind == "image"
            else project.config.video_model
        )
        if receipt["model"] != expected_model or receipt["request_hash"] not in {
            provider_config_hash(self.semantic_request(job)),
            provider_config_hash(job.request),
        }:
            raise ProviderContractError("Shared receipt model or original request hash changed")
        if binding:
            if receipt["service_id"] != binding.get("id") or receipt["model"] != binding.get(
                "model"
            ):
                raise ProviderContractError("Shared receipt original API binding changed")
            if context.get("binding_hash") != provider_config_hash(binding):
                raise ProviderContractError("Shared receipt original protocol binding changed")
        if receipt.get("execution_generation") not in {None, job.lease_generation}:
            raise ProviderContractError("Shared receipt execution generation does not match")
        if receipt.get("local_request_hash") not in {None, request_hash(job)}:
            raise ProviderContractError("Shared receipt exact local recipe does not match")
        if job.result.get("api_receipt_generation", receipt["generation"]) != receipt["generation"]:
            raise ProviderContractError("Shared receipt reservation generation does not match")
        if job.provider_job_id and receipt["provider_job_id"] not in {
            None,
            job.provider_job_id,
        }:
            raise ProviderContractError("Shared receipt original provider job ID changed")
        return receipt

    def cancel_unsubmitted(self, job: Job) -> None:
        identifier = job.request.get("api_receipt_id")
        if identifier and self.coordinator.get(identifier)["state"] == "reserved":
            generation = job.result.get("api_receipt_generation")
            if not isinstance(generation, int) or not job.lease_owner:
                raise ProviderContractError(
                    "Unsubmitted cancellation lacks its owner/generation fence"
                )
            self.coordinator.update(
                identifier, "cancelled", owner=job.lease_owner, generation=generation
            )
            job.request.pop("api_receipt_id", None)
            job.request.pop("api_binding", None)

    def claim_collection(self, job: Job) -> None:
        from stateweaver.studio.job_receipts import request_hash

        if identifier := self.receipt_id(job):
            receipt = self.coordinator.claim_collection(
                identifier,
                owner=job.lease_owner or "",
                execution_generation=job.lease_generation,
                local_request_hash=request_hash(job),
                writer_timeout_seconds=0.25,
            )
            job.result["api_receipt_generation"] = receipt["generation"]

    @staticmethod
    def video_completion(receipt: dict[str, Any]) -> dict[str, Any] | None:
        proof = receipt["result"].get("video_completion")
        if (
            receipt["state"] in {"running", "collecting"}
            and receipt["context"].get("operation") == "video_generation"
            and receipt["provider_job_id"]
            and isinstance(proof, dict)
            and proof.get("provider_job_id") == receipt["provider_job_id"]
        ):
            return proof
        return None

    def record_video_completion(
        self, job: Job, status: ProviderJob, applied_latent: Any, *, invalid_latent: bool = False
    ) -> None:
        """Release physical capacity before any local delivery work begins."""
        route = job.request.get("provider_route") or job.result.get("provider_route") or {}
        protocol = job.request.get("api_binding", {}).get("protocol", route.get("protocol"))
        provider = HTTP_VIDEO_PROVIDER if protocol == HTTP_VIDEO_PROTOCOL else "h3-ref2va"
        if (
            status.state != ProviderJobState.SUCCEEDED
            or status.provider != provider
            or (not job.provider_job_id or status.task_id != job.provider_job_id)
        ):
            raise ProviderContractError(
                "Video completion did not identify the original provider task"
            )
        observed = status.metrics.get("resolved")
        resolved = observed if isinstance(observed, dict) else {}
        proof: dict[str, Any] = {"provider_job_id": job.provider_job_id}
        if "num_frames" in resolved:
            frames = resolved["num_frames"]
            # Keep only integers exactly representable across JSON consumers.
            if type(frames) is int and 0 < frames < 2**53:
                proof["num_frames"] = frames
            else:
                proof["invalid_frame_count"] = True
        applied = applied_latent_receipt(applied_latent)
        if invalid_latent or (applied_latent and applied is None):
            proof["invalid_latent_receipt"] = True
            applied = None
        self._update_video_collection(
            job,
            {
                "video_completion": proof,
                "actual_model": status.metrics.get("actual_model"),
                "applied_latent": applied,
                "error": {},
            },
        )

    def save_video_raw(self, job: Job, media: Media) -> None:
        self._update_video_collection(job, {"media": [media.model_dump(mode="json")]})

    def _update_video_collection(self, job: Job, result: dict[str, Any]) -> None:
        if identifier := self.receipt_id(job):
            self.coordinator.update(
                identifier,
                "running",
                provider_job_id=job.provider_job_id,
                result=result,
                owner=job.lease_owner,
                generation=job.result.get("api_receipt_generation"),
            )

    @staticmethod
    def pending_video_delivery(
        receipt: dict[str, Any],
    ) -> tuple[Media | None, dict[str, Any]] | None:
        """Resume delivery using established remote completion and raw CAS when available."""
        from stateweaver.studio.contracts import Media

        proof = ProviderRuntime.video_completion(receipt)
        data = receipt["result"]
        legacy_wait = (
            receipt["state"] in {"running", "collecting"}
            and data.get("error", {}).get("code") == "local_video_storage_wait"
        )
        if proof is None and not legacy_wait:
            return None
        records = data.get("media", [])
        if len(records) > 1 or (legacy_wait and len(records) != 1):
            raise ProviderContractError("Saved H3 delivery has an inconsistent raw media receipt")
        media = Media.model_validate(records[0]) if records else None
        resolved = {"latent_continuation": data.get("applied_latent")}
        if proof and "num_frames" in proof:
            resolved["num_frames"] = proof["num_frames"]
        elif proof and proof.get("invalid_frame_count"):
            resolved["num_frames"] = None
        return media, {
            "actual_model": data.get("actual_model"),
            "resolved": resolved,
            **(
                {"invalid_latent_receipt": True}
                if proof and proof.get("invalid_latent_receipt")
                else {}
            ),
        }

    def outcome(self, job: Job, outcome: JobOutcome) -> None:
        identifier = self.receipt_id(job)
        if not identifier:
            return
        if outcome.status == "queued":
            self.cancel_unsubmitted(job)
            return
        provenance = outcome.result.get("provenance", {})
        result = {
            "actual_model": provenance.get("actual_model", outcome.result.get("actual_model")),
            "usage": provenance.get("usage") or None,
            "media": [m.model_dump(mode="json") for m in outcome.media],
            "requested_latent": requested_latent_receipt(job.request.get("latent_continuation")),
            "applied_latent": applied_latent_receipt(provenance.get("applied_latent_continuation")),
            "provider_request_id": provenance.get("provider_request_id"),
        }
        result = {k: v for k, v in result.items() if v is not None and (k != "media" or v)}
        if outcome.status == "running" and outcome.result.get("video_delivery_wait"):
            result["error"] = {
                "category": "local_storage",
                "code": "local_video_storage_wait",
                "phase": "delivery",
                "known_job": True,
                "retryable": True,
                "outcome_known": True,
            }
        if job.kind == "video" and job.provider_job_id and outcome.status in {"complete", "failed"}:
            result["error"] = {}
        if outcome.status == "failed" and outcome.result.get("delivery_failure"):
            result["error"] = {
                "category": "protocol_or_capability",
                "code": "remote_complete_delivery_invalid",
                "phase": "delivery",
                "known_job": True,
                "retryable": False,
                "outcome_known": True,
            }
        if outcome.status in {"complete", "failed"}:
            # Recovery payload deliberately excludes provider dumps/private upload
            # addresses. Original recipes and detailed provenance stay in Studio.
            result["outcome"] = {
                "status": outcome.status,
                "provider_job_id": outcome.provider_job_id,
                "media": result.get("media", []),
                "result": {
                    "media_id": outcome.result.get("media_id"),
                    "api_receipt_id": identifier,
                    "api_receipt_generation": job.result.get("api_receipt_generation"),
                    "recovered_from_shared_receipt": True,
                    "provenance": result.copy(),
                },
            }
        self.coordinator.update(
            identifier,
            outcome.status,
            provider_job_id=outcome.provider_job_id,
            result=result,
            generation=job.result.get("api_receipt_generation"),
            owner=job.lease_owner,
        )
        record_event(
            self.coordinator,
            identifier,
            "submission" if job.provider_job_id is None else "query",
            outcome.status,
        )

    def failure(self, job: Job, error: Exception) -> None:
        identifier = self.receipt_id(job)
        if not identifier:
            return
        current = self.coordinator.get(identifier)
        if current["state"] in {"complete", "failed", "cancelled"}:
            return
        phase = (
            "query"
            if job.provider_job_id or getattr(error, "request_method", None) in {"GET", "HEAD"}
            else "submission"
        )
        classification = classify_error(error, phase=phase, known_job=bool(job.provider_job_id))
        if job.kind == "video" and current["state"] == "reserved" and not job.provider_job_id:
            # A still-reserved receipt proves the send fence was never crossed.
            # Preserve this fact even if Studio crashes before committing the failure.
            classification["definitely_not_accepted"] = True
        event = {key: value for key, value in classification.items() if key != "upstream_rejection"}
        retry_after = getattr(error, "retry_after_seconds", None)
        if (
            isinstance(retry_after, (int, float))
            and not isinstance(retry_after, bool)
            and math.isfinite(retry_after)
            and retry_after > 0
        ):
            classification["retry_after_seconds"] = float(retry_after)
        if isinstance(error, ProviderCapacityWait) and not job.provider_job_id:
            # Admission/begin did not send a request. Retain the original
            # reservation; a stale waiter cannot change a newer submission.
            record_event(self.coordinator, identifier, phase, "waiting", event)
            return
        record_event(self.coordinator, identifier, phase, "failed", event)
        binding = job.request.get("api_binding", {})
        if classification["category"] == "rate_limit" and not self.video_completion(current):
            self.coordinator.cooldown(
                binding.get("resource_group") or self.coordinator.get(identifier)["resource_group"],
                getattr(error, "retry_after_seconds", None) or 60,
                model=binding.get("model") if job.kind == "image" else None,
            )
        if job.provider_job_id:
            self.coordinator.update(
                identifier,
                "running",
                provider_job_id=job.provider_job_id,
                result={"error": classification},
                generation=job.result.get("api_receipt_generation"),
                owner=job.lease_owner,
            )
        else:
            unknown = not getattr(error, "outcome_known", False) and (
                (not classification["definitely_not_accepted"] and current["state"] != "reserved")
                or isinstance(error, ProviderSubmissionUnknown)
            )
            self.coordinator.update(
                identifier,
                "unknown" if unknown else "failed",
                result={"error": classification},
                generation=job.result.get("api_receipt_generation"),
                owner=job.lease_owner,
            )

    def recovered(self, receipt: dict[str, Any]) -> JobOutcome:
        from stateweaver.studio.contracts import Media
        from stateweaver.studio.job_executor import JobOutcome

        if receipt["state"] in {"complete", "failed"}:
            stored = receipt["result"].get("outcome")
            if not stored:
                raise ProviderContractError(
                    "Known completed submission requires its saved material receipt"
                )
            result = {**stored["result"], "api_receipt_generation": receipt["generation"]}
            media = [Media.model_validate(item) for item in stored["media"]]
            provenance = dict(result.get("provenance", {}))
            if applied := provenance.get("applied_latent"):
                provenance["applied_latent_continuation"] = applied
            result["provenance"] = provenance
            if (
                stored["status"] == "failed"
                and result.get("provenance", {}).get("error", {}).get("code")
                == "remote_complete_delivery_invalid"
            ):
                result["delivery_failure"] = {
                    "remote_state": "complete",
                    "category": "validation",
                    **({"raw_media_id": media[0].id} if len(media) == 1 else {}),
                }
                result["error"] = "Video generation completed, but delivery validation failed."
            return JobOutcome(
                stored["status"],
                provider_job_id=stored["provider_job_id"],
                media=media,
                result=result,
            )
        completion = self.video_completion(receipt)
        return JobOutcome(
            "running",
            provider_job_id=receipt["provider_job_id"],
            result={
                "api_receipt_id": receipt["id"],
                "api_receipt_generation": receipt["generation"],
                **(
                    {"video_remote_completion_id": completion["provider_job_id"]}
                    if completion
                    else {}
                ),
            },
        )

    def counts(self) -> dict[str, int]:
        return self.coordinator.inflight(
            service_ids=[
                service.id for service in self.registry.services.values() if service.enabled
            ]
        )

    def shared_active(
        self, local: list[dict[str, Any]], routes: dict[str, Any]
    ) -> list[dict[str, Any]]:
        from datetime import UTC, datetime

        identities = {(i.get("project_id"), i.get("id")): i for i in local}
        released: set[tuple[Any, Any]] = set()
        result = list(local)
        groups = {
            "unresolved-video",
            *(r.get("resource_group", key) for key, r in routes.items()),
        }
        for receipt in self.coordinator.active(
            resource_groups=sorted(groups), operation="video_generation"
        ):
            context = receipt["context"]
            identity = (context.get("project_id"), context.get("job_id"))
            if self.video_completion(receipt):
                local_job = identities.get(identity, {})
                if (
                    context.get("workspace") == self.workspace
                    and local_job.get("api_receipt_id") == receipt["id"]
                    and local_job.get("provider_job_id") == receipt["provider_job_id"]
                ):
                    released.add(identity)
                continue
            if context.get("operation") != "video_generation" or identity in identities:
                continue
            physical = {
                "resource_group": receipt["resource_group"],
                "wire_hash": receipt["wire_hash"],
                "deployment_hash": context.get("deployment_hash"),
                "registry_version": receipt["service_version"],
                "service_id": receipt["service_id"],
                "model": receipt["model"],
            }
            if not physical["deployment_hash"] and receipt["service_version"] != "unknown":
                # Baseline receipts can predate deployment hashes. The same pinned
                # wire and explicit service version still occupy one known slot;
                # absent optional metadata must not block the entire GPU pool.
                deployments = {
                    route_physical_identity(route)[2]
                    for route in routes.values()
                    if route_physical_identity(route)[:2]
                    == (receipt["resource_group"], receipt["wire_hash"])
                    and route.get("registry_version") == receipt["service_version"]
                }
                if len(deployments) == 1:
                    physical["deployment_hash"] = next(iter(deployments))
            route_id = next(
                (
                    key
                    for key, route in routes.items()
                    if route_physical_identity(route) == route_physical_identity(physical)
                ),
                receipt["service_id"],
            )
            result.append(
                {
                    "id": context.get("job_id"),
                    "project_id": context.get("project_id"),
                    "route_id": route_id or "unresolved",
                    "route": physical,
                    "model": receipt["model"],
                    "status": "ambiguous" if receipt["state"] == "unknown" else "running",
                    "request": context.get("workload") or {},
                    "submitted_at": datetime.fromtimestamp(receipt["created"], UTC).isoformat(),
                }
            )
        return [item for item in result if (item.get("project_id"), item.get("id")) not in released]


def workload_request(request: dict[str, Any]) -> dict[str, Any]:
    """Numerical/structural routing inputs only, never creative text or media URIs."""
    return {
        "duration": request.get("duration", 5),
        "video_settings": {
            k: v
            for k, v in request.get("video_settings", {}).items()
            if k in {"short_edge", "aspect_ratio"}
        },
        "bindings": [{} for _ in request.get("bindings", [])],
        "video_inputs": [
            {"duration": i.get("duration", i.get("duration_seconds", 3))}
            for i in request.get("video_inputs", [])
        ],
        "audio_inputs": [{} for _ in request.get("audio_inputs", [])],
        "previous_end_state": {} if not request.get("previous_end_state") else {"present": True},
        "repair_plan": {"mode": (request.get("repair_plan") or {}).get("mode", "regenerate")},
        "latent_continuation": {
            k: v
            for k, v in request.get("latent_continuation", {}).items()
            if k in {"mode", "binding", "tail_frame_count"}
        },
    }
