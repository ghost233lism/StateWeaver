"""Read and consume only exact executive-authorized delegate preparations."""

from __future__ import annotations

from .contracts import Actor, Job, StudioProject
from .image_input_recovery import quarantine
from .memory import effective_entries


def authorized_preparation(
    project: StudioProject, actor: Actor, job: Job, *, clip_id: str | None = None
) -> bool:
    """External preparation access; existing own-actor permissions stay with callers."""
    if actor.role != "set_director" or not actor.set_id:
        return False
    owning_set = project.sets.get(actor.set_id)
    producer = project.actors.get(job.actor_id)
    if (
        owning_set is None
        or owning_set.actor_id != actor.id
        or job.kind != "reference"
        or job.status != "complete"
        or producer is None
        or producer.role != "material_executor"
        or not producer.delegate_task_id
    ):
        return False
    task = project.delegate_tasks.get(producer.delegate_task_id)
    if (
        task is None
        or task.status != "complete"
        or task.request.kind != "material_preparation"
        or task.actor_id != producer.id
        or job.id not in task.output_job_ids
    ):
        return False
    parent = project.actors.get(task.parent_actor_id)
    if parent is None or parent.role != "executive":
        return False

    source = job.request.get("source", {})
    binding = job.result.get("binding", {})
    provenance = job.result.get("provenance", {})
    if not all(isinstance(value, dict) for value in (source, binding, provenance)):
        return False
    revision_id = source.get("revision_id")
    state_id = source.get("state_id")
    asset_id = source.get("asset_id")
    asset = project.assets.get(str(asset_id))
    revision = project.references.get(str(revision_id))
    state = project.states.get(str(state_id))
    source_media = project.media.get(str(source.get("media_id")))
    output = project.media.get(str(job.result.get("media_id")))
    if (
        asset is None
        or revision is None
        or state is None
        or source_media is None
        or output is None
        or asset_id not in owning_set.asset_ids
        or state.asset_id != asset_id
        or revision.status != "selected"
        or revision.state_id != state.id
        or state.selected_revisions.get(revision.view) != revision.id
        or job.target_id != revision.id
        or job.request.get("revision_id") != revision.id
        or revision.media_id != source_media.id
        or not source_media.mime_type.startswith("image/")
        or not output.mime_type.startswith("image/")
        or not source_media.sha256
        or source.get("sha256") != source_media.sha256
        or binding != {"asset_id": asset_id, "state_id": state_id, "revision_id": revision_id}
        or job.result.get("prepared_reference_id") != job.id
        or output.owner_actor_id != producer.id
        or output.source_revision_id != revision.id
        or not output.sha256
        or provenance.get("output_sha256") != output.sha256
        or quarantine(project, source_media.id)
        or quarantine(project, output.id)
    ):
        return False
    source_fields = ("asset_id", "state_id", "revision_id", "media_id", "sha256")
    for name in ("requested_reference", "resolved_reference"):
        original = provenance.get(name)
        if not isinstance(original, dict) or any(
            original.get(field) != source.get(field) for field in source_fields
        ):
            return False

    original_clip_id = job.request.get("clip_id")
    clips = {
        identifier
        for identifier in owning_set.clip_ids
        if (clip := project.clips.get(identifier)) is not None
        and clip.set_id == actor.set_id
        and not clip.archived_at
    }
    for scope in (task.request.scope, task.allowed_scope):
        if (
            actor.set_id not in scope.set_ids
            or asset_id not in scope.asset_ids
            or state_id not in scope.state_ids
            or revision_id not in scope.revision_ids
        ):
            return False
        clips.intersection_update(scope.clip_ids)
    if original_clip_id not in clips or (clip_id is not None and clip_id not in clips):
        return False
    for entry in effective_entries(
        project,
        "global",
        context={"set_ids": [actor.set_id], "clip_ids": sorted(clips)},
    ):
        if (
            entry.actor_id != parent.id
            or entry.section != "directives"
            or actor.set_id not in entry.scope.set_ids
            or original_clip_id not in entry.scope.clip_ids
            or not any(source.kind == "job" and source.id == job.id for source in entry.sources)
        ):
            continue
        authorized_clips = clips.intersection(entry.scope.clip_ids)
        if authorized_clips and (clip_id is None or clip_id in authorized_clips):
            return True
    return False
