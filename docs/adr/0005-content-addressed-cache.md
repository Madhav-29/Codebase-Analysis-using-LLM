# ADR 0005: Content-addressed response cache

**Status:** accepted

## Context

A full run is ~123 calls:
- On a free tier it can be stopped by a daily quota.
- Any run can be interrupted.
- Changing output-only code (assembly, the report) shouldn't require re-paying for LLM calls.
- LLMs aren't bit-for-bit deterministic, and reasoning models accept no temperature, so re-running doesn't reproduce text.

## Decision

Cache every **validated** response on disk, written atomically as it arrives. The key is a SHA-256 over everything that can change the answer:

- the prompt version
- provider, model, temperature and reasoning effort
- the response JSON Schema
- the system prompt and the full user prompt, which contains the unit's code and context

A setting that isn't in effect (e.g. reasoning effort for Gemini) is left out of the key, so adding a new setting doesn't invalidate existing entries. Each entry stores the parsed response, its token usage and the served model version.

## Alternatives

- **No cache, rely on temperature 0.** It doesn't reproduce text, can't resume, and isn't available on reasoning models.
- **A cache keyed by file path or class name.** It would serve stale answers after a code or prompt change.
- **A shared cache service (e.g. Redis).** It's unnecessary for a single-user CLI.

## Consequences

- **Resume:** an interrupted or quota-stopped run resumes by re-running the same command.
- **Free rebuilds:** output-only changes are re-assembled for free. The restructure was proven behavior-neutral this way: the rebuilt Gemini analysis matched the baseline in everything but its timestamp.
- **Honest metadata:** a replayed run reports live calls and cache hits separately, plus the tokens the cached responses originally cost.
- **Not committed:** the cache is gitignored, so a fresh clone must call the provider once.
