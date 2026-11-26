"""Identifiers for idempotent project creation."""

from .memory import digest


def creation_project_id(operation_key: str) -> str:
    return digest(["studio-project-creation-v1", operation_key])[:32]
