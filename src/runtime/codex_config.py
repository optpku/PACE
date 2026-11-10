"""Per-invocation Codex settings for PACE-owned mathematical roles."""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def isolated_codex_args(workdir: Path) -> list[str]:
    """Keep authentication in CODEX_HOME while excluding personal context.

    Provider options supplied explicitly by codex_wrapper.sh remain CLI
    overrides. Neither the user's files nor their Codex settings are changed.
    """
    root = Path(workdir).resolve()
    codex_directory = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    skill_roots = {codex_directory / "skills", Path.home() / ".agents/skills"}
    skill_roots.update(parent / ".agents/skills" for parent in (root, *root.parents))
    # The enablement key is the SKILL.md file, not its containing directory.
    # Disable discovered standalone skills; PACE supplies its own saved Skills
    # explicitly through role prompts rather than Codex's personal catalog.
    skills = sorted({str(path.resolve()) for directory in skill_roots
                     if directory.is_dir() for path in directory.rglob("SKILL.md")})
    disabled_skills = "[" + ",".join(
        "{path=" + json.dumps(path, ensure_ascii=False) + ",enabled=false}"
        for path in skills
    ) + "]"
    args = ["--ignore-user-config"]
    settings = {
        "features.memories": "false",
        "memories.use_memories": "false",
        "memories.generate_memories": "false",
        "features.plugins": "false",
        "features.apps": "false",
        "features.hooks": "false",
        "features.multi_agent": "false",
        "project_doc_max_bytes": "0",
        "project_root_markers": "[]",
        "skills.config": disabled_skills,
    }
    for key, value in settings.items():
        args.extend(["-c", f"{key}={value}"])
    return args


def check_codex_isolation(executable: str) -> dict[str, Any]:
    """Check local CLI capabilities without authentication or inference."""
    row = {"name": "codex_isolation", "ok": False, "path": executable,
           "detail": "update Codex CLI: PACE requires --ignore-user-config and context-isolation settings"}
    try:
        result = subprocess.run([executable, "exec", "--help"], capture_output=True,
                                text=True, timeout=15, stdin=subprocess.DEVNULL, check=False)
        if result.returncode != 0 or "--ignore-user-config" not in result.stdout:
            return row
        result = subprocess.run([executable, "features", "list"], capture_output=True,
                                text=True, timeout=15, stdin=subprocess.DEVNULL, check=False)
        names = {line.split()[0] for line in result.stdout.splitlines() if line.split()}
        required = {"memories", "plugins", "apps", "hooks", "multi_agent"}
        if result.returncode == 0 and required <= names:
            row.update(ok=True, detail="supports isolated role calls; authentication is reused")
    except (OSError, subprocess.SubprocessError) as exc:
        row["detail"] = f"cannot check Codex isolation ({type(exc).__name__}); update Codex CLI"
    return row


@contextmanager
def isolated_codex_environment(
    environment: dict[str, str], directory: Path
) -> dict[str, str]:
    """Reuse only authentication in an empty per-call configuration home."""
    source = Path(environment.get("CODEX_HOME") or Path.home() / ".codex")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    authentication = source / "auth.json"
    original = authentication.read_bytes() if authentication.is_file() else None
    local = directory / "auth.json"
    if original is not None:
        local.symlink_to(authentication.resolve())
    try:
        yield {**environment, "CODEX_HOME": str(directory)}
    finally:
        # Some CLI versions replace auth.json atomically, replacing the symlink.
        # Preserve a normal credential refresh without overwriting a newer login.
        if original is not None and local.is_file() and not local.is_symlink():
            refreshed = local.read_bytes()
            if refreshed != original and authentication.read_bytes() == original:
                temporary = authentication.with_name(f".pace-auth-{directory.name}.tmp")
                try:
                    with temporary.open("xb") as stream:
                        os.chmod(temporary, 0o600)
                        stream.write(refreshed)
                    os.replace(temporary, authentication)
                finally:
                    temporary.unlink(missing_ok=True)


def retry_codex_args() -> list[str]:
    """One internal recovery profile; custom wrapper endpoints remain selected."""
    custom = os.environ.get("PACE_CODEX_AUTH_MODE") == "api_key"
    provider = (
        os.environ.get("PACE_CODEX_PROVIDER_NAME", "pace")
        if custom
        else "pace_openai"
    )
    settings = {}
    if not custom:
        settings.update(
            {
                "model_provider": json.dumps(provider),
                f"model_providers.{provider}.name": '"OpenAI"',
                f"model_providers.{provider}.wire_api": '"responses"',
                f"model_providers.{provider}.requires_openai_auth": "true",
                f"model_providers.{provider}.supports_websockets": "true",
            }
        )
    settings.update(
        {
            f"model_providers.{provider}.request_max_retries": "8",
            f"model_providers.{provider}.stream_max_retries": "20",
        }
    )
    return [
        item for key, value in settings.items() for item in ("-c", f"{key}={value}")
    ]
