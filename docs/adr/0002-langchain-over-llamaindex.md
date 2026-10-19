# ADR 0002: LangChain over LlamaIndex

**Status:** accepted

## Context

The assignment asks for LangChain or LlamaIndex. The job here is a fixed plan of structured-output calls: one per class unit, then per domain, then one for the project. Every response must validate against a schema, be retried on failure, and be cached. Nothing is retrieved by similarity.

## Decision

Use LangChain, narrowly:

- **Provider-neutral chat models:** `ChatGoogleGenerativeAI`, `AzureAIOpenAIApiChatModel`, optionally `ChatAnthropic`.
- **`with_structured_output(PydanticModel, method="json_schema")`:** strict mode on OpenAI-family models, so the model decodes against our JSON Schema.
- **Provider-neutral exception types** (`ModelRateLimitError`, `ModelAuthenticationError`, ...): retry decisions are made in one place without importing any provider SDK.
- **`get_num_tokens`:** native token counts for verifying the budget estimate.

Orchestration is plain Python: map/reduce, budgets, caching, retries. Provider SDKs are imported only in `llm/providers/`, and a test enforces that.

## Alternatives

- **LlamaIndex.** Its strengths are indexing and retrieval: chunk stores, vector indexes, query engines. That's the wrong shape for exhaustive extraction, where every class must be visited exactly once. It would fit a Q&A layer over the results (future work).
- **Provider SDKs directly.** It's workable, but each provider's structured-output, error and token-counting APIs differ. LangChain normalizes exactly those three things.
- **LangChain agents or LangGraph.** An agent decides what to read, which gives nondeterministic coverage at a higher call count. The plan *is* the coverage guarantee, so there is nothing for an agent to decide.

## Consequences

- Swapping providers is configuration (`LLM_PROVIDER`). This project ran Gemini and GPT on identical prompts.
- The LLM layer is testable with a fake chat model, so all tests run offline.
- LangChain shows up in two modules, not throughout the codebase. That keeps upgrades cheap, and anyone reading the code can see exactly what it's used for.
