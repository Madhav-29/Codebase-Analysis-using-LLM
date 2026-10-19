# ADR 0004: Model selection, GPT-5.4-mini on Azure AI Foundry

**Status:** accepted (replaces the earlier choice of Gemini 3.5 Flash-Lite)

## Context

**Selection criteria,** in order:
1. Java/Spring comprehension, i.e. accurate descriptions of real methods.
2. Precision of findings: few false positives, and calibrated severity.
3. Structured-output reliability (strict JSON-schema decoding).
4. Instruction following: grounding, saying "unknown", not quoting secrets.
5. Cost and rate limits.

Context-window size and code generation are not criteria: every request is under 17k tokens, and nothing is generated.

**History:**
- Gemini 3.8 Flash was the first plan, but its free tier allows only 20 requests per day, and a full run needs ~123.
- Gemini 3.5 Flash-Lite produced the first complete analysis, now the baseline in `output/baselines/`.
- On Azure, the Global Standard deployment type had no quota on this subscription, so GPT-5.4-mini is deployed as **Data Zone Standard (US)**. Its limits are 100 requests and 100,000 tokens per minute.

## Decision

GPT-5.4-mini (`gpt-5.4-mini`; served version `gpt-5.4-mini-2026-03-17`) is the primary model, with reasoning effort `low`. The decision rests on a manual evaluation of both models on identical prompts and plans (details in [evaluation](../evaluation.md)):

| | GPT-5.4-mini | Gemini 3.5 Flash-Lite |
|---|---|---|
| Golden-method descriptions: correct / partial / incorrect | **10 / 2 / 0** | 8 / 2 / 2 |
| Serious findings that are false positives | **0 of 34** | 9 of 43 |
| Recall of 15 verified defects | **14 + 1 partial** | 11 |
| High-severity findings that are real defects | **19 of 21** | 4 of 7 |

**Integration:** `AzureAIOpenAIApiChatModel` (langchain-azure-ai) on the deployment's `/openai/v1` endpoint. It authenticates with `AZURE_AI_API_KEY`, or with Entra ID via `DefaultAzureCredential`.

## Alternatives

- **Keep Gemini 3.5 Flash-Lite.** It costs nothing on the free tier and its descriptions are nearly as good. Its findings, though, include a systematic false positive (`==` on enums) and miss two significant defects: JWTs that never expire, and an NPE on an optional query parameter.
- **`ChatOpenAI(base_url=...)` instead of langchain-azure-ai.** `langchain-azure-ai` adds 33 packages (Azure Search, Content Understanding with a beta pin, AI Projects, `langgraph`, `numpy`, …), installed only with the optional `azure` extra. `ChatOpenAI` pointed at the same endpoint should work with no new dependencies: the connectivity ping reached this deployment through that endpoint and key with a plain HTTP client, though `ChatOpenAI` itself was not tried. It lacks two things the Azure class handles: built-in Entra ID credentials, and a payload fix Azure needs if the Responses API is used (this configuration uses Chat Completions). If the footprint matters more than those, this is a small change in one provider module.

## Consequences

- **Volume:** GPT reports ~3× the findings (323 vs 100). The reviewed serious slice has no false positives, but about 20% of it is style opinion, and 277 of the 320 LLM findings were not individually reviewed.
- **Tokens:** output is +46% (74,928 vs 51,287, of which 12,108 are reasoning tokens). The output records token counts only; prices are not estimated.
- **Data residency:** Data Zone Standard (US) processes requests within the US data zone, so the analyzed code leaves the machine for US data centers. `store=False` asks the service not to retain responses. Code with EU residency requirements needs an EU data-zone deployment.
- **Model version:** the deployment's version-upgrade policy (pinned or auto-upgrade) is set in Azure AI Foundry, not by this tool. Every run records the version the API actually served in `metadata.served_models`, so a version change shows in the output. The 2026-03-17 version is GA until 2027-09-20.
- **Temperature:** reasoning models accept no temperature; behavior is steered by `reasoning_effort`, recorded in metadata. Exact replay comes from the cache ([ADR 0005](0005-content-addressed-cache.md)).
