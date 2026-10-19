"""On-disk, content-addressed cache of validated LLM responses.

Key = sha256 over everything that can change the answer: prompt version,
provider, model, sampling/reasoning settings, the response JSON Schema (stricter
than a schema version), the system prompt and the full user prompt (which
contains the unit's code and supporting context). Changing any of them is a
cache miss; re-running an unchanged analysis replays identical output.

Settings that are not in effect (e.g. reasoning effort for a non-reasoning
model) are left out of the key, so adding a knob does not invalidate entries
written before it existed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel


def cache_key(
    *,
    prompt_version: str,
    provider: str,
    model: str,
    temperature: float | None,
    schema: type[BaseModel],
    system: str,
    user: str,
    reasoning_effort: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "prompt_version": prompt_version,
        "provider": provider,
        "model": model,
        "temperature": temperature,
        "schema": schema.model_json_schema(),
        "system": system,
        "user": user,
    }
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


class ResponseCache:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))

    def put(self, key: str, parsed: BaseModel, usage: dict[str, int], served_model: str | None = None) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"parsed": parsed.model_dump(), "usage": usage, "served_model": served_model}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, indent=1), encoding="utf-8")
        tmp.replace(path)  # atomic: a crashed run never leaves a half-written entry
