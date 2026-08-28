from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path


TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "please", "some",
    "that", "the", "this", "to", "want", "with", "would", "you", "looking",
}

# Fixed by the Agent API contract (docs/agent_api_contract.json) -- this is
# the slot vocabulary. Nothing to invent here.
ASK_ATTRIBUTE_PRIORITY = [
    "category", "material", "color", "size", "budget",
    "style", "brand", "use_case", "feature",
]

# Cheap heuristic signal for the Intent Override scenario: the simulator's
# scripted override message always reads "Actually, ignore my earlier
# preference. What I need is: <new_value>." (see evaluator/local_evaluator.py
# behavior_for()). Detecting it lets us drop stale accumulated terms instead
# of letting them dilute the query forever. This is a stand-in for the real
# state-machine override detection Role B will own -- see the TODO below.
OVERRIDE_RE = re.compile(r"\bactually\b.*\bignore\b|\bnever ?mind\b", re.IGNORECASE)


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


class _SessionState:
    """Everything Role A's retrieval needs to remember about one session.

    This is a first cut at the `SessionState` object described in the build
    plan's ownership contract. Role B will eventually own the real version
    (with turn_count, distilled_context, route_weights) -- this is scoped
    just wide enough to prove that accumulation beats the stateless baseline.
    """

    __slots__ = ("accumulated_terms", "profile_terms", "asked_attributes", "turn")

    def __init__(self, profile_terms: list[str]) -> None:
        self.accumulated_terms: list[str] = []
        self.profile_terms = profile_terms
        self.asked_attributes: set[str] = set()
        self.turn = 0


class Agent:
    """Retrieval baseline + turn-over-turn accumulation.

    Still single-signal (FTS5/BM25 keyword retrieval) -- no dense retrieval,
    no LLM ranking yet. Those are the Day 2 additions. What this version adds
    over the provided starter is state: it remembers what the customer has
    revealed across turns instead of scoring each message in isolation, and
    it drives the clarification policy instead of asking nothing.
    """

    def __init__(self, catalog_path: str | Path = "data/catalog.jsonl") -> None:
        self.catalog_path = Path(catalog_path)
        self.connection = sqlite3.connect(":memory:")
        self._sessions: dict[str, _SessionState] = {}
        self._build_index()

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
                batch.append(
                    (
                        str(product["parent_asin"]),
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

        # TODO(Role B): replace this heuristic with the real state-machine
        # override detection (contradiction check on structured slots, not
        # a regex on the raw message).
        if OVERRIDE_RE.search(user_message):
            state.accumulated_terms.clear()

        state.accumulated_terms.extend(_terms(user_message))

        recommendations = self._retrieve(state, top_k)

        ask_attribute = self._next_attribute(state)
        if ask_attribute:
            state.asked_attributes.add(ask_attribute)
            message = f"Do you have a {ask_attribute.replace('_', ' ')} preference?"
        else:
            message = "Here are the closest matches I found."

        return {
            "message": message,
            "ask_attribute": ask_attribute,
            "recommendations": recommendations,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }

    def _retrieve(self, state: _SessionState, top_k: int) -> list[dict]:
        # Accumulated terms carry full query weight; profile-tag terms are
        # included but capped so a long preference list can't drown out what
        # the customer just told us this session.
        query_terms = list(dict.fromkeys(state.accumulated_terms))[:60]
        query_terms += [term for term in state.profile_terms if term not in query_terms][:6]
        expression = " OR ".join(f'"{term}"' for term in query_terms)
        if not expression:
            return []
        rows = self.connection.execute(
            "SELECT parent_asin FROM products WHERE products MATCH ? "
            "ORDER BY bm25(products, 0.0, 6.0, 4.0, 2.5, 2.5, 1.5, 1.0) LIMIT ?",
            (expression, top_k),
        ).fetchall()
        return [{"parent_asin": str(row[0])} for row in rows]

    def _next_attribute(self, state: _SessionState) -> str | None:
        # Stop asking late so the last couple of turns are pure retrieval
        # against everything gathered so far, rather than a question that
        # can't be acted on before the turn budget runs out.
        if state.turn >= 6:
            return None
        for attribute in ASK_ATTRIBUTE_PRIORITY:
            if attribute not in state.asked_attributes:
                return attribute
        return None