"""Share recent H3 capability probes without sharing route failures."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from stateweaver.providers.base import (
    ProviderContractError,
    ProviderExternalError,
    ProviderHttpRejectionError,
    ProviderHttpStatusError,
)
from stateweaver.studio_media.h3 import H3ProbeUnavailable

_CAPACITY_ERRORS = frozenset(
    {"authentication", "upstream", "network", "protocol", "probe_failed", "probe_in_progress"}
)


@dataclass(frozen=True, slots=True)
class VideoCapacityObservation:
    capacity: int
    profile: dict[str, str | int]
    observed_at: float
    fresh: bool = False
    error: str | None = None


def _probe_error(error: Exception) -> str:
    if isinstance(error, (ProviderHttpStatusError, ProviderHttpRejectionError)):
        if error.http_status in {401, 403}:
            return "authentication"
        return "upstream"
    if isinstance(error, H3ProbeUnavailable):
        return "upstream"
    if isinstance(error, ProviderExternalError):
        return "network"
    if isinstance(error, ProviderContractError):
        return "protocol"
    return "probe_failed"


class VideoCapacityCache:
    """One short-lived observation and one nonblocking probe lock per route."""

    def __init__(self, root: Path, ttl_seconds: float = 20) -> None:
        self.root = root
        self.ttl_seconds = ttl_seconds

    def _read(self, path: Path) -> VideoCapacityObservation | None:
        try:
            value = json.loads(path.read_text())
            capacity, stamp, profile = (
                value["capacity"],
                value["observed_at"],
                value["profile"],
            )
            if not isinstance(capacity, int) or capacity < 0:
                return None
            if not isinstance(stamp, (int, float)) or not isinstance(profile, dict):
                return None
            error = value["error"]
            if error is not None and (not isinstance(error, str) or error not in _CAPACITY_ERRORS):
                return None
            return VideoCapacityObservation(
                capacity,
                {k: v for k, v in profile.items() if isinstance(v, (str, int))},
                float(stamp),
                error=error,
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _recent(self, observation: VideoCapacityObservation | None) -> bool:
        return (
            observation is not None
            and 0 <= time.time() - observation.observed_at < self.ttl_seconds
        )

    @staticmethod
    def _probe(
        probe: Callable[[], tuple[int, dict[str, str | int]]],
    ) -> VideoCapacityObservation:
        try:
            capacity, profile = probe()
        except Exception as error:
            return VideoCapacityObservation(
                0, {}, time.time(), fresh=True, error=_probe_error(error)
            )
        return VideoCapacityObservation(capacity, profile, time.time(), fresh=True)

    def observe(
        self,
        key: str,
        probe: Callable[[], tuple[int, dict[str, str | int]]],
    ) -> VideoCapacityObservation:
        path = self.root / (key + ".json")
        observation = self._read(path)
        if self._recent(observation):
            assert observation is not None
            return observation
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            lock = (self.root / (key + ".lock")).open("a")
        except OSError:
            return self._probe(probe)
        with lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                observation = self._read(path)
                if self._recent(observation):
                    assert observation is not None
                    return observation
                # Another worker probes this route. Do not wait or mark other
                # routes unavailable, and never renew an expired observation.
                return VideoCapacityObservation(0, {}, 0, error="probe_in_progress")
            except OSError:
                return self._probe(probe)
            try:
                observation = self._read(path)
                if self._recent(observation):
                    assert observation is not None
                    return observation
                observation = self._probe(probe)
                temporary: str | None = None
                try:
                    with tempfile.NamedTemporaryFile("w", dir=self.root, delete=False) as stream:
                        temporary = stream.name
                        json.dump(
                            {
                                "capacity": observation.capacity,
                                "profile": observation.profile,
                                "observed_at": observation.observed_at,
                                "error": observation.error,
                            },
                            stream,
                            separators=(",", ":"),
                        )
                    os.replace(temporary, path)
                except OSError:
                    # Capability caching is an optimization, not a submission gate.
                    pass
                finally:
                    if temporary is not None:
                        with suppress(OSError):
                            Path(temporary).unlink(missing_ok=True)
                return observation
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
