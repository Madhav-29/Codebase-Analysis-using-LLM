# ADR 0001: Deterministic facts, LLM interpretation

**Status:** accepted

## Context

The output has to list every method of the codebase with its signature, plus a description, complexity and findings. Some of this can be checked against the source: signatures, annotations, line ranges, REST routes, complexity, which methods exist. The rest cannot: what a method does, what a class is for, what a reviewer would flag. An LLM can produce both kinds, but when it produces the checkable kind it can misquote a signature, invent a route or silently skip a method.

## Decision

Extract everything checkable deterministically:

- **tree-sitter-java:** types, members, generics, annotations, line ranges.
- **lizard:** complexity.
- **Mapping resolution:** endpoints, as context-path + class mapping + method mapping.
- **Rules:** trivial members, framework notes, domain grouping, key-method selection.

Ask the LLM only for interpretation, keyed by ids the analyzer supplies. Facts and LLM text are joined by id at assembly. Every LLM-written field is labeled (`description_source`, `summary_source`, `source`). A coverage assertion fails the run if any parsed method is missing or duplicated.

## Alternatives

- **Let the LLM extract structure too.** Simpler code, but nothing would be verifiable, and coverage would depend on the model's attention.
- **Use a JVM tool (JavaParser) for facts.** It gives full symbol resolution, but adds a Java runtime to a Python tool. tree-sitter covers what the output needs; JavaParser is listed as future work for a call graph.

## Consequences

- Every fact in the output can be checked mechanically, and `validate --repo` re-parses the source to do so.
- The LLM's mistakes are confined to text fields and are measurable; see [evaluation](../evaluation.md).
- 330 of 608 members never reach the LLM, because rules describe trivial and framework-defined members.
- The LLM sees no call graph: a description is based on its own class plus the signatures of the types it references.
