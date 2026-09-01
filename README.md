# Shopping Copilot — TechJam Track 4 Submission

A conversational shopping agent that routes customer intent, tightens its BM25 search as the conversation reveals more, and asks only for information it doesn't already have.

Final local score: **TechnicalScore 0.615556** (Hit Rate@10 0.740, MRR 0.416853, MTTC 4.975), measured on the full 200-session public set — up from the provided baseline's 0.1067.

## How to run it

```bash
python3 -m evaluator.local_evaluator
```

That's the only command. `starter/agent.py` has **zero dependencies beyond the Python standard library** for this path — no network calls, no API keys, no external model to install. Retrieval runs entirely on SQLite's built-in FTS5 full-text index over `data/catalog.jsonl`. See `requirements.txt` for the one-line reasoning on why nothing else is required, and how to optionally re-enable the (disabled-by-default) local LLM re-ranker described below.

Python 3.10+, same as the starter kit.

## Architecture

The agent is a single class in `starter/agent.py` built around one idea: retrieval should get *stricter* as the conversation discloses more, and different intents should be routed differently rather than forced through one generic query.

**Intent routing.** Each session is classified as Buying, Browsing, Intent Override, or Boundary from the opening message and tracked in per-session state (`_SessionState`) across turns — nothing is re-derived from scratch each turn, and nothing disclosed earlier is thrown away.

**Buying track.** When the customer states a hard requirement up front (category, color, material), those terms become *mandatory* (SQL `AND`, not `OR`) in the FTS5 query immediately, rather than just nudging the BM25 ranking. A budget constraint filters the candidate pool by price before truncating to `top_k`.

**Browsing track.** Starts broad — nothing is mandatory yet, since nothing has been disclosed — and promotes each preference (color, material, category) from optional to mandatory the moment the customer mentions it. The same retrieval function behaves differently turn to turn purely based on what's been revealed, so a browsing session that starts vague and ends specific gets progressively sharper recommendations without any special-casing.

**Clarification policy.** The agent walks `ASK_ATTRIBUTE_PRIORITY` (from the provided API contract), skipping anything already known, and stops asking after turn 6 so the last few turns are pure retrieval against everything gathered — asking a question late in a 10-turn budget costs more than it's worth.

**Intent Override handling.** A scripted override phrase clears the relevant accumulated slot rather than the whole session state, so a correction lands on the right piece of context instead of resetting everything the customer already told the agent.

**BM25 field weighting.** The FTS5 query weights `title`, `categories`, `features`, `details`, `store`, and `description` differently (`categories` weighted heaviest after tuning — see v20 below); `asin` is excluded entirely since it's never semantically meaningful for matching.

## What we tried and reverted (and why that's disclosed, not hidden)

Every change in this project was measured against the real local evaluator on the full 200-session public set — kept only if it produced a measurable net improvement, reverted with the reasoning documented if not. Two significant paths were built, measured, and deliberately turned off:

- **Dense/hybrid semantic retrieval** (spaCy word vectors, reciprocal-rank-fusion merge with BM25) — built, measured, found net-negative, and fully removed as code and as a dependency (not just disabled).
- **Local LLM re-ranking** (`Qwen2.5-0.5B-Instruct`, GGUF/Q4_K_M, via `llama-cpp-python`) — built and run end-to-end on real hardware as a second-stage re-ranker over the BM25 candidate pool. The full-scale result showed it never surfaced a new correct answer that BM25 hadn't already found — hit rate was unchanged everywhere it ran — and it made MRR worse by reordering already-correct results less well on average (0.597966 → 0.591948). We then built a LoRA fine-tuning pipeline (data generation from real sessions, a runnable Colab notebook, GGUF re-conversion) to teach the model what a good rerank actually looks like on this catalog, rather than relying on zero-shot prompting. Its own holdout sanity check caught the fine-tune collapsing to a trivial identity-order prediction before any time was spent on GGUF conversion or a wasted full eval run, and we chose to lock in the confirmed-good BM25-only path instead of continuing to chase it under deadline pressure.

The re-ranker's code is kept in `agent.py` (unreachable unless `Agent(llm_model_path=...)` is explicitly passed) and the fine-tuning pipeline is kept in `training/` as evidence of the work, but neither is on the active/default path. Full before/after tables and reasoning for every version (v1 through v20) are in `DAY1_PROGRESS.md`.

## What actually moved the score

The largest, most reliable wins were evidence-grounded rather than guessed — found by reading the evaluator's own source rather than assuming what would help:

| Change | What it did | Result |
|---|---|---|
| Stateful accumulation (v1) | Stopped re-querying from scratch each turn | 0.107 → 0.428 |
| Buying-track mandatory anchor term (v3) | AND instead of OR for a stated hard requirement | → 0.524 |
| Adaptive retrieval strictness (v11) | Progressive optional→mandatory promotion for Browsing | major Browsing gain |
| Color/material vocabulary match (v13) | Matched the evaluator's own `COLOR_RE`/`MATERIAL_RE` exactly, so disclosed values are never missed by a near-miss synonym | measurable gain |
| Stopword tuning (v14) | Removed scripted-dialogue noise words that appear in real evaluator text, not guessed filler | measurable gain |
| Category mandatory for Buying too (v19) | Extended the v11 promotion logic to a route that hadn't had it | modest gain |
| BM25 field weight sweep (v20) | Raised `categories` field weight (4.0 → 8.0) after a real sweep | 0.607365 → **0.615556** |

The v20 change did cost Intent Override sessions a small hit-rate dip (0.800 → 0.767) as a side effect of sharing one retrieval function across routes — kept because the net effect across all four scenarios was still a clear win, and that tradeoff is documented rather than hidden.

## Model choice, cost, and disclosure

The submitted, default-path agent uses **no LLM and no external API** — pure BM25 over SQLite FTS5. Token usage reported by `respond()` is `0` across the board because no model call is made. There is no network dependency and no API key requirement; the submission runs identically online or fully offline, which also means it is unaffected by the organizer's option to disable network access for official scoring.

The disabled-by-default local re-ranker (`Qwen2.5-0.5B-Instruct`, Q4_K_M GGUF, ~400MB) was evaluated purely for technical merit and is not part of the scored path — see above for why it stayed off.

## Limitations

- Retrieval is lexical (BM25), not semantic — a customer describing a product in words that never appear in the catalog's title/description/features text will not be found, since dense retrieval was tried and reverted as net-negative on this catalog and session distribution.
- The clarification policy is a fixed priority order plus a turn-6 cutoff, not a learned or per-session-adaptive policy.
- Intent Override detection relies on the scripted phrase pattern present in this dataset's session generation; a differently-phrased override in the organizer's private 800 sessions could be missed if it doesn't match the same regex family.
- The BM25 field weights were tuned on the 200-session public set; they may not be the exact optimum on the private set, though they were chosen using the evaluator's own scoring rather than intuition.

## Repository layout

```text
starter/agent.py            the submitted Agent implementation
evaluator/local_evaluator.py provided evaluator, unmodified
data/                       provided catalog + public sessions
requirements.txt            dependency disclosure (none required for default path)
DAY1_PROGRESS.md            full version history: every change, measured, v1-v20
training/                   LoRA fine-tuning pipeline for the (unused) LLM re-ranker
archive/                    removed dense/hybrid retrieval code, kept for reference
demo_video_script.md        narration script for the submission demo video
results_v20_final.json      final reproducible evaluator output
```
