import os
import subprocess
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"
DEFAULT_MAX_PLUGINS = 1000


class PluginResolutionError(Exception):
    pass


def _fail(msg: str) -> NoReturn:
    raise PluginResolutionError(msg)


def _run(cmd: list[str]) -> str:
    out = subprocess.check_output(cmd, cwd=REPO_ROOT)
    return out.decode("utf-8", errors="replace")


def _is_zero_sha(sha: str | None) -> bool:
    if not sha:
        return True
    s = sha.strip()
    return bool(s) and set(s) == {"0"}


def _git_diff_names(before: str, after: str) -> list[str]:
    raw = _run(["git", "diff", "--name-only", f"{before}..{after}"])
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _git_all_plugin_paths(commit: str) -> list[str]:
    raw = _run(["git", "ls-tree", "-r", "--name-only", commit, "--", "plugins"])
    return [line.strip() for line in raw.splitlines() if line.strip()]


def get_plugin_names() -> list[str]:
    """
    Determines which plugins to process based on environment variables:
    - PLUGIN_NAMES: Comma-separated list of explicit plugin names (highest precedence)
    - BEFORE_SHA & AFTER_SHA: Git diff to find changed plugins
    - If SHAs are missing or '0000', returns ALL plugins

    Also applies MAX_PLUGINS and START_FROM constraints.
    """
    plugin_names_env = os.environ.get("PLUGIN_NAMES", "").strip()
    if plugin_names_env:
        plugin_names = sorted(list(set(n.strip() for n in plugin_names_env.split(",") if n.strip())))
    else:
        before = os.environ.get("BEFORE_SHA", "").strip()
        after = os.environ.get("AFTER_SHA", "").strip()

        if not before or _is_zero_sha(before):
            # If after is empty, use 'HEAD' as fallback for local testing, though actions will set it
            paths = _git_all_plugin_paths(after or "HEAD")
        else:
            paths = _git_diff_names(before, after)

        plugin_names_set = set()
        for p in paths:
            parts = Path(p).parts
            if len(parts) >= 2 and parts[0] == "plugins":
                plugin_names_set.add(parts[1])

        plugin_names = [n for n in sorted(plugin_names_set) if n and not n.startswith("_")]

    if not plugin_names:
        return []

    start_from = os.environ.get("START_FROM", "").strip()
    if start_from:
        if start_from in plugin_names:
            idx = plugin_names.index(start_from)
            plugin_names = plugin_names[idx:]
            print(f"START_FROM={start_from} specified. Skipping first {idx} plugins.")
        else:
            print(f"WARN: START_FROM={start_from} not found in affected plugins list. Ignoring it.")

    max_plugins = int(os.environ.get("MAX_PLUGINS", str(DEFAULT_MAX_PLUGINS)))
    if len(plugin_names) > max_plugins:
        _fail(
            f"Detected {len(plugin_names)} plugins in scope, which exceeds MAX_PLUGINS={max_plugins}. "
            "Increase MAX_PLUGINS or run multiple smaller pushes."
        )

    return plugin_names
