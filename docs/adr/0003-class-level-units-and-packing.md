# ADR 0003: Class-level units, packing and splitting

**Status:** accepted

## Context

Every non-trivial method needs a description, and every request must fit a token budget: 32,000 tokens, of which 12,000 are reserved for output, with a 10% margin. That leaves an input limit of 16,800. The repository is ~130k tokens across 188 files, mostly small DTOs, mappers and repositories.

## Decision

- **One class (one file with its nested types) per call.** A class is the context a method needs: its fields, collaborators and sibling methods. Signatures of referenced project types are added, capped at 2,000 tokens.
- **Pack small, simple classes** (≤1,500 tokens, max CC ≤5), up to 8 per call. Packs never mix packages, so a pack stays thematically coherent. Each class must come back exactly once; a class missing from a pack's answer is retried on its own.
- **Split oversized classes at method boundaries.** Every part repeats a skeleton of the class: package, imports, fields, and one line per method. A single oversized method is cut into labeled line segments. Nothing is truncated, and tests assert that every line appears exactly once across the parts.
- **Plan with a conservative, provider-independent estimate** (`chars / 3`). Every model then receives identical prompts and packs, which keeps comparisons fair. The provider's own count verifies it: the worst unit came in at 0.71 of its estimate on GPT and 0.85 on Gemini.

## Alternatives

- **One method per call.** It loses class context and costs ~3× the calls.
- **The whole repository in one prompt.** It doesn't fit the budget, and "describe all 608 methods" from one long prompt is uneven and can't be verified.
- **Fixed-size text chunks.** These cut through methods and classes, so descriptions lose their subject.

## Consequences

- 188 files become 111 calls (33 of them packs) plus 12 reduce calls.
- The coverage check can prove every method was asked for and answered.
- No class in this repository needed a split at the default budget; `--request-tokens 8000` exercises the splitting live.
- Packing trades a little per-class attention for fewer calls. That's why large or complex classes are never packed.
