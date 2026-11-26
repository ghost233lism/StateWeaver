"""Read-only library covers from selected footage or selected reference images."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
from datetime import datetime
from threading import RLock

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from stateweaver.studio.contracts import Media

CoverKey = tuple[str, int, datetime]


class ProjectCoverCache:
    """Bounded, process-local covers pinned to the project's read snapshot.

    The timestamp also fences runtime writes which do not increment the business
    revision. Concurrent cold requests share a lookup, never a generation task.
    """

    def __init__(self, capacity: int = 512) -> None:
        self.capacity = capacity
        self._values: OrderedDict[CoverKey, Media | None] = OrderedDict()
        self._pending: dict[CoverKey, Future[Media | None]] = {}
        self._lock = RLock()

    def get(self, session: Session, versions: dict[str, tuple[int, datetime]]) -> dict[str, Media]:
        values: dict[str, Media | None] = {}
        waiting: dict[CoverKey, Future[Media | None]] = {}
        owned: dict[CoverKey, Future[Media | None]] = {}
        with self._lock:
            for identifier, (revision, stamp) in versions.items():
                key = (identifier, revision, stamp)
                if key in self._values:
                    values[identifier] = self._values[key]
                    self._values.move_to_end(key)
                else:
                    future = self._pending.get(key)
                    if future is None:
                        future = Future()
                        self._pending[key] = future
                        owned[key] = future
                    waiting[key] = future
        if owned:
            try:
                # SQL and Future waits remain outside the cache lock. Different
                # pages can load independently; overlapping pages cannot deadlock.
                covers = project_covers(session, [key[0] for key in owned])
                with self._lock:
                    for key in owned:
                        self._values[key] = covers.get(key[0])
                        self._values.move_to_end(key)
                    while len(self._values) > self.capacity:
                        self._values.popitem(last=False)
                for key, future in owned.items():
                    future.set_result(covers.get(key[0]))
            except BaseException as error:
                for future in owned.values():
                    future.set_exception(error)
                raise
            finally:
                with self._lock:
                    for key in owned:
                        self._pending.pop(key, None)
        for key, future in waiting.items():
            values[key[0]] = future.result()
        # Response consumers must not mutate another request's cached Media.
        return {
            identifier: media.model_copy(deep=True)
            for identifier, media in values.items()
            if media is not None
        }


def project_covers(session: Session, project_ids: list[str]) -> dict[str, Media]:
    """Two bounded batch queries; never load project aggregates or rejected videos."""
    if not project_ids:
        return {}
    # Both levels use their existing parent/order index, so LIMIT can stop at
    # the first usable Clip/Set without parsing later planning histories. SQLite
    # CROSS JOIN keeps candidates/media from becoming the outer scan. LIMIT -1
    # prevents flattening: the outer LIMIT consumes the ordered coroutine lazily,
    # without evaluating each selected candidate twice to exclude empty Sets.
    # Only static SQL is interpolated; project IDs remain bound.
    first_clip = """
        SELECT media.payload
        FROM studio_entities AS clip INDEXED BY ix_studio_entity_parent
        LEFT JOIN studio_display_entities AS display ON
            display.project_id = clip.project_id AND display.kind = 'clips'
            AND display.id = clip.id AND display.version = clip.version
        CROSS JOIN studio_entities AS candidate ON
            candidate.project_id = clip.project_id AND candidate.kind = 'candidates'
            AND candidate.parent_id = clip.id
            AND candidate.id = json_extract(coalesce(display.payload, clip.payload),
                '$.accepted_candidate_id')
        CROSS JOIN studio_entities AS media ON
            media.project_id = clip.project_id AND media.kind = 'media'
            AND media.id = coalesce(
                json_extract(candidate.payload,
                    '$.confirmed_end_state.end_state.frame_media_id'),
                json_extract(candidate.payload, '$.end_state.frame_media_id')
            )
            AND json_extract(media.payload, '$.mime_type') LIKE 'image/%'
        WHERE clip.project_id = production_set.project_id AND clip.kind = 'clips'
            AND clip.parent_id = production_set.id
            AND json_extract(coalesce(display.payload, clip.payload), '$.archived_at') IS NULL
        ORDER BY clip.sort_key, clip.id
        LIMIT 1
    """
    frames = text(f"""
        SELECT project.id AS project_id, (
            SELECT frame.media FROM (
                SELECT ({first_clip}) AS media
                FROM studio_entities AS production_set
                WHERE production_set.project_id = project.id AND production_set.kind = 'sets'
                    AND production_set.parent_id IS NULL
                ORDER BY production_set.sort_key, production_set.id
                LIMIT -1
            ) AS frame
            WHERE frame.media IS NOT NULL
            LIMIT 1
        ) AS media
        FROM studio_projects AS project
        WHERE project.id IN :project_ids
    """).bindparams(bindparam("project_ids", expanding=True))
    covers = {
        row.project_id: Media.model_validate_json(row.media)
        for row in session.execute(frames, {"project_ids": project_ids})
        if row.media is not None
    }
    missing = [identifier for identifier in project_ids if identifier not in covers]
    if not missing:
        return covers
    # Order selected references before resolving media; row_number over the join
    # previously loaded every selected image even though only one was displayed.
    references = text("""
        SELECT project.id AS project_id, (
            SELECT media.payload
            FROM (
                SELECT reference.payload
                FROM studio_entities AS reference
                WHERE reference.project_id = project.id AND reference.kind = 'references'
                    AND reference.status = 'selected'
                ORDER BY reference.sort_key, reference.id
                LIMIT -1
            ) AS selected
            CROSS JOIN studio_entities AS media ON
                media.project_id = project.id AND media.kind = 'media'
                AND media.id = json_extract(selected.payload, '$.media_id')
                AND json_extract(media.payload, '$.mime_type') LIKE 'image/%'
            LIMIT 1
        ) AS media
        FROM studio_projects AS project
        WHERE project.id IN :project_ids
    """).bindparams(bindparam("project_ids", expanding=True))
    covers.update(
        {
            row.project_id: Media.model_validate_json(row.media)
            for row in session.execute(references, {"project_ids": missing})
            if row.media is not None
        }
    )
    return covers
