import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NoReturn, cast

import yaml
from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]

ALLOWED_YAML_KEYS = {"title", "description", "github", "tags", "screenshots"}
REQUIRED_YAML_KEYS = {"title", "description", "github"}
ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_IMAGE_BYTES = 20 * 1024
MAX_SCREENSHOT_BYTES = 2 * 1024 * 1024
MAX_SCREENSHOTS = 5
MAX_SCREENSHOT_URL_LENGTH = 200
MAX_TAGS = 5
MAX_TAG_LENGTH = 30
THUMBNAIL_BASENAME = "thumbnail"
MAX_TITLE_LENGTH = 50
MAX_DESCRIPTION_LENGTH = 500
MAX_PLUGIN_YAML_CHARS = 2000
PLUGIN_DIRNAME_PATTERN = re.compile(r"^[a-z0-9_]+$")


class ValidationError(Exception):
    pass


def _is_valid_plugin_dirname(plugin_name: str) -> bool:
    return bool(PLUGIN_DIRNAME_PATTERN.fullmatch(plugin_name))


def _run(cmd: list[str]) -> str:
    out = subprocess.check_output(cmd, cwd=REPO_ROOT)
    return out.decode("utf-8", errors="replace")


def _run_bytes(cmd: list[str]) -> bytes:
    return subprocess.check_output(cmd, cwd=REPO_ROOT)


def _git_ls_tree_names(commit: str, path: str) -> list[str]:
    raw = _run(["git", "ls-tree", "-r", "--name-only", commit, "--", path])
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _git_show_bytes(commit: str, file_path: str) -> bytes:
    try:
        return _run_bytes(["git", "show", f"{commit}:{file_path}"])
    except subprocess.CalledProcessError as e:
        _fail(f"Unable to read {file_path} at {commit}: {e}")


def _get_changed_files(base_sha: str, head_sha: str) -> list[tuple[str, str]]:
    raw = _run(["git", "diff", "--name-status", f"{base_sha}..{head_sha}"])
    changes: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        changes.append((parts[0], parts[-1]))
    return changes


def _get_affected_paths(base_sha: str, head_sha: str) -> list[str]:
    raw = _run(["git", "diff", "--name-status", f"{base_sha}..{head_sha}"])
    affected: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        for p in parts[1:]:
            p = p.strip()
            if p:
                affected.append(p)
    return affected


def _fail(msg: str) -> NoReturn:
    raise ValidationError(msg)


def _validate_remote_screenshot_url(plugin_yaml_path: str, screenshot_url: str) -> None:
    if len(screenshot_url) > MAX_SCREENSHOT_URL_LENGTH:
        _fail(
            f"{plugin_yaml_path} screenshot URL exceeds {MAX_SCREENSHOT_URL_LENGTH} characters: {screenshot_url}"
        )

    parsed = urllib.parse.urlparse(screenshot_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        _fail(f"{plugin_yaml_path} screenshot URL must be a valid http(s) URL: {screenshot_url}")

    ext = Path(parsed.path).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        _fail(
            f"{plugin_yaml_path} screenshot URL must end with one of {sorted(ALLOWED_IMAGE_EXTS)}: {screenshot_url}"
        )

    req = urllib.request.Request(
        screenshot_url,
        headers={"User-Agent": "a0-plugins-validator"},
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            total = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SCREENSHOT_BYTES:
                    _fail(
                        f"{plugin_yaml_path} screenshot exceeds max size {MAX_SCREENSHOT_BYTES} bytes: {screenshot_url}"
                    )
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        _fail(f"Unable to fetch screenshot URL ({e.code}) {screenshot_url}: {body[:300]}")
    except Exception as e:
        _fail(f"Unable to fetch screenshot URL {screenshot_url}: {e}")


def _validate_yaml_bytes(plugin_yaml_path: str, content: bytes) -> None:
    try:
        yaml_text = content.decode("utf-8", errors="strict")
    except Exception as e:
        _fail(f"Invalid UTF-8 in {plugin_yaml_path}: {e}")

    if len(yaml_text) > MAX_PLUGIN_YAML_CHARS:
        _fail(
            f"{plugin_yaml_path} must be at most {MAX_PLUGIN_YAML_CHARS} characters"
        )

    loaded: Any = None
    try:
        loaded = yaml.safe_load(yaml_text)
    except Exception as e:
        _fail(f"Invalid YAML in {plugin_yaml_path}: {e}")

    if not isinstance(loaded, dict):
        _fail(f"{plugin_yaml_path} must contain a YAML mapping/object")

    data = cast(dict[str, Any], loaded)

    keys = set(data.keys())
    extra = keys - ALLOWED_YAML_KEYS
    missing = REQUIRED_YAML_KEYS - keys

    if extra:
        _fail(
            f"{plugin_yaml_path} contains unsupported fields: {sorted(extra)}. "
            f"Allowed fields are: {sorted(ALLOWED_YAML_KEYS)}"
        )
    if missing:
        _fail(f"{plugin_yaml_path} is missing required fields: {sorted(missing)}")

    for k in REQUIRED_YAML_KEYS:
        v = data.get(k)
        if not isinstance(v, str) or not v.strip():
            _fail(f"{plugin_yaml_path} field '{k}' must be a non-empty string")

    title = data.get("title")
    if isinstance(title, str) and len(title) > MAX_TITLE_LENGTH:
        _fail(f"{plugin_yaml_path} field 'title' must be at most {MAX_TITLE_LENGTH} characters")

    description = data.get("description")
    if isinstance(description, str) and len(description) > MAX_DESCRIPTION_LENGTH:
        _fail(f"{plugin_yaml_path} field 'description' must be at most {MAX_DESCRIPTION_LENGTH} characters")

    github = data.get("github")
    if isinstance(github, str) and not re.match(r"^https?://", github.strip()):
        _fail(f"{plugin_yaml_path} field 'github' must be a valid http(s) URL")
    if isinstance(github, str):
        _validate_github_repo(github.strip())

    if "tags" in data:
        tags = data.get("tags")
        if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip() for t in tags):
            _fail(f"{plugin_yaml_path} field 'tags' must be a list of non-empty strings")
        tags_list = cast(list[str], tags)
        if len(tags_list) > MAX_TAGS:
            _fail(f"{plugin_yaml_path} field 'tags' must contain at most {MAX_TAGS} entries")
        for tag in tags_list:
            if len(tag) > MAX_TAG_LENGTH:
                _fail(f"{plugin_yaml_path} tag '{tag}' exceeds max length {MAX_TAG_LENGTH}")

    if "screenshots" in data:
        screenshots = data.get("screenshots")
        if not isinstance(screenshots, list) or not all(isinstance(s, str) and s.strip() for s in screenshots):
            _fail(f"{plugin_yaml_path} field 'screenshots' must be a list of non-empty URL strings")
        screenshot_list = cast(list[str], screenshots)
        if len(screenshot_list) > MAX_SCREENSHOTS:
            _fail(f"{plugin_yaml_path} field 'screenshots' must contain at most {MAX_SCREENSHOTS} entries")
        for screenshot_url in screenshot_list:
            _validate_remote_screenshot_url(plugin_yaml_path, screenshot_url.strip())


def _validate_thumbnail(image_path: Path) -> None:
    if image_path.suffix.lower() not in ALLOWED_IMAGE_EXTS:
        _fail(f"Thumbnail must be one of {sorted(ALLOWED_IMAGE_EXTS)}: {image_path.name}")

    size = image_path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        _fail(f"Thumbnail is too large ({size} bytes). Max is {MAX_IMAGE_BYTES} bytes")

    try:
        with Image.open(image_path) as im:
            w, h = im.size
    except Exception as e:
        _fail(f"Thumbnail image could not be opened: {e}")

    if w != h:
        _fail(f"Thumbnail must be square (width == height). Got {w}x{h}")


def _github_api_get_json(url: str) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "a0-plugins-validator",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        _fail(f"GitHub API request failed ({e.code}) for {url}: {msg}")
    except Exception as e:
        _fail(f"GitHub API request failed for {url}: {e}")

    try:
        parsed = __import__("json").loads(payload)
    except Exception as e:
        _fail(f"GitHub API returned invalid JSON for {url}: {e}")

    if not isinstance(parsed, dict):
        _fail(f"GitHub API returned non-object JSON for {url}")
    return cast(dict[str, Any], parsed)


def _parse_github_repo_url(repo_url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(repo_url)
    if parsed.netloc.lower() != "github.com":
        _fail(f"github field must point to github.com: {repo_url}")

    path = parsed.path.strip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        _fail(f"github field must be a GitHub repository URL like https://github.com/<owner>/<repo>: {repo_url}")

    owner = parts[0]
    repo = parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]

    return owner, repo


def _validate_github_repo(repo_url: str) -> None:
    owner, repo = _parse_github_repo_url(repo_url)

    repo_info = _github_api_get_json(f"https://api.github.com/repos/{owner}/{repo}")
    default_branch = repo_info.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch.strip():
        _fail(f"Unable to determine default branch for GitHub repo: {repo_url}")

    _github_api_get_json(
        f"https://api.github.com/repos/{owner}/{repo}/contents/plugin.yaml?ref={urllib.parse.quote(default_branch)}"
    )


def main() -> int:
    base_sha = os.environ.get("BASE_SHA")
    head_sha = os.environ.get("HEAD_SHA")
    if not base_sha or not head_sha:
        _fail("BASE_SHA and HEAD_SHA environment variables are required")

    changes = _get_changed_files(base_sha, head_sha)
    if not changes:
        _fail("No changed files detected")

    affected_paths = [Path(p) for p in _get_affected_paths(base_sha, head_sha)]
    if not affected_paths:
        _fail("No changed files detected")

    plugin_roots: set[Path] = set()
    for p in affected_paths:
        parts = p.parts
        if len(parts) < 3 or parts[0] != "plugins":
            _fail(
                "PRs must only change files under plugins/<plugin-name>/. "
                f"Found change outside plugins/: {p.as_posix()}"
            )
        plugin_roots.add(Path(parts[0]) / parts[1])

    if len(plugin_roots) != 1:
        _fail(f"PR must submit exactly one plugin folder. Found: {sorted(pr.as_posix() for pr in plugin_roots)}")

    plugin_root_rel = next(iter(plugin_roots))
    plugin_name = plugin_root_rel.parts[1]

    if plugin_name.startswith("_"):
        _fail(f"Plugin folder '{plugin_name}' starts with '_' which is reserved and not visible in Agent Zero")

    if not _is_valid_plugin_dirname(plugin_name):
        _fail(f"Plugin folder '{plugin_name}' must use lowercase letters, numbers, and underscores only")

    plugin_root_path = plugin_root_rel.as_posix()
    plugin_files = _git_ls_tree_names(head_sha, plugin_root_path)
    if not plugin_files:
        _fail(f"Plugin folder does not exist in PR head: {plugin_root_path}")

    plugin_yaml_path = f"{plugin_root_path}/plugin.yaml"
    if plugin_yaml_path not in plugin_files:
        _fail(f"Missing required file in PR head: {plugin_yaml_path}")

    thumbnails: list[str] = []
    for f in plugin_files:
        path_obj = Path(f)
        if path_obj.name == "plugin.yaml":
            continue

        if len(path_obj.parts) == 2:
            suffix = path_obj.suffix.lower()
            if suffix in ALLOWED_IMAGE_EXTS:
                if path_obj.stem.lower() != THUMBNAIL_BASENAME:
                    _fail(
                        f"Thumbnail must be named '{THUMBNAIL_BASENAME}<ext>' (e.g. thumbnail.png). Found: {f}"
                    )
                thumbnails.append(f)
                continue

        _fail(
            f"Unsupported file in plugin folder: {f}. "
            "Only plugin.yaml and an optional thumbnail image are allowed. "
            "Screenshots must be provided as external URLs in plugin.yaml."
        )

    if len(thumbnails) > 1:
        _fail("At most one thumbnail image is allowed. Found: " + ", ".join(thumbnails))

    plugin_yaml_bytes = _git_show_bytes(head_sha, plugin_yaml_path)
    _validate_yaml_bytes(plugin_yaml_path, plugin_yaml_bytes)

    if thumbnails:
        thumb_path = thumbnails[0]
        thumb_bytes = _git_show_bytes(head_sha, thumb_path)
        with tempfile.NamedTemporaryFile(suffix=Path(thumb_path).suffix, delete=False) as tmp:
            tmp.write(thumb_bytes)
            tmp_path = Path(tmp.name)
        try:
            _validate_thumbnail(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    deleted = [p for status, p in changes if status.startswith("D")]
    if deleted:
        _fail(f"PR must not delete files. Deleted: {deleted}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValidationError as e:
        print(f"Validation failed: {e}")
        raise SystemExit(1)
