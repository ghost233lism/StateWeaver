"""Reference-view recipes for a logical asset's single chronological state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from stateweaver.providers.base import AssetInput, ProviderContractError

from .images import Image25Adapter, ImageRequest
from .skills import load_media_skill

PERSON_VIEWS = ("close_up", "full_body", "three_view", "expression_grid")
OBJECT_VIEWS = ("three_view",)


def required_views(asset_kind: str) -> tuple[str, ...]:
    if asset_kind in {"person", "character", "人物"}:
        return PERSON_VIEWS
    if asset_kind in {"object", "prop", "物体", "道具"}:
        return OBJECT_VIEWS
    # Scenes and other categories are chosen explicitly during director preparation.
    return ()


def view_prompt(description: str, view: str, *, recipe: Mapping[str, Any] | None = None) -> str:
    config = recipe if recipe is not None else load_media_skill("image25-v1").config
    views = config.get("views")
    if not isinstance(views, dict) or not all(
        isinstance(v, str) and v.strip() for v in views.values()
    ):
        raise ProviderContractError("image skill has invalid view recipes")
    instruction = views.get(view, view)
    try:
        return str(config["prompt_template"]).format(
            description=description, instruction=instruction
        )
    except (IndexError, KeyError, ValueError):
        raise ProviderContractError("image skill prompt template is invalid") from None


def image_recipe_snapshot(
    view: str, *, asset_kind: str = "", orientation: str | None = None
) -> dict[str, Any]:
    """Pin the effective recipe/settings when a new durable image job is created."""
    skill = load_media_skill("image25-v1")
    profiles = skill.config.get("output_profiles", {})
    default = "landscape" if asset_kind in {"scene", "场景"} else "square"
    profile = skill.config.get("view_profiles", {}).get(view, orientation or default)
    size = profiles.get(profile)
    if not isinstance(size, str) or skill.config.get("quality") != "high":
        raise ProviderContractError("image skill requires high quality and a valid output profile")
    return {
        "version": 3,
        "skill_sha256": skill.sha256,
        "recipe": skill.config,
        "quality": "high",
        "size": size,
    }


def image_view_request(description: str, view: str) -> ImageRequest:
    snapshot = image_recipe_snapshot(view)
    return ImageRequest(
        view_prompt(description, view, recipe=snapshot["recipe"]),
        size=snapshot["size"],
        quality=snapshot["quality"],
    )


@dataclass(frozen=True, slots=True)
class AssetViewGenerator:
    adapter: Image25Adapter

    def generate_views(
        self,
        description: str,
        *,
        views: tuple[str, ...],
        existing: Mapping[str, AssetInput] | None = None,
        anchor: AssetInput | None = None,
    ) -> dict[str, AssetInput]:
        """Generate a coherent set by deriving every further view from an anchor.

        Existing decoded references can be used to resume a partial set. Persistence,
        parent revision IDs, selection and usage bindings stay with the caller.
        """
        result = dict(existing or {})
        if anchor is None and result:
            anchor = next(iter(result.values()))
        for view in views:
            if view in result:
                continue
            request = image_view_request(description, view)
            generated = (
                self.adapter.edit(request, (anchor,)) if anchor else self.adapter.generate(request)
            )
            material = self.adapter.materialize(generated.assets[0], name=f"{view}.png")
            result[view] = material
            if anchor is None:
                anchor = material
        return result
