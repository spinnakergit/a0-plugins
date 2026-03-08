import json
import os
import subprocess
from pathlib import Path
from typing import Any, NoReturn, cast

import yaml

from plugin_resolution import (
    DEFAULT_MAX_PLUGINS,
    PLUGINS_DIR,
    REPO_ROOT,
    PluginResolutionError,
    get_plugin_names,
    is_valid_plugin_dirname,
)

INDEX_JSON_PATH = REPO_ROOT / "index.json"
AUTHORS_DIR = REPO_ROOT / "authors"
ALLOWED_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


class GenerateIndexError(Exception):
    pass


def _fail(msg: str) -> NoReturn:
    raise GenerateIndexError(msg)


def _run(cmd: list[str]) -> str:
    out = subprocess.check_output(cmd, cwd=REPO_ROOT)
    return out.decode("utf-8", errors="replace")


def _load_index() -> dict[str, Any]:
    if not INDEX_JSON_PATH.exists():
        return {"version": 1, "plugins": {}}

    try:
        loaded = json.loads(INDEX_JSON_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        _fail(f"Unable to parse {INDEX_JSON_PATH.name}: {e}")

    if not isinstance(loaded, dict):
        _fail(f"{INDEX_JSON_PATH.name} must contain a JSON object")

    if "plugins" not in loaded or not isinstance(loaded.get("plugins"), dict):
        loaded["plugins"] = {}

    if "version" not in loaded:
        loaded["version"] = 1

    return cast(dict[str, Any], loaded)


def _plugin_exists(plugin_name: str) -> bool:
    plugin_yaml = PLUGINS_DIR / plugin_name / "plugin.yaml"
    return plugin_yaml.exists()


def _full_scan_requested() -> bool:
    plugin_names_env = os.environ.get("PLUGIN_NAMES", "").strip()
    if plugin_names_env:
        return False

    before = os.environ.get("BEFORE_SHA", "").strip()
    return not before or set(before) == {"0"}


def _prune_removed_plugins(index: dict[str, Any]) -> int:
    plugins = index.get("plugins")
    if not isinstance(plugins, dict):
        return 0

    removed = 0
    for plugin_name in list(plugins.keys()):
        if not isinstance(plugin_name, str):
            continue
        if not plugin_name:
            continue
        if not is_valid_plugin_dirname(plugin_name):
            del plugins[plugin_name]
            removed += 1
            continue
        if not _plugin_exists(plugin_name):
            del plugins[plugin_name]
            removed += 1

    return removed


def _save_index(data: dict[str, Any]) -> None:
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
    # Ensure deterministic output order.
    data["plugins"] = {k: plugins[k] for k in sorted(plugins.keys())}
    INDEX_JSON_PATH.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _index_plugin_entry(plugin_name: str, meta: dict[str, Any]) -> dict[str, Any]:
    title = meta.get("title") if isinstance(meta.get("title"), str) else None
    description = meta.get("description") if isinstance(meta.get("description"), str) else None
    gh = meta.get("github") if isinstance(meta.get("github"), str) else None
    gh_str = gh if isinstance(gh, str) else ""
    author = _parse_github_owner_from_url(gh_str) if gh_str else None
    tags_val = meta.get("tags")
    tags: list[str] | None = None
    if isinstance(tags_val, list) and all(isinstance(t, str) for t in tags_val):
        tags = [t for t in tags_val if t.strip()]
    thumb_rel = _thumbnail_rel_path(plugin_name)
    thumb = _repo_file_url(thumb_rel) if isinstance(thumb_rel, str) else None
    screenshots_val = meta.get("screenshots")
    screenshots: list[str] = []
    if isinstance(screenshots_val, list) and all(isinstance(s, str) for s in screenshots_val):
        screenshots = [s.strip() for s in screenshots_val if s.strip()]
    return {
        "title": title,
        "description": description,
        "github": gh,
        "author": author,
        "tags": tags,
        "thumbnail": thumb,
        "screenshots": screenshots,
    }


def _upsert_index_plugin(
    index: dict[str, Any],
    plugin_name: str,
    meta: dict[str, Any],
    discussion_url: str | None,
) -> None:
    plugins = index.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
        index["plugins"] = plugins

    existing = plugins.get(plugin_name)
    existing_dict = existing if isinstance(existing, dict) else {}

    # Merge-based update: preserve any fields not owned by this generator.
    entry: dict[str, Any] = dict(existing_dict)

    generated = _index_plugin_entry(plugin_name, meta)
    entry["title"] = generated.get("title")
    entry["description"] = generated.get("description")
    entry["github"] = generated.get("github")
    entry["author"] = generated.get("author")
    entry["tags"] = generated.get("tags")
    entry["thumbnail"] = generated.get("thumbnail")
    entry["screenshots"] = generated.get("screenshots")
    if discussion_url is not None:
        entry["discussion"] = discussion_url

    plugins[plugin_name] = entry


def _thumbnail_rel_path(plugin_name: str) -> str | None:
    plugin_dir = PLUGINS_DIR / plugin_name
    if not plugin_dir.exists():
        return None
    for ext in ALLOWED_IMAGE_EXTS:
        p = plugin_dir / f"thumbnail{ext}"
        if p.exists():
            return p.relative_to(REPO_ROOT).as_posix()
    return None



def _read_authors() -> dict[str, Any]:
    if not AUTHORS_DIR.exists() or not AUTHORS_DIR.is_dir():
        return {}

    authors: dict[str, Any] = {}
    for author_dir in sorted(AUTHORS_DIR.iterdir(), key=lambda x: x.name):
        if not author_dir.is_dir():
            continue
        author_yaml = author_dir / "author.yaml"
        if not author_yaml.exists():
            continue
        try:
            loaded = yaml.safe_load(author_yaml.read_text(encoding="utf-8"))
        except Exception as e:
            _fail(f"Invalid author metadata in {author_yaml.relative_to(REPO_ROOT)}: {e}")

        if not isinstance(loaded, dict):
            _fail(f"{author_yaml.relative_to(REPO_ROOT)} must contain a YAML mapping/object")

        authors[author_dir.name] = loaded

    return authors


def _authors_changed() -> bool:
    before = os.environ.get("BEFORE_SHA", "").strip()
    after = os.environ.get("AFTER_SHA", "").strip()

    if not before or not after:
        return False

    changed = _run(["git", "diff", "--name-only", f"{before}..{after}"]).splitlines()
    return any(line.strip().startswith("authors/") for line in changed)


def _maybe_update_authors(index: dict[str, Any]) -> None:
    existing = index.get("authors")
    authors_missing = not isinstance(existing, dict)
    if not authors_missing and not _authors_changed():
        return

    index["authors"] = _read_authors()


def _repo_file_url(rel_path: str) -> str:
    repo_full = os.environ.get("GITHUB_REPOSITORY")
    if not repo_full or "/" not in repo_full:
        _fail("GITHUB_REPOSITORY is required (owner/repo)")
    owner, repo = repo_full.split("/", 1)

    ref = os.environ.get("GITHUB_REF_NAME") or "main"

    # Use raw.githubusercontent.com for direct file access.
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{rel_path.lstrip('/')}"


def _parse_github_owner_from_url(url: str) -> str | None:
    s = url.strip()
    if not s:
        return None

    # Support common formats:
    # - https://github.com/OWNER/REPO
    # - https://github.com/OWNER/REPO/
    # - git@github.com:OWNER/REPO.git
    # - OWNER/REPO
    s = s.removeprefix("https://")
    s = s.removeprefix("http://")
    if s.startswith("github.com/"):
        s = s[len("github.com/") :]
    if s.startswith("www.github.com/"):
        s = s[len("www.github.com/") :]

    if s.startswith("git@github.com:"):
        s = s[len("git@github.com:") :]

    s = s.strip("/")
    if s.endswith(".git"):
        s = s[: -len(".git")]

    parts = [p for p in s.split("/") if p]
    if len(parts) >= 2:
        owner = parts[0]
        return owner if owner else None
    return None


def _read_plugin_yaml(plugin_name: str) -> dict[str, Any]:
    plugin_yaml = PLUGINS_DIR / plugin_name / "plugin.yaml"
    if not plugin_yaml.exists():
        _fail(f"Missing plugin.yaml for plugin '{plugin_name}': {plugin_yaml.relative_to(REPO_ROOT)}")

    loaded: Any = None
    try:
        loaded = yaml.safe_load(plugin_yaml.read_text(encoding="utf-8"))
    except Exception as e:
        _fail(f"Invalid YAML for plugin '{plugin_name}': {e}")

    if not isinstance(loaded, dict):
        _fail(f"plugin.yaml for '{plugin_name}' must be a YAML mapping/object")

    return cast(dict[str, Any], loaded)


def main() -> int:
    repo_full = os.environ.get("GITHUB_REPOSITORY")

    if not repo_full or "/" not in repo_full:
        _fail("GITHUB_REPOSITORY is required")

    plugin_names = get_plugin_names()

    index = _load_index()
    index_before = json.dumps(index, sort_keys=True)

    if _full_scan_requested():
        removed = _prune_removed_plugins(index)
        if removed:
            print(f"Pruned {removed} removed plugins during full scan")

    if not plugin_names:
        _maybe_update_authors(index)
        index_after = json.dumps(index, sort_keys=True)
        if index_after != index_before:
            _save_index(index)
            print(f"Updated {INDEX_JSON_PATH.name}")
        print("No plugin changes detected; index left as is.")
        return 0

    updated = 0

    for plugin_name in plugin_names:
        if not _plugin_exists(plugin_name):
            plugins_obj = index.get("plugins")
            if isinstance(plugins_obj, dict) and plugin_name in plugins_obj:
                del plugins_obj[plugin_name]
                print(f"Removed from index (plugin deleted): {plugin_name}")
            else:
                print(f"Plugin deleted (not in index): {plugin_name}")
            continue

        meta = _read_plugin_yaml(plugin_name)
        existing_entry: dict[str, Any] = {}
        plugins_obj = index.get("plugins")
        if isinstance(plugins_obj, dict) and isinstance(plugins_obj.get(plugin_name), dict):
            existing_entry = cast(dict[str, Any], plugins_obj.get(plugin_name))

        discussion_url = existing_entry.get("discussion") if isinstance(existing_entry.get("discussion"), str) else None
        _upsert_index_plugin(index, plugin_name, meta, discussion_url)
        updated += 1

    _maybe_update_authors(index)

    index_after = json.dumps(index, sort_keys=True)
    if index_after != index_before:
        _save_index(index)
        print(f"Updated {INDEX_JSON_PATH.name}")

    print(f"Done. updated={updated} total={len(plugin_names)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GenerateIndexError, PluginResolutionError) as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)
