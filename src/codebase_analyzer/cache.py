"""On-disk cache of validated LLM responses.

Key = sha256 over everything that can change the answer: prompt version,
provider, model, sampling/effort settings, output schema, system prompt and the
full user prompt (which already contains the unit's code and supporting
context). Changing any of them is a cache miss; re-running an unchanged
analysis replays identical output, which is also how runs are reproducible.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel

from .prompts import PROMPT_VERSION


def cache_key(*, provider: str, model: str, temperature: float | None, effort: str | None,
              schema: type[BaseModel], system: str, user: str) -> str:
    payload = json.dumps(
        {
            "prompt_version": PROMPT_VERSION,
            "provider": provider,
            "model": model,
            "temperature": temperature,
            "effort": effort,
            "schema": schema.model_json_schema(),
            "system": system,
            "user": user,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ResponseCache:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def put(self, key: str, parsed: BaseModel, usage: dict[str, int]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"parsed": parsed.model_dump(), "usage": usage}, indent=1), encoding="utf-8")
        tmp.replace(path)  # atomic: a crashed run never leaves a half-written entry
