# Dev Session Dialog Patterns

Reverse-engineered from `data/public_set.jsonl` + `evaluator/local_evaluator.py`
(the simulated-customer policy is deterministic and fully readable — this
isn't guesswork). Written for Role B's state machine design (Pillars II/III),
but Role A's retrieval logic already leans on a couple of these findings too.

## Scenario mix (fixed, both public and private splits per the spec)

| scenario_type | share | difficulty_bucket (public set) | n (public) |
|---|---|---|---|
| buying | 40% | always `easy` | 80 |
| browsing | 40% | always `medium` | 80 |
| intent_override | 15% | always `hard` | 30 |
| boundary | 5% | always `medium` | 10 |

`difficulty_bucket` is perfectly correlated with `scenario_type` in the
public set — it isn't independent signal, just a label on the same four
buckets. Don't build anything that depends on seeing it (the Agent never
receives it anyway — this is just a note for interpreting `public_set.jsonl`
by eye).

## Opening message templates (turn 1)

| scenario | template | example |
|---|---|---|
| buying | `I'm looking for {category}. A key requirement is: {constraint}.` | "I'm looking for Jewelry Necklaces. A key requirement is: Material:alloy." |
| browsing | `I'm looking for {category}, but I'm still exploring.` | "I'm looking for Basketball Men, but I'm still exploring." |
| boundary | *same template as browsing* — boundary is only distinguishable by what happens on turn 2 | "I'm looking for Athletic Walking, but I'm still exploring." |
| intent_override | `I'm looking for {category}. {old_value}` (no explicit marker) | "I'm looking for Accessories Belts. Buckle closure" |

Practical implication: a real agent **cannot tell Boundary and Browsing
apart from turn 1 alone** — they share the exact same opener shape. It also
can't tell Buying apart from Override reliably by marker text alone at turn
1 in general (Override's opener has no "A key requirement is:" phrase, so
in practice this isn't a huge ambiguity — but don't assume turn-1 classification
is ever 100% certain).

## Turn-by-turn disclosure policy (`customer_reply`)

Every turn, the simulated customer's reply depends only on the `ask_attribute`
the agent sent:

- **No attribute asked** (`ask_attribute: null`) → "Those options are not
  quite right yet. Ask me about one specific attribute." (pure filler, no
  new information — asking nothing is never rewarded)
- **Attribute asked, matching undisclosed constraint exists** → reveals up
  to 2 matching constraints: `"For that, what matters is: {a}; {b}."`
- **Attribute asked, nothing left to disclose for it** → `"I don't have an
  additional preference for {attribute}."` (a dead turn — no new signal)
- **Boundary scenario only, first attribute question, once per session** →
  `"I don't have a preference for {attribute}; please use your judgment."`
  This overrides the normal disclosure logic for exactly one turn, whichever
  attribute happens to be asked first.

Constraints are matched to an attribute via keyword heuristics
(`classify_constraint` in the evaluator): material names, color words,
size/fit words, budget/price patterns, department/style words, and a small
use-case keyword list; anything else falls back to `feature`. `category` is
**never** a matchable constraint type — the category is only ever disclosed
in the turn-1 opener, never through `customer_reply`.

## Finding: don't ask about `category`

Because `category` is disclosed in the opener and is never a matchable
constraint in `customer_reply`, asking `ask_attribute: "category"` always
produces "I don't have an additional preference for category." — a wasted
turn, 100% of the time, in every scenario. Confirmed against every
transcript sampled. `starter/agent.py`'s `ASK_ATTRIBUTE_PRIORITY` has
`category` removed for this reason.

## Finding: the Boundary one-shot interacts badly with question ordering

Boundary sessions burn their *first* asked attribute on the scripted
"use your judgment" non-answer, regardless of which attribute the agent
picked. Whichever attribute an agent's priority list asks first, Boundary
sessions get zero information from it. Since `material` is usually the
single most informative attribute (see transcripts below), leading with it
is a good general default but a real cost specifically for the 5% of
sessions that are Boundary — a genuine trade-off, not a bug to "solve" for
free. Measured impact of removing `category` from the priority list (so
`material` is asked first instead):

| | Buying | Browsing | Override | Boundary | Overall |
|---|---|---|---|---|---|
| `category` asked first | 0.700 | 0.500 | 0.800 | **0.500** | 0.524 |
| `material` asked first | 0.750 | 0.5375 | 0.833 | **0.300** | 0.563 |

Net positive (Boundary is only 5% of sessions; the other three scenarios
combined are 95%), but worth stating explicitly in the README's trade-offs
section rather than treating the Boundary drop as free. A smarter fix for
later: detect the "use your judgment" reply pattern and treat it as a signal
to lean more on retrieval/profile evidence rather than continuing straight
down the fixed priority list — that's real state-machine behavior, not a
priority-list tweak, and is in scope for Role B's Day 2/3 work.

## Intent Override: exact mechanics

- Fires at a fixed turn per session — always turn 3 or turn 4 (`rng.choice([3, 4])`,
  seeded deterministically per sample, so it's fixed for a given session but
  varies across sessions).
- Fires **regardless of what the agent asked that turn** — it replaces the
  customer's reply entirely, not just answers a question.
- The message is always exactly: `"Actually, ignore my earlier preference.
  What I need is: {new_value}."` — a fixed template, reliably matchable by
  regex (see `OVERRIDE_RE` in `starter/agent.py`).
- A session **cannot convert (register a hit) before the override fires**,
  even if the pre-override recommendations happened to contain the target —
  the evaluator ignores hits before `override_applied` is true. So there's
  no benefit to over-optimizing pre-override turns for Override sessions
  specifically; the real work happens turn 3/4 onward.

## Example transcripts (generated with a probe agent that just asks
`ASK_ATTRIBUTE_PRIORITY` in order, to show the simulator's raw behavior)

```
SCENARIO: buying — public_0001
  turn 1 [customer] I'm looking for Jewelry Necklaces. A key requirement is: Material:alloy.
  turn 1 [agent]    asks material
  turn 2 [customer] I don't have an additional preference for material.
  ...

SCENARIO: browsing — public_0006
  turn 1 [customer] I'm looking for Basketball Men, but I'm still exploring.
  turn 1 [agent]    asks material
  turn 2 [customer] For that, what matters is: polyester; 100% Polyester.
  ...

SCENARIO: intent_override — public_0002
  turn 1 [customer] I'm looking for Accessories Belts. Buckle closure
  turn 1 [agent]    asks material
  turn 2 [customer] I don't have an additional preference for material.
  turn 3 [customer] Actually, ignore my earlier preference. What I need is: leather.
  turn 3 [agent]    asks color
  turn 4 [customer] For that, what matters is: 100% Leather.
  ...

SCENARIO: boundary — public_0035
  turn 1 [customer] I'm looking for Athletic Walking, but I'm still exploring.
  turn 1 [agent]    asks material
  turn 2 [customer] I don't have a preference for material; please use your judgment.
  turn 2 [agent]    asks color
  turn 3 [customer] I don't have an additional preference for color.
  ...
```
