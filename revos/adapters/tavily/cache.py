"""A file cache of Tavily answers, so a demo can replay offline and a repeated question costs nothing.

One JSON file per request, named by a hash of the request. Each keeps the request, the answer, the time it was
fetched and the credits it cost. Nothing secret is stored: the key is never part of a request body."""

import hashlib
import json
from pathlib import Path
from typing import Any


class ResearchCache:
    def __init__(self, *dirs: Path) -> None:
        if not dirs:
            raise ValueError("a cache needs at least one directory")
        self._write_dir, self._read_dirs = dirs[0], dirs

    @staticmethod
    def key(op: str, params: dict[str, Any]) -> str:
        blob = json.dumps({"op": op, "params": params}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def get(self, op: str, params: dict[str, Any]) -> dict[str, Any] | None:
        name = f"{self.key(op, params)}.json"
        for directory in self._read_dirs:
            path = directory / name
            if path.exists():
                return json.loads(path.read_text())
        return None

    def put(self, op: str, params: dict[str, Any], response: dict[str, Any], fetched_at: str,
            credits: int) -> None:
        self._write_dir.mkdir(parents=True, exist_ok=True)
        entry = {"op": op, "params": params, "response": response, "fetched_at": fetched_at,
                 "credits": credits}
        (self._write_dir / f"{self.key(op, params)}.json").write_text(
            json.dumps(entry, indent=1, sort_keys=True))
