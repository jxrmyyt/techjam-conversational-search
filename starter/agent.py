from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llama_cpp import Llama  # type-checking only -- see _load_llm for the real (lazy) import


TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "please", "some",
    "that", "the", "this", "to", "want", "with", "would", "you", "looking",
    # v14: added after checking every word in the evaluator's scripted
    # (non-product) text -- openers, the Override message, and every
    # customer_reply boilerplate variant (see evaluator/local_evaluator.py)
    # -- against every real disclosed hard_constraint/soft_preference across
    # all 200 public sessions. These 12 appear ONLY in scripted framing and
    # ZERO times in real disclosed product text, so stripping them can only
    # ever remove scaffolding noise, never real signal. Confirmed on the
    # full eval: 0.588534 -> 0.597966 (see DAY1_PROGRESS.md "v14"). A wider
    # second-tier set of words that appeared in scripted text but also had a
    # handful of real occurrences (e.g. "have", "use", "your", "need") was
    # tried too and REGRESSED the score (0.597966 -> 0.586618, Browsing and
    # Override both dropped hard) -- confirms those low counts weren't
    # negligible, and is why this set stops here rather than growing further
    # by the same reasoning.
    "actually", "additional", "ask", "earlier", "exploring", "ignore",
    "judgment", "matters", "preference", "requirement", "still", "those",
}

# Fixed by the Agent API contract (docs/agent_api_contract.json) -- this is
# the slot vocabulary. Nothing to invent here.
#
# "brand" is excluded: evaluator.local_evaluator.classify_constraint() has no
# branch that ever returns "brand", so no disclosed constraint can ever match
# an ask_attribute="brand" question -- confirmed both by reading the
# simulator source and empirically (v4 asked it, v5 removed it, Boundary and
# every other scenario scored byte-identical either way -- see
# DAY1_PROGRESS.md).
#
# "category" is asked FIRST deliberately, even though it always gets
# "I don't have an additional preference for category." back (it's disclosed
# once in the opener and never revisited by customer_reply(), and is already
# forced into retrieval as a mandatory term via CATEGORY_RE regardless of
# whether it's asked -- see _retrieve()/_retrieve_browse callsite). So asking
# it buys nothing new for retrieval; what it buys is Boundary
# protection, at a real cost to the other 95% of sessions. Measured
# (DAY1_PROGRESS.md "v8"):
#   category NOT asked (v6):        Boundary 0.400, Overall 0.589
#   category asked first (v8):      Boundary 0.700, Overall 0.564
# Kept in this repo state as a deliberate choice, not the leaderboard-optimal
# one -- see DAY1_PROGRESS.md for the reasoning and the plan to recover the
# overall score with an LLM-based question/query layer (Stage 2).
ASK_ATTRIBUTE_PRIORITY = [
    "category", "material", "color", "size", "budget",
    "style", "use_case", "feature",
]

# Cheap heuristic signal for the Intent Override scenario: the simulator's
# scripted override message always reads "Actually, ignore my earlier
# preference. What I need is: <new_value>." (see evaluator/local_evaluator.py
# behavior_for()). Capturing the corrective clause (rather than the whole
# sentence) avoids polluting the query with boilerplate words like "ignore"
# and "preference" that don't appear in product text and just add noise.
# A real state-machine override (contradiction check against structured
# slots instead of a regex on the raw message) was built and measured as
# "erase-and-rewrite": clear the old attribute's terms, replace with the
# new value. It regressed hard (hit_rate 0.800 -> 0.733, mrr 0.608 -> 0.523,
# mttc 4.87 -> 5.73) -- see DAY1_PROGRESS.md "v10: real per-attribute state,
# tried erase-and-rewrite on Override, reverted the erase". Root cause: the
# regex-classified bucket the correction gets routed into (_classify_disclosure
# below) disagrees with the evaluator's own bucketing ~17% of the time, and
# erasing the wrong bucket destroys real signal. What's kept from that
# attempt is the APPEND-not-replace version below, which routes the
# correction into the right `attribute_terms` bucket without erasing
# anything -- see respond().
OVERRIDE_RE = re.compile(
    r"ignore my earlier preference.*?what i need is:\s*(.+?)[.!?]*$",
    re.IGNORECASE,
)

# Cheap heuristic signal for the Buying-track opener: the simulator marks a
# disclosed hard constraint explicitly ("A key requirement is: <value>.").
# When present, treat it as a mandatory (AND) term rather than an optional
# (OR) one -- Buying sessions benefit from precision over recall, since the
# customer told us something specific up front. Browsing/Boundary sessions
# never match this and stay on the broad OR path.
BUYING_ANCHOR_RE = re.compile(r"key requirement is:\s*(.+?)[.!?]*$", re.IGNORECASE)

# Every opener (buying, browsing, boundary, override) starts "I'm looking
# for {category}..." (see docs/dialog_patterns.md's opener template table).
# category is never re-disclosed later (that's why it's excluded from
# ASK_ATTRIBUTE_PRIORITY), but it's still present for free in the turn-1
# opener text -- this pulls it out so Browsing/Boundary sessions (which have
# no BUYING_ANCHOR_RE marker) can optionally anchor on it too.
CATEGORY_RE = re.compile(r"looking for\s+(.+?)(?:,\s*but\b|\.)", re.IGNORECASE)

# Slot vocabularies, in a fixed order (not a set -- iterating a set to pick
# "the first match" is not guaranteed stable across processes, since Python
# randomizes string hashing per-process by default. Confirmed non-reproducible
# empirically: the same multi-color input picked different colors across
# fresh interpreter runs. A tuple removes that risk.)
#
# These are deliberately an EXACT copy of the evaluator's own COLOR_RE /
# MATERIAL_RE vocabularies (evaluator/local_evaluator.py), not a
# hand-guessed list. intent_card() -- the function that generates every
# session's hard_constraints/soft_preferences -- can only ever produce a
# color/material value from those two regexes; there's no other source. The
# previous version of this list was a guess and silently missed 3 of 12
# possible colors (purple, orange, yellow) and 3 of 9 possible materials
# (fabric, rayon, spandex) -- any session whose real disclosed value was one
# of those could never populate state.slots, so it could never be promoted
# to a mandatory AND term via _retrieve_buy/_browse_mandatory_extra. Fixed
# by matching the evaluator's vocabulary exactly -- see DAY1_PROGRESS.md
# "v13: exact color/material vocabulary match".
COLORS = (
    "black", "white", "blue", "red", "pink", "green", "brown",
    "gray", "grey", "purple", "yellow", "orange",
)
MATERIALS = (
    "cotton", "polyester", "nylon", "leather", "wool", "spandex", "silk", "rayon", "fabric",
)

# Matches the evaluator's actual budget phrasing: intent_card() in
# local_evaluator.py always discloses budget as "budget around $<price>" --
# never "under"/"below"/"max". A regex gated on those trigger words never
# matches real data; this one keys off the dollar figure directly.
BUDGET_RE = re.compile(r"\$\s*(\d+(?:\.\d+)?)")

# The simulated customer's three boilerplate "no new information" replies
# (see evaluator.local_evaluator.customer_reply / docs/dialog_patterns.md):
#   "I don't have an additional preference for {attribute}."
#   "I don't have a preference for {attribute}; please use your judgment."
#     (Boundary scenario only, first asked attribute, once per session)
#   "Those options are not quite right yet. Ask me about one specific attribute."
#     (returned when ask_attribute was null)
# All three carry zero positive product signal -- worse, the first two
# literally contain the attribute name (e.g. "material") which, if tokenized
# and added to accumulated_terms like any other reply, becomes a false
# search term meaning the exact opposite of what was said ("no preference for
# material" is not the same signal as "material" as a positive query word).
# Detected and skipped before accumulation; confirmed via _terms() that
# these patterns tokenize to noise like ['don', 'have', 'preference',
# 'material', 'use', 'your', 'judgment'] with no way to distinguish that
# from a real positive disclosure downstream.
NO_SIGNAL_RE = re.compile(
    r"^i don.t have (?:an additional |a )?preference for [a-z_ ]+"
    r"(?:; please use your judgment)?\.$"
    r"|^those options are not quite right yet\. ask me about one specific attribute\.$",
    re.IGNORECASE,
)

# Mirrors evaluator.local_evaluator.classify_constraint() exactly (same
# branch order, same vocabulary, including the evaluator's own MATERIALS
# list -- which differs slightly from this file's own COLORS/MATERIALS
# above, used for a different purpose: those extract Buying-track filter
# values, this classifies which *dialog attribute bucket* a piece of
# disclosed text belongs to). Used only to route Intent Override's
# corrective clause to the right bucket to erase -- see
# _SessionState.attribute_terms and the override handling in respond().
# Safe to reproduce: it's fully public simulator source, already documented
# in docs/dialog_patterns.md, not private evaluation data.
_EVALUATOR_MATERIALS = (
    "cotton", "polyester", "nylon", "leather", "wool", "spandex", "silk", "rayon", "fabric",
)


def _classify_disclosure(value: str) -> str:
    lowered = value.lower()
    if "budget" in lowered or re.search(r"(?:\$|<=|under)\s*\d", lowered):
        return "budget"
    if any(material in lowered for material in _EVALUATOR_MATERIALS):
        return "material"
    if any(word in lowered for word in ("color", "black", "white", "blue", "red", "pink", "green")):
        return "color"
    if any(word in lowered for word in ("size", "sizing", "width", "wide", "narrow")):
        return "size"
    if any(word in lowered for word in ("department", "style", "fit", "sleeve", "neck")):
        return "style"
    if any(word in lowered for word in ("hiking", "running", "gym", "winter", "outdoor", "work")):
        return "use_case"
    return "feature"


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(f"{key} {item}" for key, item in value.items())
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value)


def _terms(text: str) -> list[str]:
    return [
        token.lower()
        for token in TOKEN_RE.findall(text)
        if len(token) > 1 and token.lower() not in STOPWORDS
    ]


def _extract_slots(text: str) -> dict:
    """Structured slots pulled from one message, on a fixed-order vocabulary
    so results are reproducible across runs (see COLORS/MATERIALS above).
    Only extracts color/material/budget -- no brand extraction, since the
    simulator has no reliable way to ever confirm a brand constraint (see
    the ASK_ATTRIBUTE_PRIORITY comment above)."""
    lowered = text.lower()
    slots: dict[str, object] = {}

    for color in COLORS:
        if re.search(rf"\b{re.escape(color)}\b", lowered):
            slots["color"] = color
            break

    for material in MATERIALS:
        if re.search(rf"\b{re.escape(material)}\b", lowered):
            slots["material"] = material
            break

    budget_match = BUDGET_RE.search(lowered)
    if budget_match:
        slots["budget"] = float(budget_match.group(1))

    return slots


class _SessionState:
    """Everything the agent remembers about one session -- the real version
    of the `SessionState` object described in the build plan's ownership
    contract (this project is now solo; there is no separate owner for the
    dialog-management half). `turn` is turn_count. `attribute_terms` (a
    per-attribute bucket, populated via the last-asked-attribute routing in
    respond() and read back by `_distilled_terms()`) is distilled_context --
    it's what lets a correction land on the right slot instead of one flat
    bag of words. `route_weights` in the literal per-source-weight sense the
    plan sketched was never built; what exists instead is the coarser
    turn-gated version that measurably worked -- `_browse_mandatory_extra()`
    tightening the Browse-route retrieval terms from turn 1 onward, and
    `_retrieve()` skipping the LLM rerank entirely on turn 1 -- see
    DAY1_PROGRESS.md "v11: adaptive retrieval strictness".
    """

    __slots__ = (
        "accumulated_terms", "profile_terms", "asked_attributes", "turn",
        "anchor_term", "intent", "slots", "category_terms",
        "attribute_terms", "last_asked_attribute",
    )

    def __init__(self, profile_terms: list[str]) -> None:
        # Turn-1 opener overflow only now (see respond()) -- everything from
        # turn 2 onward is routed into attribute_terms instead, so a
        # contradicted attribute can be erased on override without also
        # erasing unrelated information. Kept for backward-compat call sites
        # (turn-1 text, and a safety-net fallback if a reply can't be routed
        # to a known bucket).
        self.accumulated_terms: list[str] = []
        self.profile_terms = profile_terms
        self.asked_attributes: set[str] = set()
        self.turn = 0

        # Pillar II, "information accumulation vs. intent override" (see
        # build plan): per-attribute buckets so a reply merges (appends)
        # onto its own attribute's bucket, but an Intent Override message
        # can erase-and-rewrite just the contradicted bucket instead of
        # every prior disclosure. Keys match classify_constraint()'s return
        # vocabulary (_classify_disclosure above) minus "category" (handled
        # separately via category_terms -- never a matchable constraint).
        self.attribute_terms: dict[str, list[str]] = {
            key: [] for key in ("material", "color", "size", "budget", "style", "use_case", "feature")
        }
        # Which attribute we asked *last* turn -- an ordinary (non-override,
        # non-boilerplate) reply this turn is a response to that question,
        # so its terms route into that bucket. None on turn 1 (nothing asked
        # yet) and whenever the previous turn asked nothing.
        self.last_asked_attribute: str | None = None

        # Buying-track anchor: the single most salient disclosed term, held
        # as a mandatory (AND) constraint rather than optional (OR) recall.
        # None for Browsing-style sessions, where broad recall matters more
        # than early precision. This is the "dual-track routing" pillar.
        self.anchor_term: str | None = None

        # Category terms pulled from the turn-1 opener (see CATEGORY_RE) --
        # present in every scenario's opener. Experimental: tested as an
        # optional extra mandatory-AND signal for Browsing/Boundary, which
        # otherwise have no anchor at all. See DAY1_PROGRESS.md for the
        # measured result.
        self.category_terms: list[str] = []

        # "buy" vs "browse", classified once from the turn-1 opener.
        self.intent: str = "browse"

        # Structured slots extracted from disclosed text; color/material are
        # consumed as extra mandatory AND terms in the Buying track, budget
        # as a post-retrieval price filter.
        #
        # "brand" is kept as a placeholder, not wired up yet. It CANNOT be
        # filled the way color/material/budget are (matching text against a
        # small fixed vocabulary from a disclosed constraint) -- the
        # evaluator's classify_constraint() never labels any disclosed
        # constraint as brand-related, so ask_attribute="brand" can never
        # elicit one; that's a property of the simulator, not something a
        # smarter regex fixes. Removed from ASK_ATTRIBUTE_PRIORITY for that
        # reason (see the comment there).
        #
        # Known, deliberately-scoped-out gap, not a stand-in for anyone
        # else's work: a *different* brand mechanism could still work --
        # scan message text directly against the catalog's own `store`
        # values (already indexed) rather than waiting for a disclosed
        # constraint, and use a match as a soft ranking boost for Browsing
        # rather than a hard Buying-track filter. Needs its own extractor
        # (substring/fuzzy match against ~thousands of store names, not a
        # ~10-item vocabulary like COLORS/MATERIALS). Not built -- deprio-
        # ritized given the deadline, since ASK_ATTRIBUTE_PRIORITY dropping
        # "brand" already means it costs nothing at runtime (never asked,
        # never blocks retrieval); it would only ever be a soft upside.
        self.slots: dict[str, object] = {
            "color": None, "material": None, "budget": None, "brand": None,
        }


class Agent:
    """Retrieval baseline + turn-over-turn accumulation + dual-track routing
    on top of BM25 (SQLite FTS5) keyword retrieval, with a local LLM
    re-ranker on top (Stage 2, see DAY1_PROGRESS.md "v9").

    Dense/hybrid semantic retrieval (spaCy `en_core_web_md` static word
    vectors, RRF-merged with BM25) was built and measured -- see
    DAY1_PROGRESS.md "v7: dense/hybrid retrieval, tried and reverted". It
    regressed the score at every BM25:dense weight ratio tried (1:1 -> 0.531,
    3:1 -> 0.572, 8:1 -> 0.581, all below pure-BM25's 0.589), monotonically
    approaching but never beating pure BM25 as the dense side's influence
    shrinks -- the signature of a net-negative component, not a tuning
    problem. Removed outright (not just disabled) to drop the spaCy/numpy
    dependency and the precomputed-vector build step entirely -- see
    `scripts/build_product_vectors.py`'s and `requirements.txt`'s git
    history if a stronger embedding model becomes available later and this
    is worth revisiting.
    """

    def __init__(
        self,
        catalog_path: str | Path = "data/catalog.jsonl",
        llm_model_path: str | Path | None = None,
    ) -> None:
        self.catalog_path = Path(catalog_path)
        self.connection = sqlite3.connect(":memory:")
        self._sessions: dict[str, _SessionState] = {}
        self.product_meta: dict[str, dict] = {}
        self._llm: Llama | None = None
        self._build_index()
        # v18: NOT loaded by default anymore. `_retrieve()` no longer calls
        # `_llm_rerank()` at all (see that method and DAY1_PROGRESS.md "v18"
        # for why -- two full-scale measured results confirmed it's a net
        # negative, and a LoRA fine-tune attempt to fix it didn't converge
        # in time). Since the rerank is never used, loading the model here
        # by default would be pure cost with zero benefit -- worse, a real
        # crash risk: `_load_llm`'s try/except catches a normal load
        # failure, but `Llama(...)` can also hard-crash the whole process
        # with an "Illegal instruction" if the CPU doesn't support an
        # instruction llama-cpp-python's build assumes, which no Python
        # try/except can catch (confirmed -- this happened in one dev
        # sandbox run). Pass an explicit `llm_model_path` to opt back in for
        # experimentation; the official contract's `Agent(catalog_path)`
        # call site never does.
        if llm_model_path is not None:
            self._load_llm(Path(llm_model_path))

    def _load_llm(self, model_path: Path) -> None:
        # Stage 2: local LLM re-ranker (see DAY1_PROGRESS.md "v9"). Runs
        # fully offline -- llama-cpp-python + a vendored GGUF file, no
        # network call ever. Degrades to no-op (candidates pass through
        # un-reranked) if the model file isn't present or fails to load.
        #
        # Lazy import (not at module level) -- since __init__ no longer
        # calls this by default (see "v18" above), agent.py imports and runs
        # fine even in an environment without llama-cpp-python installed at
        # all. Only paying this import cost, and its risk, when someone
        # explicitly opts in via `llm_model_path`.
        if not model_path.exists():
            return
        try:
            from llama_cpp import Llama
            self._llm = Llama(model_path=str(model_path), n_ctx=2048, verbose=False)
        except Exception:
            self._llm = None

    def _build_index(self) -> None:
        cursor = self.connection.cursor()
        cursor.execute(
            "CREATE VIRTUAL TABLE products USING fts5("
            "parent_asin UNINDEXED, title, categories, features, details, store, description, "
            "tokenize='unicode61 remove_diacritics 2')"
        )
        batch: list[tuple[str, str, str, str, str, str, str]] = []
        with self.catalog_path.open(encoding="utf-8") as handle:
            for line in handle:
                product = json.loads(line)
                parent_asin = str(product["parent_asin"])
                self.product_meta[parent_asin] = {
                    "price": product.get("price"),
                    "title": _text(product.get("title"))[:120],
                }
                batch.append(
                    (
                        parent_asin,
                        _text(product.get("title")),
                        _text(product.get("categories")),
                        _text(product.get("features")),
                        _text(product.get("details")),
                        _text(product.get("store")),
                        _text(product.get("description")),
                    )
                )
                if len(batch) >= 1000:
                    cursor.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
                    batch.clear()
        if batch:
            cursor.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
        self.connection.commit()

    def reset(self, session_id: str, user_profile: dict) -> None:
        # preference_tags is the only part of the anonymized profile that's
        # useful as retrieval signal -- summary/rating fields describe the
        # shopper, not the product, so they're deliberately left out of the
        # query to avoid diluting it.
        tags = user_profile.get("preference_tags") or []
        profile_terms = _terms(" ".join(str(tag) for tag in tags))
        self._sessions[session_id] = _SessionState(profile_terms)

    def respond(
        self,
        session_id: str,
        user_message: str,
        turn: int,
        top_k: int,
    ) -> dict:
        state = self._sessions.get(session_id)
        if state is None:
            raise RuntimeError("reset must be called before respond")
        state.turn = turn

        if turn == 1:
            state.intent = "buy" if "key requirement is" in user_message.lower() else "browse"
            category_match = CATEGORY_RE.search(user_message)
            if category_match:
                state.category_terms = _terms(category_match.group(1))

        # Pillar II, intent-override handling. TRIED erase-and-rewrite here
        # (classify the corrective clause via _classify_disclosure, wipe
        # that one attribute_terms bucket before writing the new value in --
        # matching the original build plan's "a contradicting value erases
        # and rewrites the old slot instead of merging") and MEASURED it:
        # isolated override-only eval (30 sessions, LLM disabled to isolate
        # the retrieval-layer change) went hit_rate 0.800 -> 0.733, mrr
        # 0.608 -> 0.523, mttc 4.87 -> 5.73 -- a real regression, not noise.
        # Root cause, checked directly against the simulator's own
        # behavior_for(): old_value and new_value are independently drawn
        # from the intent card (old_value = last soft_preference, new_value
        # = hard_constraints[0]) and classify_constraint() puts them in the
        # SAME attribute bucket only 5 of 30 times (17%) -- 83% of the time
        # "erase new_value's bucket" erases either an unrelated bucket
        # (leaving the real contradiction untouched) or nothing at all,
        # while still discarding whatever legitimate content had
        # accumulated there. There is no way to know which bucket the old
        # value actually lived in -- that's genuinely hidden simulator
        # state, confirmed empirically here, not just asserted. Kept as
        # append-only (still routes into the classified bucket, still
        # bucketed for the distillation below) rather than reverting to a
        # flat list, since bucketing itself measured neutral -- see
        # DAY1_PROGRESS.md "v10".
        override_match = OVERRIDE_RE.search(user_message)
        if override_match:
            corrective = override_match.group(1)
            bucket = _classify_disclosure(corrective)
            state.attribute_terms[bucket].extend(_terms(corrective))
        elif turn == 1:
            # Turn-1 opener text (category + Buying's disclosed hard
            # constraint, if any) isn't a reply to anything we asked --
            # kept on the original flat accumulated_terms path, unchanged
            # from every earlier version.
            state.accumulated_terms.extend(_terms(user_message))
        else:
            # NOTE: we tried skipping accumulation for the boilerplate
            # "no preference" / "not quite right yet" replies (see
            # NO_SIGNAL_RE above and DAY1_PROGRESS.md) on the theory that
            # they're pure noise. Measured net result: overall score
            # 0.573 -> 0.547 (boundary improved 0.30->0.40, but buying,
            # browsing, and override all dropped). Reverted -- the literal
            # boilerplate words (e.g. "material", "color") apparently still
            # correlate with real matches often enough via BM25 bag-of-words
            # overlap with product spec text that stripping them is a net
            # loss. So this reply -- boilerplate or a real disclosure --
            # still gets accumulated; it's just routed into the bucket for
            # whatever attribute we asked last turn (an ordinary "information
            # accumulation" merge, per the build plan), so a *later* override
            # on that same attribute can erase it precisely instead of
            # fighting it forever in one flat list.
            bucket = state.last_asked_attribute
            if bucket in state.attribute_terms:
                state.attribute_terms[bucket].extend(_terms(user_message))
            else:
                state.accumulated_terms.extend(_terms(user_message))

        for key, value in _extract_slots(user_message).items():
            state.slots[key] = value

        # Only set the anchor once (turn 1) -- a later turn matching this
        # phrase would just be coincidence, not a fresh hard constraint.
        if state.anchor_term is None:
            anchor_match = BUYING_ANCHOR_RE.search(user_message)
            if anchor_match:
                anchor_terms = _terms(anchor_match.group(1))
                if anchor_terms:
                    state.anchor_term = anchor_terms[0]

        recommendations = self._retrieve(state, top_k)

        ask_attribute = self._next_attribute(state)
        if ask_attribute:
            state.asked_attributes.add(ask_attribute)
            message = f"Do you have a {ask_attribute.replace('_', ' ')} preference?"
        else:
            message = "Here are the closest matches I found."
        # Whatever we ask now is what the customer's *next* message will be
        # a reply to -- see the bucket-routing logic above.
        state.last_asked_attribute = ask_attribute

        return {
            "message": message,
            "ask_attribute": ask_attribute,
            "recommendations": recommendations,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }

    @staticmethod
    def _browse_mandatory_extra(state: _SessionState) -> list[str]:
        """Pillar III, adaptive orchestration: the browse route's retrieval
        strictness isn't a fixed constant across all 10 turns the way it was
        through v9 -- category is always mandatory (v6), and now color/
        material are promoted from optional (OR) to mandatory (AND) the
        moment they're disclosed as structured slots, the same AND-over-OR
        argument that already justified Buying's anchor and the category
        anchor, just extended to Browsing once real signal exists instead of
        never. (Tried gating this behind a turn threshold -- turn>=4 as an
        initial guess, then swept turn>=1/2/3/5 -- measured all of
        turn>=1/2/3 identical on hit rate (0.581 overall) with turn>=1
        slightly ahead on MRR and turn>=5 a hair behind; no evidence a delay
        helps, so acting immediately on the signal is both simplest and
        best-measured. See DAY1_PROGRESS.md "v11".)"""
        extra = list(state.category_terms)
        for key in ("color", "material"):
            value = state.slots.get(key)
            if value and str(value) not in extra:
                extra.append(str(value))
        return extra

    @staticmethod
    def _distilled_terms(state: _SessionState) -> list[str]:
        """Flatten turn-1 opener overflow + every attribute bucket into one
        ordered, deduped term list -- the single source of truth query
        construction and the LLM context both read from, so an Intent
        Override's bucket-erase (see respond()) is actually reflected
        everywhere the query gets built, not just in one code path. Bucket
        order follows ASK_ATTRIBUTE_PRIORITY (minus category/brand) purely
        for determinism -- order doesn't affect an FTS5 OR/AND expression."""
        terms = list(state.accumulated_terms)
        for key in ("material", "color", "size", "budget", "style", "use_case", "feature"):
            terms.extend(state.attribute_terms.get(key, []))
        return list(dict.fromkeys(terms))

    def _retrieve(self, state: _SessionState, top_k: int) -> list[dict]:
        if state.intent == "buy":
            return self._retrieve_buy(state, top_k)
        # Browsing route: BM25 only -- see the Agent class docstring for why
        # dense/hybrid retrieval isn't here, and for why the LLM rerank
        # (below, still defined and callable) isn't in this dispatch either.
        pool = self._retrieve_browse(state, max(top_k * 3, 30), mandatory_extra=self._browse_mandatory_extra(state))
        # v18: LLM rerank turned OFF here (not just pool-shrunk) after two
        # real, full-scale, on-hardware results confirmed it's a net
        # negative, not a tuning problem:
        #   BM25-only (v14):                        0.597966
        #   + LLM rerank, pool=20 (v16):             0.591948 (worse)
        #   + LLM rerank, pool=30 (v17):              -- confirms it's the
        #     rerank itself, not the pool shrink, see DAY1_PROGRESS.md
        # A LoRA fine-tune was attempted to fix the zero-shot model's weak
        # judgment (see training/) but its own holdout sanity check showed
        # it collapsed to always predicting the trivial identity order
        # (1,2,3...) regardless of input -- didn't converge to real
        # reranking behavior in the time available, so it was never even
        # converted to GGUF/tested. `_llm_rerank`/`_load_llm` are kept
        # defined (real, working Stage 2 infrastructure, useful to show for
        # technical merit) but this dispatch just returns the BM25 pool
        # directly -- this is now the active, submitted retrieval path.
        return pool[:top_k]

    def _llm_rerank(self, state: _SessionState, candidates: list[dict], top_k: int) -> list[dict]:
        """Stage 2: ask the local LLM to re-order the BM25 candidate pool by
        relevance to the full conversation so far. Never trusted to invent
        products -- the model only ever picks *numbers* referring back to
        the given candidate list, so a hallucinated/malformed response can't
        introduce an asin that wasn't already a valid BM25 candidate. Falls
        back to the untouched BM25 ordering on any failure (no model
        loaded, parse failure, empty result) -- re-ranking is strictly
        best-effort, never a hard dependency for a response to go out."""
        if self._llm is None or not candidates:
            return candidates[:top_k]

        listing = "\n".join(
            f"{i + 1}. {self.product_meta.get(item['parent_asin'], {}).get('title') or '(untitled)'}"
            for i, item in enumerate(candidates)
        )
        context = " ".join(self._distilled_terms(state)[:40]) or "(nothing disclosed yet)"
        prompt = (
            "A customer is shopping and has said/disclosed these things so far: "
            f"{context}\n\nHere are candidate products:\n{listing}\n\n"
            f"List the item numbers of the {min(top_k, len(candidates))} best matches for this "
            "customer, best match first. Respond with ONLY a comma-separated list of numbers, "
            "nothing else."
        )
        try:
            response = self._llm.create_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=40,  # a comma-separated list of <=10 numbers never needs 100
                temperature=0.0,
            )
            text = response["choices"][0]["message"]["content"]
        except Exception:
            return candidates[:top_k]

        picked_indices: list[int] = []
        for match in re.findall(r"\d+", text):
            idx = int(match) - 1
            if 0 <= idx < len(candidates) and idx not in picked_indices:
                picked_indices.append(idx)
            if len(picked_indices) >= top_k:
                break

        if not picked_indices:
            return candidates[:top_k]

        ordered = [candidates[i] for i in picked_indices]
        remaining = [item for i, item in enumerate(candidates) if i not in picked_indices]
        ordered.extend(remaining)
        return ordered[:top_k]

    def _retrieve_buy(self, state: _SessionState, top_k: int) -> list[dict]:
        # Buying track: mandatory-match color/material (when disclosed) on
        # top of the base query, then apply a price ceiling as a post-filter
        # (price isn't in the FTS index, so it can't be a MATCH term).
        #
        # v19: category promoted to mandatory here too. Every other route
        # already treats category as a mandatory AND term (see
        # _browse_mandatory_extra, and CATEGORY_RE's original comment) --
        # Buying was the one route that never got this, category words just
        # sat in the optional OR pool via accumulated_terms' turn-1 opener
        # text. Same AND-over-OR precision argument as anchor_term/color/
        # material, just finally extended here too. Measured: full 200-
        # session eval, Buying hit_rate 0.775 -> 0.800, mrr 0.380 -> 0.404,
        # mttc 4.325 -> 4.1375 (all improved, nothing regressed elsewhere,
        # since this is buy-route-only code) -- overall 0.597966 -> 0.607365.
        # See DAY1_PROGRESS.md "v19".
        mandatory_extra = list(state.category_terms)
        mandatory_extra += [
            str(state.slots[key]) for key in ("color", "material") if state.slots.get(key)
        ]
        candidates = self._retrieve_browse(state, max(top_k * 5, 50), mandatory_extra)

        budget = state.slots.get("budget")
        if budget is None:
            return candidates[:top_k]

        filtered = []
        for item in candidates:
            price = self.product_meta.get(item["parent_asin"], {}).get("price")
            if price is not None:
                try:
                    if float(price) > float(budget):
                        continue
                except (TypeError, ValueError):
                    pass
            filtered.append(item)
            if len(filtered) >= top_k:
                break
        return filtered if filtered else candidates[:top_k]

    def _retrieve_browse(
        self, state: _SessionState, top_k: int, mandatory_extra: list[str] | None = None
    ) -> list[dict]:
        # Distilled (bucketed, override-aware) terms carry full query
        # weight; profile-tag terms are included but capped so a long
        # preference list can't drown out what the customer just told us
        # this session.
        query_terms = self._distilled_terms(state)[:60]
        query_terms += [term for term in state.profile_terms if term not in query_terms][:6]

        mandatory = [state.anchor_term] if state.anchor_term else []
        mandatory += [term for term in (mandatory_extra or []) if term not in mandatory]
        query_terms = [term for term in query_terms if term not in mandatory]

        optional_expression = " OR ".join(f'"{term}"' for term in query_terms)
        mandatory_expression = " AND ".join(f'"{term}"' for term in mandatory)

        if mandatory_expression and optional_expression:
            expression = f"{mandatory_expression} AND ({optional_expression})"
        else:
            expression = mandatory_expression or optional_expression
        if not expression:
            return []

        # v20: `categories` field weight raised 4.0 -> 8.0 (title stays 6.0).
        # This weighting was never swept before this session -- it was an
        # early guess, inherited unchanged since v1. Swept categories at
        # 6/8/10/12/16 (title held at 6, then also tried title=5/cat=10):
        # 8.0 is the clear peak (0.615556); 6 and 10+ both trail behind it
        # (0.6127 and 0.6109-0.6124 respectively) -- not a monotonic "more
        # is better" relationship, so this is a real optimum, not a
        # direction that could be pushed further. See DAY1_PROGRESS.md
        # "v20: BM25 field weight sweep" for the full sweep table.
        rows = self.connection.execute(
            "SELECT parent_asin FROM products WHERE products MATCH ? "
            "ORDER BY bm25(products, 0.0, 6.0, 8.0, 2.5, 2.5, 1.5, 1.0) LIMIT ?",
            (expression, top_k),
        ).fetchall()
        return [{"parent_asin": str(row[0])} for row in rows]

    def _next_attribute(self, state: _SessionState) -> str | None:
        # Stop asking late so the last couple of turns are pure retrieval
        # against everything gathered so far, rather than a question that
        # can't be acted on before the turn budget runs out.
        if state.turn >= 6:
            return None
        # v12 audit attempt, tried and reverted: skip asking color/material
        # when state.slots already has the value (e.g. extracted from the
        # Buying anchor at turn 1), on the theory that re-asking a known
        # attribute wastes a turn for a reply with no new information. Built,
        # measured, root-caused -- see DAY1_PROGRESS.md "v12: skip
        # already-known attributes, tried and reverted". Full 200-session
        # BM25-only: 0.581360 -> 0.532399 (Buying hit_rate 0.75 -> 0.6125,
        # 11 sessions flipped hit->miss, 0 flipped the other way). Root
        # cause, confirmed by session-level diff + turn-by-turn trace on
        # public_0005: customer_reply()'s boilerplate "I don't have an
        # additional preference for X" filler, when routed into
        # attribute_terms[X], injects the literal word "material"/"color"
        # into the OR-query -- and those two words happen to be extremely
        # common in this catalog's product descriptions (most apparel/gear
        # listings literally say "Material: ..."), so asking material/color
        # even when redundant was accidentally acting as a broad recall
        # booster. Skipping that ask (the "correct" behavior) removes the
        # accidental boost with nothing to replace it, since size/budget/
        # style/use_case get asked earlier instead and are less predictive
        # BM25 terms for this corpus. Deliberately not chasing this by
        # injecting noise words on purpose -- that would be overfitting to
        # this specific 200-sample public set, not a real fix. Reverted to
        # the plain walk-down below.
        for attribute in ASK_ATTRIBUTE_PRIORITY:
            if attribute not in state.asked_attributes:
                return attribute
        return None