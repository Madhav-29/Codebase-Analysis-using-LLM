# Operations

## Configuration

Settings come from (highest first) CLI flags, environment variables, `.env`, then defaults. Start from [`.env.example`](../.env.example). API keys are read from the environment by the provider modules. They are never settings, never logged, and never written to output.

| Setting | Default | CLI flag | Notes |
|---|---|---|---|
| `LLM_PROVIDER` | `azure_foundry` | `--provider` | `azure_foundry`, `google_genai`, `anthropic` |
| `LLM_MODEL` | `gpt-5.4-mini` | `--model` | Model id, or the deployment name on Azure |
| `AZURE_AI_ENDPOINT` | | | `https://<resource>.services.ai.azure.com/openai/v1` |
| `AZURE_AI_API_KEY` | | | Empty → Entra ID via `DefaultAzureCredential` |
| `GOOGLE_API_KEY` | | | Gemini API key |
| `LLM_REASONING_EFFORT` | `low` | | Reasoning models only |
| `LLM_TEMPERATURE` | `0` | | Sent only to models that accept it |
| `LLM_CONCURRENCY` | per provider | `--concurrency` | Azure 4, Gemini 1, Anthropic 1 |
| | | `--request-tokens`, `--output-reserve` | Token budget (32,000 / 12,000) |
| | | `--max-classes N` | Analyze N files, picked round-robin across domains |
| | | `--no-cache` | Ignore and don't write the response cache |

## Providers

### Azure AI Foundry (primary): GPT-5.4-mini

1. Install the extra: `uv sync --extra azure`.
2. In Azure AI Foundry, deploy `gpt-5.4-mini`. This project used **Data Zone Standard (US)**, because Global Standard had no quota on the subscription. The limits were 100 RPM and 100,000 TPM.
3. Set `AZURE_AI_ENDPOINT` to the deployment's OpenAI-compatible v1 endpoint, ending in `/openai/v1`. Set `LLM_MODEL` to the **deployment name**.
4. Set `AZURE_AI_API_KEY`, or leave it empty and sign in with `az login` (Entra ID).

**Details:**
- **Data residency:** a Data Zone Standard (US) deployment processes requests within the US data zone. The analyzed code is sent there. `store=False` asks the service not to retain responses. Use an EU data-zone deployment if EU residency is required.
- **Model version:** the deployment uses auto-upgrade (Azure's default, "Once a new default version is available"), so Azure may change the model version between runs. Each run records the version actually served in `metadata.served_models` (`gpt-5.4-mini-2026-03-17` for the committed output, GA until 2027-09-20) for traceability.
- **What is sent:** `reasoning_effort` and `max_completion_tokens`, no temperature, and strict JSON-schema output.

### Gemini API (alternative): Gemini 3.5 Flash-Lite

- **Setup:** set `LLM_PROVIDER=google_genai`, `LLM_MODEL=gemini-3.5-flash-lite`, and `GOOGLE_API_KEY` (free key at <https://aistudio.google.com/apikey>).
- **Free tier:**
  - Flash-Lite's quota covered the full run.
  - Gemini 3.8 Flash allows only 20 requests/day.
  - On the free tier, Google may use prompts to improve its products. That's fine for a public repository; use a paid tier for private code.
- **Temperature:** Flash-Lite uses fixed sampling, so temperature isn't sent, and the metadata says so.

### Anthropic (optional)

Install with `uv sync --extra anthropic`, then set `LLM_PROVIDER=anthropic`, `LLM_MODEL` and `ANTHROPIC_API_KEY`.

## Running

```bash
uv run codebase-analyzer estimate --repo ../spring-rest-sakila [--native-count]   # calls and tokens; no LLM calls
uv run codebase-analyzer analyze  --repo ../spring-rest-sakila --max-classes 5 --output output/smoke/analysis.json
uv run codebase-analyzer analyze  --repo ../spring-rest-sakila                     # -> output/analysis.json
uv run codebase-analyzer validate output/analysis.json --repo ../spring-rest-sakila
uv run codebase-analyzer report                                                     # -> output/analysis_report.md
uv run codebase-analyzer eval output/analysis.json --repo ../spring-rest-sakila    # -> evals/review_<model>.md
```

**Cost.** The tool reports token counts (input, output, reasoning; live and cached) and deliberately never estimates prices. A full run with GPT-5.4-mini was 306k input and 75k output tokens.

## Rate limits, retries, resume

- **Concurrency:** 4 for Azure, 1 for the Gemini free tier. SDK retries are disabled; the client owns retries.
- **429s and 5xx:** exponential backoff (10 s doubling to 120 s, 8 retries), honoring `Retry-After`/`retry-after-ms` and Gemini's `retryDelay`.
- **Daily quota:** a daily-quota error stops the run and prints the exact command to resume.
- **Auth, permission or unknown deployment:** fails fast. This includes Gemini's "API key not valid" (a 400).
- **Content filter:** the unit fails at once with the reason, and the run continues.
- **Invalid structured output:** re-sent up to 3 times with the validation error, then reported as failed. Nothing is fabricated.
- **Cache:** responses are cached as they arrive ([ADR 0005](adr/0005-content-addressed-cache.md)), so re-running the same command resumes an interrupted run, and output-only changes rebuild for free.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `error: The azure_foundry provider needs its extra` | `uv sync --extra azure` |
| `error: AZURE_AI_ENDPOINT must be the OpenAI-compatible v1 endpoint` | Use `https://<resource>.services.ai.azure.com/openai/v1`, not the resource root or a deployment URL |
| `ProviderAuthError` with 401/403 | Check `AZURE_AI_API_KEY`, or your Entra ID role on the resource |
| `ProviderAuthError` with 404 | `LLM_MODEL` must be the deployment name |
| `error: ... has no src/main/java directory` | Point `--repo` at the Java project root |
| Windows: "Filename too long" when cloning the target | `git -c core.longpaths=true clone ...` |
| Windows: Smart App Control blocks `codebase-analyzer.exe` | `uv run python -m codebase_analyzer <command>` |

## Development

```bash
uv sync --all-extras
uv run ruff check && uv run ruff format --check
uv run mypy                     # strict on src/
uv run pytest --cov             # offline: fake chat model, no keys, no network
uvx pre-commit install          # ruff, formatting, whitespace, YAML, private-key detection
```

CI (`.github/workflows/ci.yml`) runs the same checks on Ubuntu and Windows with no secrets.
