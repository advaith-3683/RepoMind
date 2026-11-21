"""Tiny best-effort on-disk JSON cache (used for LLM responses and query embeddings).
Failures to read or write never break a request."""
import hashlib
import json
import os
import tempfile
from pathlib import Path


class DiskCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    @staticmethod
    def key(obj) -> str:
        blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _path(self, namespace: str, key: str) -> Path:
        return self.root / namespace / key[:2] / f"{key}.json"

    def get(self, namespace: str, key: str):
        try:
            return json.loads(self._path(namespace, key).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def set(self, namespace: str, key: str, value) -> None:
        p = self._path(namespace, key)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(value, f)
                os.replace(tmp, p)          # atomic: readers never see a half-written file
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except OSError:
            pass
