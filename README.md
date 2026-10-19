# codebase-analyzer

A command-line tool that reads a Java/Spring codebase and uses an LLM (Google Gemini, through LangChain) to produce a structured JSON description of it: what the project does, its key methods with signatures and descriptions, its REST endpoints, code complexity, and noteworthy findings.

It was built as a take-home assignment, with [codejsha/spring-rest-sakila](https://github.com/codejsha/spring-rest-sakila) (a Spring Boot REST API over the Sakila DVD-rental database) as the codebase to analyze. The main challenge is covering every method of a real codebase reliably, while keeping each LLM request within its token limit.

## What's in this repo

| Path | What it is |
|---|---|
| `src/codebase_analyzer/` | The analyzer |
| `tests/` | 43 tests, no network needed |
| `output/analysis.json` | The result of a full run on spring-rest-sakila |
| `output/analysis.schema.json` | JSON Schema of that result |
| `DOCUMENTATION.md` | Approach, design decisions, token strategy, output format, evaluation, assumptions and limitations |

## Setup

You need Python 3.11+, [uv](https://docs.astral.sh/uv/) and git.

```bash
uv sync
git -c core.longpaths=true clone https://github.com/codejsha/spring-rest-sakila target_repo
git -C target_repo checkout 62873f7bb8a06681f299f8bd209cb88089e9be04
cp .env.example .env          # Windows: Copy-Item .env.example .env
```

Then open `.env` and set `GOOGLE_API_KEY`. You can get a free key at <https://aistudio.google.com/apikey>. `.env` is gitignored.

## Usage

```bash
uv run codebase-analyzer estimate --repo ./target_repo                       # preview: LLM calls and tokens, no API calls
uv run codebase-analyzer analyze  --repo ./target_repo --output output/analysis.json
uv run codebase-analyzer validate output/analysis.json --repo ./target_repo  # check the result
uv run pytest
```

Add `--max-classes 5` to `analyze` for a quick trial run.

## What happens when you run it

1. The Java source is parsed (tree-sitter). Signatures, annotations, endpoints and complexity are extracted without the LLM.
2. Code is grouped into requests that each fit the token budget. Trivial methods (getters, setters and the like) are described by rules and never sent to the LLM.
3. Gemini describes each class and its methods, and flags issues. Then it summarizes each domain, and finally the whole project.
4. Everything is merged into one JSON file, and the run fails if any method is missing.

A full run on spring-rest-sakila makes ~124 LLM calls and takes about 8 minutes on the free tier. Responses are cached, so re-running is instant, and an interrupted run continues where it stopped.

See [DOCUMENTATION.md](DOCUMENTATION.md) for how and why it works this way.
