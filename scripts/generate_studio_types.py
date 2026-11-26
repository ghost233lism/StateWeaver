"""Generate the studio TypeScript contract from Pydantic, without starting runtime."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI
from pydantic.json_schema import models_json_schema
from stateweaver.studio.contracts import (
    BodyChunk,
    CandidateComparison,
    ClipPreparationUpdate,
    ClipReadiness,
    CommandResult,
    CreateProject,
    EntityLabelBatch,
    EntityPage,
    EventBatch,
    ProductionStatus,
    ProjectList,
    SetDependencyReport,
    SetPreview,
    StudioProject,
    StudioSnapshot,
    ToolCommand,
    TranscriptPage,
    UserAction,
    WorkspaceLayout,
    WorkspaceLayoutUpdate,
)

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "apps/web"
TARGET = WEB_ROOT / "src/generated/studio-schema.ts"
OPENAPI_TYPESCRIPT_VERSION = "7.13.0"


class ContractGenerationError(RuntimeError):
    """Raised when the checked-in Studio contract cannot be generated safely."""


def _verify_pinned_generator() -> Path:
    manifest = json.loads((WEB_ROOT / "package.json").read_text(encoding="utf-8"))
    declared = manifest.get("devDependencies", {}).get("openapi-typescript")
    if declared != OPENAPI_TYPESCRIPT_VERSION:
        raise ContractGenerationError(
            "apps/web/package.json must pin openapi-typescript exactly to "
            f"{OPENAPI_TYPESCRIPT_VERSION}; found {declared!r}"
        )

    installed_manifest = WEB_ROOT / "node_modules/openapi-typescript/package.json"
    if not installed_manifest.is_file():
        raise ContractGenerationError(
            "openapi-typescript is not installed; run "
            "`corepack pnpm install --frozen-lockfile` from the repository root"
        )
    installed = json.loads(installed_manifest.read_text(encoding="utf-8")).get("version")
    if installed != OPENAPI_TYPESCRIPT_VERSION:
        raise ContractGenerationError(
            "installed openapi-typescript version does not match the pin: "
            f"expected {OPENAPI_TYPESCRIPT_VERSION}, found {installed!r}; run "
            "`corepack pnpm install --frozen-lockfile` from the repository root"
        )

    executable = WEB_ROOT / "node_modules/.bin/openapi-typescript"
    if not executable.is_file():
        raise ContractGenerationError(f"openapi-typescript executable is missing: {executable}")
    return executable


def _generate_typescript(executable: Path, schema: Path, output: Path) -> None:
    completed = subprocess.run(
        (str(executable), str(schema), "--output", str(output), "--alphabetize"),
        cwd=ROOT,
        env={**os.environ, "NO_COLOR": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
        raise ContractGenerationError(f"openapi-typescript failed: {detail}")
    if not output.is_file() or not output.read_bytes().strip():
        raise ContractGenerationError("openapi-typescript produced an empty contract")


def generate(*, check: bool) -> int:
    executable = _verify_pinned_generator()
    _, schema = models_json_schema(
        [
            (model, "validation")
            for model in (
                StudioProject,
                ProjectList,
                ProductionStatus,
                SetPreview,
                SetDependencyReport,
                EventBatch,
                CreateProject,
                UserAction,
                ToolCommand,
                StudioSnapshot,
                CommandResult,
                TranscriptPage,
                EntityLabelBatch,
                EntityPage,
                BodyChunk,
                CandidateComparison,
                ClipReadiness,
                ClipPreparationUpdate,
                WorkspaceLayout,
                WorkspaceLayoutUpdate,
            )
        ],
        ref_template="#/components/schemas/{model}",
    )
    api = FastAPI(title="StateWeaver Studio", version="2.0.0").openapi()
    api["components"] = {"schemas": schema["$defs"]}
    with tempfile.TemporaryDirectory(prefix="stateweaver-studio-contract-") as directory:
        source = Path(directory) / "schema.json"
        destination = Path(directory) / "studio-schema.ts"
        source.write_text(json.dumps(api), encoding="utf-8")
        _generate_typescript(executable, source, destination)
        expected = destination.read_bytes()
        if check and (not TARGET.is_file() or TARGET.read_bytes() != expected):
            print(
                f"Generated Studio contract is stale or missing: {TARGET.relative_to(ROOT)}. "
                "Run `corepack pnpm --dir apps/web generate:studio-types`.",
                file=sys.stderr,
            )
            return 1
        if not check:
            TARGET.parent.mkdir(parents=True, exist_ok=True)
            TARGET.write_bytes(expected)
            print(f"Generated {TARGET.relative_to(ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail without modifying files when the committed contract differs from Pydantic",
    )
    args = parser.parse_args()
    try:
        return generate(check=args.check)
    except (ContractGenerationError, OSError, ValueError) as exc:
        print(f"Studio contract generation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
