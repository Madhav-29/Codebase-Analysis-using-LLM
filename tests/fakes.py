"""A fake chat model with LangChain's structured-output contract (no network).

`with_structured_output(schema, method=..., include_raw=True)` returns a
Runnable whose output is {"raw": AIMessage, "parsed": schema | None,
"parsing_error": Exception | None}, exactly what langchain-anthropic returns.
"""

from __future__ import annotations

import json
import re

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from pydantic import ValidationError

from codebase_analyzer.schema import ClassUnitResponse, DomainResponse, ProjectResponse

TYPE_LINE = re.compile(r"^- (\S+) \((\w+), lines (\d+)-(\d+)\)$", re.M)
MEMBER_LINE = re.compile(r"^- (m\d+) in (\S+): ", re.M)


class FakeStructuredModel:
    def __init__(self, responder=None):
        self.responder = responder or smart_responder
        self.calls: list[tuple[type, str]] = []

    def with_structured_output(self, schema, method="json_schema", include_raw=False):
        assert method == "json_schema" and include_raw

        def call(messages):
            user = messages[-1].content
            self.calls.append((schema, user))
            payload = self.responder(schema, user, len(self.calls))
            if isinstance(payload, BaseException):
                raise payload
            raw = AIMessage(content=json.dumps(payload),
                            usage_metadata={"input_tokens": 100, "output_tokens": 40, "total_tokens": 140})
            try:
                return {"raw": raw, "parsed": schema.model_validate(payload), "parsing_error": None}
            except ValidationError as exc:
                return {"raw": raw, "parsed": None, "parsing_error": exc}

        return RunnableLambda(call)


def class_payload(user: str, drop_types: set[str] = frozenset(), drop_members: set[str] = frozenset()) -> dict:
    types = [m.group(1) for m in TYPE_LINE.finditer(user)]
    members = [(m.group(1), m.group(2)) for m in MEMBER_LINE.finditer(user)]
    first_line = int(TYPE_LINE.search(user).group(3))
    return {"classes": [
        {"type_key": t, "summary": f"Summary of {t}.", "responsibilities": ["does things"],
         "methods": [{"member_key": k, "description": f"Describes {k}."}
                     for k, owner in members if owner == t and k not in drop_members],
         "findings": [{"category": "maintainability", "severity": "info", "title": f"Note on {t}",
                       "detail": "Detail.", "line_start": first_line, "line_end": first_line}]}
        for t in types if t not in drop_types]}


def smart_responder(schema, user, n):
    if schema is ClassUnitResponse:
        return class_payload(user)
    if schema is DomainResponse:
        return {"summary": "Domain summary.", "key_responsibilities": ["r1"], "findings": []}
    if schema is ProjectResponse:
        return {"purpose": "A service.", "functionality": ["f"], "architecture": "Layered.",
                "technologies": ["Spring Boot"], "cross_cutting_concerns": [{"name": "HATEOAS", "description": "Links."}],
                "findings": []}
    raise AssertionError(schema)
