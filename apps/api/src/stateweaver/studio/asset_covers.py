"""Selected per-asset covers, independent of the paginated story-memory list."""

from sqlalchemy import text
from sqlalchemy.orm import Session


def asset_cover_states(session: Session, project_id: str) -> list[str]:
    # A recent state without images must not hide an older usable base state.
    # Check the exact selected view, its state and image before ranking. This is
    # one indexed batch query, not a full reference history read per asset.
    return list(
        session.scalars(
            text("""
        SELECT id FROM (
            SELECT state.id, row_number() OVER (
                PARTITION BY state.parent_id
                ORDER BY coalesce(json_extract(state.payload, '$.is_base'), 0) DESC,
                    state.story_time, state.id
            ) AS position
            FROM studio_entities AS state
            WHERE state.project_id = :project_id AND state.kind = 'states'
                AND EXISTS (
                    SELECT 1 FROM json_each(state.payload, '$.selected_revisions') AS chosen
                    JOIN studio_entities AS reference ON
                        reference.project_id = state.project_id
                        AND reference.kind = 'references' AND reference.id = chosen.value
                        AND reference.parent_id = state.id AND reference.status = 'selected'
                        AND json_extract(reference.payload, '$.view') = chosen.key
                    JOIN studio_entities AS media ON
                        media.project_id = state.project_id AND media.kind = 'media'
                        AND media.id = json_extract(reference.payload, '$.media_id')
                        AND json_extract(media.payload, '$.mime_type') LIKE 'image/%'
                )
        ) WHERE position = 1
    """),
            {"project_id": project_id},
        )
    )
