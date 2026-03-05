"""Compatibility shim: kept for backwards compatibility.

Canonical implementation now lives in scripts/update_plugin_discussions.py.
"""

import update_plugin_discussions


def main() -> int:
    return int(update_plugin_discussions.main())


if __name__ == "__main__":
    raise SystemExit(main())
