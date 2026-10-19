# Evaluation and model comparison

Both models analyzed spring-rest-sakila @ `62873f7` with **identical prompts and an identical plan** (111 class units + 12 reduce calls). Planning doesn't depend on the provider, so the comparison is like for like.

All verdicts are manual, against the source. They are recorded in [`evals/`](../evals) and scored by code:

```bash
uv run codebase-analyzer eval output/analysis.json --repo ../spring-rest-sakila
uv run codebase-analyzer eval output/baselines/analysis_gemini_flash_lite.json --repo ../spring-rest-sakila
```

Each run writes a review sheet (`evals/review_<model>.md`) with every description next to its source lines, and prints the recorded scores.

## Method descriptions: 12 golden methods

**Selection:** 12 methods across 8 domains: the most complex non-trivial methods (CC 3–5) plus the longest service and repository logic. They're listed in [`evals/golden_methods.yaml`](../evals/golden_methods.yaml).

**Rubric:**
- **correct**: the description covers the branches and side effects a reader needs, and says nothing false.
- **partially correct**: the main behavior is right, but a path or side effect is missing or imprecisely described.
- **incorrect**: the main behavior is wrong, or a path is described as doing the opposite.

| # | Method | GPT-5.4-mini | Gemini 3.5 Flash-Lite |
|---|---|---|---|
| 1 | `ExpressionUtils.betweenExpression` | correct | correct |
| 2 | `ActorController.getActorFilmList` | correct | correct |
| 3 | `FilmController.getFilmList` | correct | correct |
| 4 | `LocalDateYearSerializer.serialize` | partial: says "the year" where a configured formatter writes the full date | correct |
| 5 | `JwtAuthenticationProvider.authenticate` | correct | partial: omits `return null` on a bad token |
| 6 | `GeometryConverter.convertToEntityAttribute` | correct | **incorrect**: says it returns null on a parse error; it throws |
| 7 | `RentalServiceImpl.rentDvd` | correct | correct |
| 8 | `RentalServiceImpl.returnDvd` | correct | correct |
| 9 | `CustomStoreRepositoryImpl.findStoreDetail` | correct | partial: omits what is joined and projected |
| 10 | `PaymentServiceImpl.createPayment` | correct | correct |
| 11 | `CustomStaffRepositoryImpl.findStaffDetailsById` | correct | correct |
| 12 | `CustomCustomerRepositoryImpl.findAllCustomerRentalListByIdWithCondition` | partial: doesn't say `RENTED` selects *returned* rentals | **incorrect**: describes the intended filter |
| | **Total** | **10 / 2 / 0** | 8 / 2 / 2 |

## Findings: precision

**Review scope:** every LLM finding with category `potential_bug` and severity medium or high, the slice a reader acts on first. It covers 34 GPT findings and 43 Flash-Lite findings. Each is categorized as:

- **valid bug**: the code fails or misbehaves as described.
- **valid improvement**: accurate and worth changing, but not a defect.
- **style opinion**: accurate, but a matter of convention or defensive style.
- **false positive**: the claim about the code is wrong.

The per-finding verdicts and notes are in [`evals/findings_review.yaml`](../evals/findings_review.yaml).

| | GPT-5.4-mini | Gemini 3.5 Flash-Lite |
|---|---|---|
| Valid bug | 18 | 27 |
| Valid improvement | 9 | 5 |
| Style opinion | 7 | 2 |
| False positive | **0** | **9** |
| Actionable (bug + improvement) | **79%** (27/34) | 74% (32/43) |
| Factually accurate (not a false positive) | **100%** | 79% |

**How each model goes wrong:**
- **Flash-Lite makes false claims.** Six of its nine are the same misconception: that `==` on an enum is unsafe, when it's correct Java. The others: an assembler that correctly passes its controller class, `valueOf` on enum names that do match, and a speculative claim about Sakila's schema.
- **GPT is over-cautious instead.** Its seven style opinions are accurate but low-value: null-guards on internal calls, DTO equality conventions, a cast that is always safe in a servlet app.
- **Raw counts aren't comparable.** GPT merges repeated occurrences (one finding for three `Optional.of` sites), so its bug count isn't comparable one-to-one with Flash-Lite's.

## Findings: recall of verified defects

Fifteen distinct defects were verified against the source across both reviews. For each, did the model report it **anywhere** in its output, at any severity or category?

| # | Defect | GPT-5.4-mini | Flash-Lite |
|---|---|---|---|
| 1 | `Optional.of(query.fetchFirst())` throws an NPE when nothing matches (8 sites) | yes | yes |
| 2 | `update()` writes FK columns mapped `insertable = false, updatable = false`, so the change is silently lost | yes | yes |
| 3 | Stub repository/service methods return `null` or `Optional.empty()` | yes | yes |
| 4 | `CountryDto.equals` compares a `String` with `==` | yes | yes |
| 5 | `FilmDetailsRepresentationModelAssembler` passes a DTO class where the controller class belongs | yes | yes |
| 6 | `JpaUserDetailsService` dereferences a missing user | yes | yes |
| 7 | `getFilmRentalPrice` null-checks the rate but not the result | yes | yes |
| 8 | `RentalStatus.valueOf` on raw input turns a bad value into a 500 | yes | yes |
| 9 | An unknown country id throws in `replaceCountryIdToCountryInString` | yes | yes |
| 10 | A null query result is passed to `replaceCountryIdToCountry` | yes | yes |
| 11 | The rental-status filter: `RENTED` selects returned rentals | partial (only noticed `RETURNED` is unfiltered) | yes |
| 12 | The date-range filter uses `endDate.atStartOfDay()` as an inclusive upper bound, excluding the end date | **yes** | no |
| 13 | An empty role set is written as `""` and cannot be read back | **yes** | no |
| 14 | JWTs are issued without an expiration | **yes** | no |
| 15 | `/api/v1/actors/{id}/films` throws an NPE when the optional `releaseYear` is omitted | **yes** | no |
| | **Found** | **14 + 1 partial** | 11 |

## Severity calibration

| | GPT-5.4-mini | Flash-Lite |
|---|---|---|
| `high` findings | 21 | 7 |
| …that are real defects | **19** (2 are improvements) | 4 (3 are overstated improvements) |

## Run comparison

| | GPT-5.4-mini (Azure, Data Zone Standard) | Gemini 3.5 Flash-Lite (free tier) |
|---|---|---|
| Calls | 120 live + 3 cache hits (from the smoke run, same prompts) | 124 live |
| Input / output tokens | 306,271 / 74,928 (12,108 reasoning) | 294,867 / 51,287 |
| Wall time | 3 min 45 s (concurrency 4; 100 RPM / 100k TPM) | 7 min 36 s (concurrency 1; free-tier limits) |
| Rate-limit retries / validation retries / failed units | 0 / 0 / 0 | 7 / 1 / 0 |
| Findings | 323 (320 LLM) | 100 (97 LLM) |
| Cost | token counts only; prices not included | free tier |

The wall-time difference mostly reflects the different rate limits and concurrency, not model speed.

## Conclusion

GPT-5.4-mini is the primary model, and its output is `output/analysis.json`. It describes methods more reliably (no incorrect description in the sample) and made no false claims in the reviewed slice. It found four real defects Flash-Lite missed, two of them significant (non-expiring JWTs, a crash on an optional parameter), and its "high" severity can be trusted.

The costs are 46% more output tokens and roughly three times as many findings. About 20% of the reviewed slice is style opinion. Of GPT's 320 LLM findings, 43 were reviewed (the 34 above plus the 9 other high-severity ones); the remaining **277** (medium findings in other categories, and all low/info) were not reviewed individually. Flash-Lite remains a reasonable zero-cost option, kept in `output/baselines/`.

**Earlier sample:** on a 5-class smoke sample, Gemini 3.8 Flash described methods as accurately as both models and found 4 valid class-level findings. Its free tier (20 requests/day) ruled out a full run.
