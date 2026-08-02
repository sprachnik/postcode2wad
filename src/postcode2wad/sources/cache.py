"""On-disk response cache.

Overpass is a shared free service run on donated hardware. Re-running the
generator on the same tile must not re-query it — cache first, ask later.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

DEFAULT_CACHE_DIR = Path("cache")


def cache_path(
    namespace: str, key: str, cache_dir: Path | None = None, suffix: str = ".json"
) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return (cache_dir or DEFAULT_CACHE_DIR) / namespace / f"{digest}{suffix}"


def load_json(namespace: str, key: str, cache_dir: Path | None = None):
    path = cache_path(namespace, key, cache_dir)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def store_json(namespace: str, key: str, value, cache_dir: Path | None = None) -> Path:
    path = cache_path(namespace, key, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path
