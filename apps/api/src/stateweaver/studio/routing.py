"""Completion-time estimates, separated by deployment/topology and input workload."""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from stateweaver.providers.registry import route_physical_identity
from stateweaver.providers.video_http_config import HTTP_VIDEO_PROTOCOL
from stateweaver.studio_media.latent import CONTINUATION_FRAMES, ENDPOINT_ANCHOR

from .contracts import Job
from .memory import digest


def supports_request(route: dict[str, Any], request: dict[str, Any]) -> bool:
    descriptor = request.get("latent_continuation", {})
    if not isinstance(descriptor, dict):
        return False
    mode = descriptor.get("mode", "off")
    return mode == "off" or (
        mode in str(route.get("latent_modes", "off")).split(",")
        and (
            route.get("latent_endpoint_only") is True
            if descriptor.get("binding") == "endpoint-only-v1"
            else str(descriptor.get("tail_frame_count", 72))
            in str(route.get("latent_tail_frame_counts", "72")).split(",")
        )
    )


class H3Readiness(TypedDict):
    default_ready: bool
    continuation_ready: bool
    anchored_edit_ready: bool
    missing: list[str]


def default_h3_readiness(route: dict[str, Any] | None) -> H3Readiness:
    """Report readiness for the enabled protocol and H3-specific capabilities."""
    if route is None:
        return {
            "default_ready": False,
            "continuation_ready": False,
            "anchored_edit_ready": False,
            "missing": ["workers"],
        }
    if route.get("protocol") == HTTP_VIDEO_PROTOCOL:
        return {
            "default_ready": True,
            "continuation_ready": False,
            "anchored_edit_ready": False,
            "missing": [],
        }
    mode = {"mode": ENDPOINT_ANCHOR}
    continuation = supports_request(
        route, {"latent_continuation": {**mode, "tail_frame_count": CONTINUATION_FRAMES}}
    )
    editing = supports_request(
        route, {"latent_continuation": {**mode, "binding": "endpoint-only-v1"}}
    )
    missing = []
    if ENDPOINT_ANCHOR not in str(route.get("latent_modes", "off")).split(","):
        missing.append(ENDPOINT_ANCHOR)
    if str(CONTINUATION_FRAMES) not in str(route.get("latent_tail_frame_counts", "72")).split(","):
        missing.append(f"tail-{CONTINUATION_FRAMES}")
    if route.get("latent_endpoint_only") is not True:
        missing.append("endpoint-only-v1")
    return {
        "default_ready": continuation and editing,
        "continuation_ready": continuation,
        "anchored_edit_ready": editing,
        "missing": missing,
    }


def profile_key(route: dict[str, Any]) -> str:
    parts = [route["id"], route.get("revision", "unknown"), route.get("topology", "unknown")]
    if (
        route.get("model", "MiniMax-H3-Sol") != "MiniMax-H3-Sol"
        or route.get("registry_version", "unknown") != "unknown"
    ):
        parts += [route.get("model"), route.get("registry_version"), route.get("base_url")]
    if route.get("protocol") == HTTP_VIDEO_PROTOCOL:
        parts += [HTTP_VIDEO_PROTOCOL, route["video_http"]]
    return digest(parts)


def workload_key(request: dict[str, Any]) -> str:
    settings = request.get("video_settings", {})
    return digest(
        [
            settings.get("short_edge", 768),
            settings.get("aspect_ratio", "16:9"),
            math.ceil(float(request.get("duration", 5)) / 5),
            len(request.get("reference_inputs", request.get("bindings", [])))
            - sum(
                bool(x.get("endpoint_coverage_alias")) for x in request.get("reference_inputs", [])
            ),
            bool(request.get("previous_end_state")),
            [
                round(float(v.get("duration_seconds", v.get("duration", 3))), 1)
                for v in request.get("video_inputs", [])
            ],
            len(request.get("audio_inputs", [])),
            request.get("latent_continuation", {}).get("mode", "off"),
        ]
        + (
            ["edit", request.get("latent_continuation", {}).get("binding", "tail")]
            if (request.get("repair_plan") or {}).get("mode") == "edit"
            or request.get("latent_continuation", {}).get("binding") == "endpoint-only-v1"
            else []
        )
    )


def workload_features(request: dict[str, Any]) -> dict[str, Any]:
    settings = request.get("video_settings", {})
    latent = request.get("latent_continuation") or {}
    return {
        "short_edge": int(settings.get("short_edge", 768)),
        "aspect_ratio": settings.get("aspect_ratio", "16:9"),
        "duration": float(request.get("duration", 5)),
        "images": len(request.get("reference_inputs", request.get("bindings", [])))
        - sum(bool(x.get("endpoint_coverage_alias")) for x in request.get("reference_inputs", []))
        + int(bool(request.get("previous_end_state"))),
        "video_seconds": sum(
            float(v.get("duration", v.get("duration_seconds", 0)))
            for v in request.get("video_inputs", [])
        ),
        "audio_seconds": sum(
            float(v.get("duration", v.get("duration_seconds", 0)))
            for v in request.get("audio_inputs", [])
        ),
        "latent_mode": latent.get("mode", "off"),
        "latent_binding": latent.get("binding", "tail"),
        "editing": (request.get("repair_plan") or {}).get("mode") == "edit",
    }


def workload_units(features: dict[str, Any]) -> float:
    # A transparent interpolation prior; exact measured workloads always win.
    return max(
        1.0,
        float(features.get("duration", 5))
        + 0.12 * float(features.get("video_seconds", 0))
        + 0.15 * int(features.get("images", 0))
        + 0.05 * float(features.get("audio_seconds", 0)),
    )


def timestamp(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return None


def estimate(
    route: dict[str, Any], request: dict[str, Any], samples: list[dict[str, Any]]
) -> dict[str, Any]:
    route_profile = profile_key(route)
    deployment = [
        sample
        for sample in samples
        if valid_sample(sample) and sample["profile_key"] == route_profile
    ]
    matching = []
    if deployment:
        workload = workload_key(request)
        matching = [sample for sample in deployment if sample["workload_key"] == workload]
    duration = float(request.get("duration", 5))
    features = workload_features(request)
    basis = "observed"
    if not matching:
        matching = [
            sample
            for sample in deployment
            if sample.get("workload")
            and all(
                sample["workload"].get(key) == features[key]
                for key in (
                    "short_edge",
                    "aspect_ratio",
                    "latent_mode",
                    "latent_binding",
                    "editing",
                )
            )
        ]
        basis = "similar_workload"
    if not matching:
        matching = [
            sample
            for sample in deployment
            if sample.get("workload")
            and all(
                sample["workload"].get(key) == features[key]
                for key in ("short_edge", "aspect_ratio")
            )
        ]
        basis = "deployment_extrapolation"
    if matching:
        # Oldest to newest; bounded exponential smoothing adapts without making a
        # lone unusually quick task erase the observed cost of a workload.
        generation, collection = 0.0, 0.0
        for index, sample in enumerate(
            sorted(matching, key=lambda s: timestamp(s["completed_at"]) or 0)[-32:]
        ):
            ratio = (
                workload_units(features) / workload_units(sample["workload"])
                if sample.get("workload")
                else duration / sample["duration"]
            )
            if basis == "deployment_extrapolation":
                ratio *= 1.15
            weight = 1.0 if index == 0 else 0.25
            generation = generation * (1 - weight) + sample["generation_seconds"] * ratio * weight
            collection = collection * (1 - weight) + sample["collection_seconds"] * weight
        return {
            "generation_seconds": generation,
            "collection_seconds": collection,
            "basis": basis,
            "sample_count": len(matching),
            "profile_sample_count": len(deployment),
        }
    prior = float(route.get("prior_seconds_per_second", 180))
    edge = float(request.get("video_settings", {}).get("short_edge", 768))
    return {
        "generation_seconds": max(1.0, duration * prior * (edge / 768) ** 2),
        "collection_seconds": 10.0,
        "basis": "cold_start",
        "sample_count": 0,
        "profile_sample_count": 0,
    }


def valid_sample(sample: dict[str, Any]) -> bool:
    if not isinstance(sample, dict):
        return False
    completed = timestamp(sample.get("completed_at"))
    workload = sample.get("workload")
    if workload is not None:
        if not isinstance(workload, dict):
            return False
        if any(
            key in workload and not finite_number(workload[key], minimum)
            for key, minimum in (
                ("duration", 0.001),
                ("short_edge", 1),
                ("images", 0),
                ("video_seconds", 0),
                ("audio_seconds", 0),
            )
        ):
            return False
        if (
            any(
                key in workload and (not isinstance(workload[key], str) or not workload[key])
                for key in ("aspect_ratio", "latent_mode", "latent_binding")
            )
            or "editing" in workload
            and not isinstance(workload["editing"], bool)
        ):
            return False
    return bool(
        all(
            isinstance(sample.get(key), str) and sample[key]
            for key in ("profile_key", "workload_key")
        )
        and completed is not None
        and math.isfinite(completed)
        and all(
            finite_number(sample.get(key), minimum)
            for key, minimum in (
                ("duration", 0.001),
                ("generation_seconds", 0.001),
                ("collection_seconds", 0),
            )
        )
    )


def finite_number(value: Any, minimum: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= minimum
    )


def calibration_samples(path: Path | None) -> list[dict[str, Any]]:
    """An optional small offline calibration file; live samples retain their DB audit."""
    if path is None or not path.is_file():
        return []
    try:
        value = json.loads(path.read_text())
        samples = value.get("samples", [])
        return [s for s in samples if valid_sample(s)][:64] if isinstance(samples, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def active_for_routes(
    active: list[dict[str, Any]], routes: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Map same-wire model aliases, retaining genuinely competing/unknown modes."""
    groups = {route_physical_identity(route)[0] for route in routes.values()}
    result = []
    for item in active:
        original = item.get("route") or (item.get("request") or {}).get("provider_route") or {}
        key = item.get("route_id")
        if not original and key in routes:
            original = routes[key]
        if not original:
            result.append({**item, "route_id": "unresolved", "route": {}})
            continue
        identity = route_physical_identity(original)
        if identity[0] not in groups and identity[0] != "unresolved-video":
            continue  # Independent physical pools are not project-wide blockers.
        matching = [k for k, route in routes.items() if route_physical_identity(route) == identity]
        selected = next(
            (k for k in matching if routes[k].get("model") == item.get("model")),
            matching[0] if matching else key,
        )
        result.append({**item, "route_id": selected or "unresolved", "route": original})
    return result


def route_occupancy(route: dict[str, Any], active: list[dict[str, Any]]) -> tuple[int, bool]:
    identity = route_physical_identity(route)
    count, competing = 0, False
    for item in active:
        original = item.get("route") or {}
        if not original:
            competing = True
            continue
        other = route_physical_identity(original)
        if other[0] == "unresolved-video":
            competing = True
        elif other == identity:
            count += 1
        elif other[0] == identity[0]:
            competing = True
    return count, competing


def timing_sample(project_id: str, job: Job) -> dict[str, Any] | None:
    route = job.request.get("provider_route")
    timings = job.result.get("timings", {})
    start, end = timestamp(job.submitted_at), timestamp(job.completed_at)
    if job.kind != "video" or job.status != "complete" or not route or start is None or end is None:
        return None
    collection = timings.get("collection_seconds")
    if (
        not isinstance(collection, (float, int))
        or not math.isfinite(collection)
        or collection < 0
        or end <= start + collection
    ):
        return None
    duration = float(job.request.get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        return None
    generation = end - start - collection
    metrics = timings.get("provider_metrics") or {}
    inference = metrics.get("inference_time_s") if isinstance(metrics, dict) else None
    if (
        isinstance(inference, (float, int))
        and math.isfinite(inference)
        and 0 < inference <= generation
    ):
        generation = inference + min(30.0, generation - inference)
    return {
        "project_id": project_id,
        "job_id": job.id,
        "route_id": route["id"],
        "profile_key": profile_key(route),
        "workload_key": workload_key(job.request),
        "duration": duration,
        "generation_seconds": generation,
        "collection_seconds": collection,
        "completed_at": job.completed_at,
        "workload": workload_features(job.request),
    }
