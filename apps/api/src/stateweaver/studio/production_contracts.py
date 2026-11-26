"""Source-grounded scene requirements for Studio planning."""

from __future__ import annotations

from .contracts import Actor, MemoryEntry, ProductionSet, StudioProject
from .memory import require


def missing_planned_events(p: StudioProject, production_set: ProductionSet) -> list[str]:
    """Check required handoff coverage, without requiring footage or output registration."""
    planned = {
        event_id
        for identifier in production_set.clip_ids
        if identifier in p.clips and not p.clips[identifier].archived_at
        for event_id in p.clips[identifier].story_event_ids
    }
    return [
        identifier
        for identifier in production_set.story_event_ids
        if identifier not in planned
        and ((event := p.story_events.get(identifier)) is None or event.completion_required)
    ]


def validate_state_boundary(p: StudioProject, actor: Actor, entry: MemoryEntry) -> None:
    boundary = entry.state_boundary
    assert boundary is not None
    require(actor.role == "executive", "Only the executive can resolve a source state boundary")
    require(
        entry.kind == "constraint" and entry.basis == "fact" and entry.section == "directives",
        "A source boundary is a source-backed directive, never an observed seam",
    )
    prior = p.clips.get(boundary.prior_clip_id)
    target = p.clips.get(boundary.target_clip_id)
    require(
        prior and target and not prior.archived_at and not target.archived_at,
        "Boundary Clips must be active",
    )
    assert prior is not None and target is not None
    require(prior.id != target.id, "A source boundary must name two distinct active Clips")
    require(
        boundary.prior_event_id in prior.story_event_ids
        and boundary.target_event_id in target.story_event_ids,
        "Boundary events must belong to their specified Clips",
    )
    before = p.story_events[boundary.prior_event_id]
    after = p.story_events[boundary.target_event_id]
    require(before.order < after.order, "A source boundary must advance story order")
    before_facts, after_facts = set(before.source_fact_ids), set(after.source_fact_ids)
    if prior.set_id == target.set_id:
        require(
            bool(before_facts - after_facts) and bool(after_facts - before_facts),
            "A same-Set source boundary needs distinct original source facts on both sides",
        )
    require(
        set(entry.scope.clip_ids) == {prior.id, target.id}
        and set(entry.scope.event_ids) == {before.id, after.id},
        "Boundary memory must scope exactly its two Clips and events",
    )
    keys = boundary.state_keys
    require(
        len(keys) == len(set(keys))
        and all(k.strip() and k in before.results and k in after.preconditions for k in keys),
        "Boundary state keys must name specific prior results and target preconditions",
    )
    facts = before_facts | after_facts
    cited = {source.id for source in entry.sources if source.kind == "screenplay_fact"}
    require(
        before.source_fact_ids and after.source_fact_ids and facts <= cited,
        "Cite both original source events for the authorized time/place advance",
    )
    require(
        entry.reason.strip(),
        "A source boundary needs the executive interpretation and retained limitations",
    )
