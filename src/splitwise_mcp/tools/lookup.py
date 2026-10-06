"""Splitwise lookup tools — categories, currencies, and fuzzy name → id resolution.

Two static catalogues (`GET /get_categories`, `GET /get_currencies`) are cached in-process
for 24 h because Splitwise never changes them between calls; `refresh=True` bypasses the
cache. Three resolvers turn a name an LLM heard from a person ("Jon", "the Peru trip",
"groceries") into the id every other tool needs, scored with stdlib
`difflib.SequenceMatcher` on case- and accent-folded strings — no extra dependency.

`SequenceMatcher.ratio()` ignores length and meaning ("daniela" scores 0.923 against
"Daniel"), so a high score alone never picks a person or a group: friends and groups
resolve ONLY on an exact match, a strong fuzzy candidate is reported as `probable`, and
every resolver applies three guards before trusting a fuzzy candidate (query length,
matching digit runs, no other candidate starting with the query).
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from splitwise_mcp.client import get_client
from splitwise_mcp.errors import handle_api_error
from splitwise_mcp.formatters import ResponseFormat, clip_response, fmt_person, to_json
from splitwise_mcp.server import mcp

MAX_DISPLAY_ROWS = 50
CACHE_TTL_SECONDS = 24 * 60 * 60
# A non-exact candidate needs at least this score to be `probable` (friends, groups) or
# `unique_strong` (categories) — and it must be the only one, and pass the guards.
RESOLVED_THRESHOLD = 0.85
# A folded query shorter than this (spaces removed) never gets a fuzzy resolution.
MIN_FUZZY_QUERY_LEN = 4
MAX_RESOLVE_LIMIT = 25
# Parent categories shown under the subcategory candidates in splitwise_resolve_category.
PARENT_ROWS = 3

_DIGIT_RUN_RE = re.compile(r"\d+")
_PIECE_SPLIT_RE = re.compile(r"[/\s]+")
_GROUP_ZERO_NOTE = " — pseudo-group: group_id 0 = no group"

# path -> (monotonic fetch time, items). Only successful, non-empty answers are stored.
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _now() -> float:
    """Monotonic clock (indirected so tests can move time forward)."""
    return time.monotonic()


def clear_cache() -> None:
    """Forget the cached categories and currencies (the next call refetches)."""
    _CACHE.clear()


async def _cached_list(path: str, key: str, *, refresh: bool) -> tuple[list[dict[str, Any]], bool]:
    """Return `(items, from_cache)` for a static catalogue endpoint, honouring the 24 h TTL.

    A failed or empty fetch raises / returns without touching the cache, so a warm entry
    survives a failed `refresh`.
    """
    entry = _CACHE.get(path)
    if not refresh and entry is not None and _now() - entry[0] < CACHE_TTL_SECONDS:
        return entry[1], True
    resp = await get_client().request("GET", path)
    body = resp.json()
    raw = body.get(key) if isinstance(body, dict) else None
    items = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    if items:
        _CACHE[path] = (_now(), items)
    return items, False


def _source_note(from_cache: bool) -> str:
    if from_cache:
        return "_Source: in-process cache (fetched less than 24 h ago; pass `refresh: true` to refetch)._"
    return "_Source: Splitwise API — cached in-process for 24 h._"


def _cell(value: Any) -> str:
    """Make a value safe for a markdown table cell."""
    return " ".join(str(value).replace("|", "\\|").split())


def _fold(text: Any) -> str:
    """Casefold, strip accents and collapse whitespace, so `María  GARCÍA` == `maria garcia`."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(bare.casefold().split())


def _int_id(item: dict[str, Any]) -> int:
    value = item.get("id")
    return value if isinstance(value, int) else -1


# ---------------------------------------------------------------------------
# Scoring and resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Match:
    score: float
    matched_on: str
    matched_text: str
    texts: tuple[str, ...]
    label: str
    item_id: int
    item: dict[str, Any]
    parent: dict[str, Any] | None = None


def _match(
    query: str,
    fields: list[tuple[str, Any]],
    *,
    label: str,
    item: dict[str, Any],
    parent: dict[str, Any] | None = None,
) -> _Match:
    """Score `item` as the best `SequenceMatcher` ratio of the folded query over its `(label, text)` fields.

    Earlier fields win ties, so list the most specific field first.
    """
    best_score, best_label, best_text = 0.0, fields[0][0] if fields else "", ""
    texts: list[str] = []
    for field_label, raw in fields:
        text = _fold(raw)
        if not text:
            continue
        texts.append(text)
        score = SequenceMatcher(None, query, text).ratio()
        if score > best_score:
            best_score, best_label, best_text = score, field_label, text
    return _Match(best_score, best_label, best_text, tuple(texts), label, _int_id(item), item, parent)


def _rank(matches: list[_Match]) -> list[_Match]:
    """Highest score first; ties broken by name then id so the order is deterministic."""
    return sorted(matches, key=lambda m: (-m.score, m.label.casefold(), m.item_id))


def _digits_ok(query: str, match: _Match) -> bool:
    """Guard (c): every digit run in the query is also a digit run of the matched text ("2025" ≠ "2026")."""
    wanted = _DIGIT_RUN_RE.findall(query)
    if not wanted:
        return True
    present = set(_DIGIT_RUN_RE.findall(match.matched_text))
    return all(run in present for run in wanted)


@dataclass(frozen=True)
class _Resolution:
    status: str  # exact | probable | unique_strong | ambiguous | none
    match: _Match | None  # the resolved (exact / unique_strong) or probable candidate
    count: int  # candidates in contention when ambiguous
    reason: str


def _resolve(ranked: list[_Match], query: str, *, fuzzy_status: str, kind: str) -> _Resolution:
    """Decide whether one candidate is THE answer; computed over ALL candidates, not just the top N.

    1. Several exact (1.0) scores → `ambiguous`.
    2. Otherwise the pick is the single exact match, or else the single candidate scoring
       ≥ RESOLVED_THRESHOLD whose digit runs match the query's (guard c); several → `ambiguous`,
       none → `none`.
    3. Guard (d): another candidate with a scored text starting with the query → `ambiguous`.
    4. An exact pick → `exact`. A fuzzy pick needs a query of ≥ MIN_FUZZY_QUERY_LEN characters
       (guard b) and then becomes `fuzzy_status`: `probable` (friends, groups — never resolved)
       or `unique_strong` (categories — resolved).
    """
    exact = [m for m in ranked if m.score >= 1.0]
    if len(exact) > 1:
        return _Resolution("ambiguous", None, len(exact), f"{len(exact)} candidates match exactly")
    if exact:
        pick = exact[0]
    else:
        strong = [m for m in ranked if m.score >= RESOLVED_THRESHOLD]
        eligible = [m for m in strong if _digits_ok(query, m)]
        if len(eligible) > 1:
            return _Resolution(
                "ambiguous", None, len(eligible), f"{len(eligible)} candidates score ≥ {RESOLVED_THRESHOLD:.2f}"
            )
        if not eligible:
            if strong:
                return _Resolution(
                    "none", None, 0, "the closest candidates carry different numbers than the query (e.g. another year)"
                )
            return _Resolution("none", None, 0, f"no candidate scores ≥ {RESOLVED_THRESHOLD:.2f}")
        pick = eligible[0]
    rivals = [m for m in ranked if m is not pick and any(text.startswith(query) for text in m.texts)]
    if rivals:
        count = len(rivals) + 1
        return _Resolution(
            "ambiguous", None, count, f'{count} candidates could be meant: "{query}" is also how another name starts'
        )
    if pick.score >= 1.0:
        return _Resolution("exact", pick, 1, f"exact match on {pick.matched_on}")
    if len(query.replace(" ", "")) < MIN_FUZZY_QUERY_LEN:
        return _Resolution(
            "none", None, 0, f'"{query}" is under {MIN_FUZZY_QUERY_LEN} characters, too short to accept a fuzzy match'
        )
    if fuzzy_status == "probable":
        return _Resolution(
            "probable",
            pick,
            1,
            f"score {pick.score:.3f} on {pick.matched_on}; a {kind} resolves only on an exact match",
        )
    return _Resolution(
        "unique_strong",
        pick,
        1,
        f"the only candidate scoring ≥ {RESOLVED_THRESHOLD:.2f} ({pick.score:.3f}, on {pick.matched_on})",
    )


def _resolution_line(res: _Resolution, display: str) -> str:
    if res.status in ("exact", "unique_strong"):
        return f"**Resolved**: {display} — {res.reason}."
    if res.status == "probable":
        return (
            f"**Not resolved.** Probable match — confirm with the user before using this id: {display} ({res.reason})."
        )
    if res.status == "ambiguous":
        return f"**Not resolved**: {res.reason} — ask the user which one (or use the id) instead of guessing."
    return f"**Not resolved**: {res.reason} — confirm with the user before using any id below."


def _resolve_payload(query: str, res: _Resolution) -> dict[str, Any]:
    return {
        "query": query,
        "resolution": res.status,
        "ambiguous_count": res.count if res.status == "ambiguous" else 0,
        "reason": res.reason,
    }


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class GetCategoriesInput(BaseModel):
    """Input for splitwise_get_categories."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    parent: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description=(
            "Only show parent categories whose name contains this text (case-insensitive), or whose id equals "
            "it when numeric — e.g. 'food' or '25'. Omit for every category."
        ),
    )
    refresh: bool = Field(default=False, description="Bypass the 24 h in-process cache and refetch from Splitwise.")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="'markdown' (default) for a table, 'json' for the raw objects."
    )


class GetCurrenciesInput(BaseModel):
    """Input for splitwise_get_currencies."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description="Only show currencies whose code or unit contains this text (case-insensitive), e.g. 'pen'.",
    )
    refresh: bool = Field(default=False, description="Bypass the 24 h in-process cache and refetch from Splitwise.")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="'markdown' (default) for a table, 'json' for the raw objects."
    )


class _ResolveBase(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., min_length=1, max_length=100, description="The name as the user said it, e.g. 'Jon'.")
    limit: int = Field(
        default=5, ge=1, le=MAX_RESOLVE_LIMIT, description=f"How many candidates to return (1-{MAX_RESOLVE_LIMIT})."
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' (default) for a ranked table, 'json' for the candidates with their scores.",
    )


class ResolveFriendInput(_ResolveBase):
    """Input for splitwise_resolve_friend."""


class ResolveGroupInput(_ResolveBase):
    """Input for splitwise_resolve_group."""


class ResolveCategoryInput(_ResolveBase):
    """Input for splitwise_resolve_category."""

    refresh: bool = Field(default=False, description="Bypass the 24 h category cache and refetch from Splitwise.")


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


def _subcategories(parent: dict[str, Any]) -> list[dict[str, Any]]:
    subs = parent.get("subcategories")
    return [sub for sub in subs if isinstance(sub, dict)] if isinstance(subs, list) else []


def _named(item: dict[str, Any]) -> str:
    return f"{item.get('name') or 'unnamed'} (id {item.get('id', 'N/A')})"


def _parent_matches(parent: dict[str, Any], needle: str) -> bool:
    if needle.isdigit() and str(parent.get("id")) == needle:
        return True
    return _fold(needle) in _fold(parent.get("name"))


@mcp.tool(
    name="splitwise_get_categories",
    annotations=ToolAnnotations(
        title="Splitwise Expense Categories",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_get_categories(params: GetCategoriesInput) -> str:
    """List Splitwise's expense categories: each parent with its subcategories and their ids.

    Calls `GET /get_categories` once and caches the answer in-process for 24 h (the list is
    static); `refresh: true` refetches. **Expenses must use a subcategory id** as
    `category_id` — a parent id is not accepted. When nothing fits, use the "Other"
    subcategory under the closest parent (or "General" under "Uncategorized").

    When to Use:
    - To browse the categories before creating or recategorising an expense.
    - To list one parent's subcategories (`parent: "food"`).

    When NOT to Use:
    - To turn a category name into an id — use `splitwise_resolve_category`, which ranks
      subcategories by similarity and says when one is a clear match.
    - For currencies — use `splitwise_get_currencies`.

    Returns:
    A markdown table with one row per parent (flagged as not usable for expenses) and its
    subcategories as `Name (id N)`, capped at 50 parents; or the raw `{"categories": [...]}`
    objects (with icons and nested `subcategories`) when `response_format` is `json`.

    Examples:
        params = {}
        params = {"parent": "transport"}
        params = {"refresh": true, "response_format": "json"}

    Error Handling:
    A 401 means the API key is missing or was regenerated. A failed fetch is never cached
    (a failed `refresh` keeps the previous cached list); a parent filter that matches
    nothing lists the parent names to pick from.
    """
    try:
        categories, from_cache = await _cached_list("/get_categories", "categories", refresh=params.refresh)
        needle = params.parent
        selected = [c for c in categories if needle is None or _parent_matches(c, needle)]
        if params.response_format is ResponseFormat.JSON:
            return clip_response(to_json({"categories": selected}))
        if not categories:
            return "Splitwise returned no categories (an empty answer is not cached — try again later)."
        if not selected:
            names = ", ".join(_named(c) for c in categories)
            return f"No parent category matches {needle!r}. Parent categories: {names}.\n\n{_source_note(from_cache)}"
        shown = selected[:MAX_DISPLAY_ROWS]
        lines = [
            "# Splitwise categories" + (f" — parent matching {needle!r}" if needle else ""),
            "",
            "Use a **subcategory** id as `category_id` on an expense; parent ids are not accepted "
            '("Other" under the closest parent when nothing fits).',
            "",
            "| Parent (not usable for expenses) | Subcategories (use these ids) |",
            "|---|---|",
        ]
        sub_count = 0
        for parent in shown:
            subs = _subcategories(parent)
            sub_count += len(subs)
            lines.append(f"| {_cell(_named(parent))} | {_cell(', '.join(_named(s) for s in subs) or '—')} |")
        lines.append("")
        summary = f"{len(shown)} parent(s), {sub_count} subcategories"
        if len(selected) > len(shown):
            summary += f" — showing {len(shown)} of {len(selected)} parents; narrow with `parent`"
        lines.extend([f"_{summary}._", _source_note(from_cache)])
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)


# ---------------------------------------------------------------------------
# Currencies
# ---------------------------------------------------------------------------


@mcp.tool(
    name="splitwise_get_currencies",
    annotations=ToolAnnotations(
        title="Splitwise Supported Currencies",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_get_currencies(params: GetCurrenciesInput) -> str:
    """List the currency codes Splitwise accepts, optionally filtered by a substring.

    Calls `GET /get_currencies` once and caches the answer in-process for 24 h (the list is
    static); `refresh: true` refetches. Codes are mostly ISO 4217 plus a few colloquial ones
    (e.g. BTC); each comes with its display unit (`$`, `S/`, `€`).

    When to Use:
    - To check that a currency code is accepted before using it as `currency_code` on an
      expense or as a user's `default_currency`.
    - To find a code from its symbol (`query: "€"`).

    When NOT to Use:
    - For balances per currency — use `splitwise_get_balances`.
    - For categories — use `splitwise_get_categories`.

    Returns:
    A markdown table of `Code | Unit` (capped at 50 rows; narrow with `query`), or the raw
    `{"currencies": [{currency_code, unit}]}` objects when `response_format` is `json`.

    Examples:
        params = {}
        params = {"query": "pen"}
        params = {"query": "eur", "response_format": "json"}

    Error Handling:
    A 401 means the API key is missing or was regenerated. A failed fetch is never cached;
    a query that matches nothing says so instead of returning an empty table.
    """
    try:
        currencies, from_cache = await _cached_list("/get_currencies", "currencies", refresh=params.refresh)
        needle = _fold(params.query) if params.query else None
        selected = [
            c
            for c in currencies
            if needle is None or needle in _fold(c.get("currency_code")) or needle in _fold(c.get("unit"))
        ]
        if params.response_format is ResponseFormat.JSON:
            return clip_response(to_json({"currencies": selected}))
        if not currencies:
            return "Splitwise returned no currencies (an empty answer is not cached — try again later)."
        if not selected:
            return (
                f"No currency code or unit contains {params.query!r} ({len(currencies)} currencies checked).\n\n"
                + _source_note(from_cache)
            )
        shown = selected[:MAX_DISPLAY_ROWS]
        title = "# Splitwise currencies" + (f" matching {params.query!r}" if params.query else "")
        lines = [title, "", "| Code | Unit |", "|---|---|"]
        lines.extend(f"| {_cell(c.get('currency_code', 'N/A'))} | {_cell(c.get('unit') or '—')} |" for c in shown)
        lines.append("")
        summary = f"{len(selected)} of {len(currencies)} currencies"
        if len(selected) > len(shown):
            summary += f" — showing the first {len(shown)}; narrow with `query`"
        lines.extend([f"_{summary}._", _source_note(from_cache)])
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)


# ---------------------------------------------------------------------------
# Friends
# ---------------------------------------------------------------------------


def _score_friend(query: str, friend: dict[str, Any]) -> _Match:
    first = str(friend.get("first_name") or "")
    last = str(friend.get("last_name") or "")
    email = str(friend.get("email") or "")
    label = " ".join(p for p in (first, last) if p) or email or f"user {friend.get('id')}"
    return _match(
        query,
        [
            ("full name", f"{first} {last}"),
            ("first name", first),
            ("last name", last),
            ("email local-part", email.split("@", 1)[0]),
            ("email", email),
        ],
        label=label,
        item=friend,
    )


def _friend_brief(match: _Match) -> dict[str, Any]:
    friend = match.item
    return {
        "id": friend.get("id"),
        "first_name": friend.get("first_name"),
        "last_name": friend.get("last_name"),
        "email": friend.get("email"),
        "score": round(match.score, 4),
        "matched_on": match.matched_on,
    }


@mcp.tool(
    name="splitwise_resolve_friend",
    annotations=ToolAnnotations(
        title="Splitwise Resolve Friend Name",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_resolve_friend(params: ResolveFriendInput) -> str:
    """Turn a friend's name (or email) into their Splitwise user id, with ranked fuzzy candidates.

    Calls `GET /get_friends` (not cached — friends change) and scores every friend with
    `difflib.SequenceMatcher` on case- and accent-folded text: the best of "first last",
    the first name, the last name, the email local-part and the full email. Returns the top
    `limit` candidates with their scores.

    A friend is **resolved ONLY on an exact match** (1.000 on one of those fields, for
    exactly one friend, and no other friend's name or email starting with the query). A
    wrong id here is a wrong person — the expense, the group membership or the deletion
    lands on someone else — so a close-but-not-exact candidate ("Daniela" vs "Daniel",
    0.923) is reported as **probable** and must be confirmed with the user. No fuzzy
    candidate is offered for a query under 4 characters or whose numbers differ.

    When to Use:
    - Before `splitwise_create_expense`, `splitwise_add_user_to_group`,
      `splitwise_get_friend` or `splitwise_delete_friend`, when the user named a person
      ("split it with Jon").

    When NOT to Use:
    - For balances with each friend — use `splitwise_get_balances` / `splitwise_get_friends`.
    - For groups or categories — use `splitwise_resolve_group` / `splitwise_resolve_category`.
    - Someone who is not your friend yet: they will not be listed — add them with
      `splitwise_create_friend`.

    Returns:
    The resolution line plus a markdown table (rank, `First Last (id N)`, email, score,
    matched on); or, with `response_format` `json`, `{query, resolution, ambiguous_count,
    reason, resolved, probable, candidates: [{id, first_name, last_name, email, score,
    matched_on}]}` where `resolution` is `exact` | `probable` | `ambiguous` | `none`,
    `ambiguous_count` (always present, 0 unless ambiguous) says how many candidates are in
    contention, `resolved` is set only for `exact`, and `probable` only for `probable`.

    Examples:
        params = {"query": "Jon"}
        params = {"query": "maria garcia", "limit": 3, "response_format": "json"}

    Error Handling:
    A 401 means the API key is missing or was regenerated. With no friends at all the tool
    says so. Anything but `exact` means: ask the user, do not pick.
    """
    try:
        resp = await get_client().request("GET", "/get_friends")
        body = resp.json()
        raw = body.get("friends") if isinstance(body, dict) else None
        friends = [f for f in raw if isinstance(f, dict)] if isinstance(raw, list) else []
        query = _fold(params.query)
        ranked = _rank([_score_friend(query, f) for f in friends])
        res = _resolve(ranked, query, fuzzy_status="probable", kind="friend")
        top = ranked[: params.limit]
        if params.response_format is ResponseFormat.JSON:
            payload = _resolve_payload(params.query, res)
            payload["resolved"] = _friend_brief(res.match) if res.match and res.status == "exact" else None
            payload["probable"] = _friend_brief(res.match) if res.match and res.status == "probable" else None
            payload["candidates"] = [_friend_brief(m) for m in top]
            return clip_response(to_json(payload))
        if not friends:
            return f'No friends on this Splitwise account to match "{params.query}" against.'
        lines = [
            f'# Friend matches for "{params.query}"',
            "",
            _resolution_line(res, fmt_person(res.match.item) if res.match else ""),
            "",
            "| # | Friend | Email | Score | Matched on |",
            "|---|---|---|---|---|",
        ]
        for rank, match in enumerate(top, start=1):
            friend = match.item
            lines.append(
                f"| {rank} | {_cell(fmt_person(friend))} | {_cell(friend.get('email') or '—')} | "
                f"{match.score:.3f} | {match.matched_on} |"
            )
        lines.extend(["", f"_{len(top)} of {len(friends)} friend(s) shown, best first._"])
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


def _members_count(group: dict[str, Any]) -> int:
    members = group.get("members")
    return len(members) if isinstance(members, list) else 0


def _group_display(group: dict[str, Any]) -> str:
    return _named(group) + (_GROUP_ZERO_NOTE if group.get("id") == 0 else "")


def _group_brief(match: _Match) -> dict[str, Any]:
    group = match.item
    return {
        "id": group.get("id"),
        "name": group.get("name"),
        "group_type": group.get("group_type"),
        "members_count": _members_count(group),
        "pseudo_group": group.get("id") == 0,
        "score": round(match.score, 4),
    }


@mcp.tool(
    name="splitwise_resolve_group",
    annotations=ToolAnnotations(
        title="Splitwise Resolve Group Name",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_resolve_group(params: ResolveGroupInput) -> str:
    """Turn a group's name into its Splitwise group id, with ranked fuzzy candidates.

    Calls `GET /get_groups` (not cached — groups change) and scores every group name with
    `difflib.SequenceMatcher` on case- and accent-folded text. Returns the top `limit`
    candidates with their scores. Group id 0 is Splitwise's pseudo-group for non-group
    expenses (`group_id: 0` = no group).

    A group is **resolved ONLY on an exact name match** (1.000, for exactly one group, and
    no other group name starting with the query — "Peru trip" next to "Peru trip 2026" is
    ambiguous). A close-but-not-exact name is reported as **probable** and must be
    confirmed with the user; a query whose numbers differ ("peru trip 2025" vs "Peru trip
    2026") or that is under 4 characters gets no fuzzy candidate at all.

    When to Use:
    - Before `splitwise_create_expense`, `splitwise_get_group`, `splitwise_get_expenses`,
      `splitwise_add_user_to_group` or `splitwise_delete_group`, when the user named a
      group ("the Peru trip").

    When NOT to Use:
    - To list every group with balances — use `splitwise_get_groups`.
    - For people or categories — use `splitwise_resolve_friend` / `splitwise_resolve_category`.

    Returns:
    The resolution line plus a markdown table (rank, `Name (id N)`, type, members, score);
    or, with `response_format` `json`, `{query, resolution, ambiguous_count, reason,
    resolved, probable, candidates: [{id, name, group_type, members_count, pseudo_group,
    score}]}` where `resolution` is `exact` | `probable` | `ambiguous` | `none`,
    `ambiguous_count` (always present, 0 unless ambiguous) says how many candidates are in
    contention, `resolved` is set only for `exact`, and `probable` only for `probable`.

    Examples:
        params = {"query": "peru trip"}
        params = {"query": "home", "limit": 3, "response_format": "json"}

    Error Handling:
    A 401 means the API key is missing or was regenerated. With no groups the tool says
    so. Anything but `exact` means: ask the user, do not pick.
    """
    try:
        resp = await get_client().request("GET", "/get_groups")
        body = resp.json()
        raw = body.get("groups") if isinstance(body, dict) else None
        groups = [g for g in raw if isinstance(g, dict)] if isinstance(raw, list) else []
        query = _fold(params.query)
        ranked = _rank(
            [
                _match(query, [("name", group.get("name"))], label=str(group.get("name") or ""), item=group)
                for group in groups
            ]
        )
        res = _resolve(ranked, query, fuzzy_status="probable", kind="group")
        top = ranked[: params.limit]
        if params.response_format is ResponseFormat.JSON:
            payload = _resolve_payload(params.query, res)
            payload["resolved"] = _group_brief(res.match) if res.match and res.status == "exact" else None
            payload["probable"] = _group_brief(res.match) if res.match and res.status == "probable" else None
            payload["candidates"] = [_group_brief(m) for m in top]
            return clip_response(to_json(payload))
        if not groups:
            return f'No groups on this Splitwise account to match "{params.query}" against.'
        lines = [
            f'# Group matches for "{params.query}"',
            "",
            _resolution_line(res, _group_display(res.match.item) if res.match else ""),
            "",
            "| # | Group | Type | Members | Score |",
            "|---|---|---|---|---|",
        ]
        for rank, match in enumerate(top, start=1):
            group = match.item
            lines.append(
                f"| {rank} | {_cell(_group_display(group))} | {_cell(group.get('group_type') or '—')} | "
                f"{_members_count(group)} | {match.score:.3f} |"
            )
        lines.extend(["", f"_{len(top)} of {len(groups)} group(s) shown, best first._"])
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)


# ---------------------------------------------------------------------------
# Categories resolver
# ---------------------------------------------------------------------------


def _subcategory_fields(parent_name: str, sub_name: str) -> list[tuple[str, Any]]:
    """The subcategory's name, each piece of it split on `/` and spaces, and "Parent Sub"."""
    pieces = [piece for piece in _PIECE_SPLIT_RE.split(_fold(sub_name)) if piece]
    fields: list[tuple[str, Any]] = [("name", sub_name)]
    if len(pieces) > 1:
        fields.extend(("name piece", piece) for piece in pieces)
    fields.append(("parent + name", f"{parent_name} {sub_name}"))
    return fields


def _category_brief(match: _Match) -> dict[str, Any]:
    """Compact JSON view: a subcategory (usable, with its parent) or a parent (not usable, with its subs)."""
    item, parent = match.item, match.parent
    brief: dict[str, Any] = {"id": item.get("id"), "name": item.get("name"), "score": round(match.score, 4)}
    if parent is None:
        brief["usable_for_expenses"] = False
        brief["matched_on"] = match.matched_on
        brief["subcategories"] = [{"id": s.get("id"), "name": s.get("name")} for s in _subcategories(item)]
    else:
        brief["usable_for_expenses"] = True
        brief["parent_id"] = parent.get("id")
        brief["parent_name"] = parent.get("name")
        brief["matched_on"] = match.matched_on
    return brief


@mcp.tool(
    name="splitwise_resolve_category",
    annotations=ToolAnnotations(
        title="Splitwise Resolve Category Name",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_resolve_category(params: ResolveCategoryInput) -> str:
    """Turn a category name into the subcategory id an expense needs, with ranked fuzzy candidates.

    Reads the categories from `GET /get_categories` through the same 24 h in-process cache
    as `splitwise_get_categories` (`refresh: true` refetches). Every **subcategory** is
    scored with `difflib.SequenceMatcher` on case- and accent-folded text — the best of its
    name, each piece of the name split on `/` and spaces ("phone" → TV/Phone/Internet), and
    "Parent Subcategory" (so "transportation other" picks the right "Other").

    Subcategories are listed first and are the only ones that can be **resolved**: exactly
    one exact (1.000) match, or — with no exact match — exactly one scoring ≥ 0.85
    (`unique_strong`; a wrong category is cheap to fix with `splitwise_update_expense`).
    Either way no other subcategory may start with the query ("tax" next to Taxi and Taxes
    is ambiguous), a fuzzy match needs a query of 4+ characters, and digits in the query
    must match. The closest parent categories follow, flagged "parent — not usable for
    expenses", with their subcategories so you can pick one; a parent scores the better of
    its own name and its best subcategory (so "groceries" surfaces "Food and drink").

    When to Use:
    - Before `splitwise_create_expense` / `splitwise_update_expense`, when the user named
      a category ("groceries", "taxi").

    When NOT to Use:
    - To browse the whole tree — use `splitwise_get_categories`.
    - For people or groups — use `splitwise_resolve_friend` / `splitwise_resolve_group`.

    Returns:
    The resolution line, a markdown table of the top `limit` subcategories (rank,
    `Name (id N)`, parent, score, matched on), then up to 3 parents with their
    subcategories; or, with `response_format` `json`, `{query, resolution,
    ambiguous_count, reason, resolved, subcategories: [...], parents: [...]}` where
    `resolution` is `exact` | `unique_strong` | `ambiguous` | `none`, `ambiguous_count`
    (always present, 0 unless ambiguous) says how many candidates are in contention, and
    parents carry `usable_for_expenses: false`.

    Examples:
        params = {"query": "groceries"}
        params = {"query": "transportation other", "response_format": "json"}

    Error Handling:
    A 401 means the API key is missing or was regenerated. A failed fetch is never
    cached. Several subcategories share the name "Other" — "other" alone is reported as
    ambiguous; qualify it with the parent.
    """
    try:
        categories, from_cache = await _cached_list("/get_categories", "categories", refresh=params.refresh)
        query = _fold(params.query)
        sub_matches: list[_Match] = []
        parent_matches: list[_Match] = []
        for parent in categories:
            parent_name = str(parent.get("name") or "")
            own = _match(query, [("name", parent_name)], label=parent_name, item=parent)
            best_score, best_on = own.score, own.matched_on
            for sub in _subcategories(parent):
                sub_name = str(sub.get("name") or "")
                match = _match(
                    query, _subcategory_fields(parent_name, sub_name), label=sub_name, item=sub, parent=parent
                )
                sub_matches.append(match)
                if match.score > best_score:
                    best_score, best_on = match.score, f"subcategory {sub_name}"
            parent_matches.append(replace(own, score=best_score, matched_on=best_on))
        ranked_subs = _rank(sub_matches)
        res = _resolve(ranked_subs, query, fuzzy_status="unique_strong", kind="category")
        resolved = res.match if res.status in ("exact", "unique_strong") else None
        top_subs = ranked_subs[: params.limit]
        top_parents = _rank(parent_matches)[: min(params.limit, PARENT_ROWS)]
        if params.response_format is ResponseFormat.JSON:
            payload = _resolve_payload(params.query, res)
            payload["resolved"] = _category_brief(resolved) if resolved else None
            payload["subcategories"] = [_category_brief(m) for m in top_subs]
            payload["parents"] = [_category_brief(m) for m in top_parents]
            return clip_response(to_json(payload))
        if not categories:
            return f'Splitwise returned no categories to match "{params.query}" against.'
        display = f"{_named(resolved.item)} under {(resolved.parent or {}).get('name')}" if resolved else ""
        lines = [
            f'# Category matches for "{params.query}"',
            "",
            _resolution_line(res, display),
            "",
            "## Subcategories (use one of these ids as `category_id`)",
            "",
            "| # | Subcategory | Parent | Score | Matched on |",
            "|---|---|---|---|---|",
        ]
        for rank, match in enumerate(top_subs, start=1):
            parent_name = (match.parent or {}).get("name") or "—"
            lines.append(
                f"| {rank} | {_cell(_named(match.item))} | {_cell(parent_name)} | "
                f"{match.score:.3f} | {match.matched_on} |"
            )
        lines.extend(
            [
                "",
                "## Closest parent categories (parent — not usable for expenses)",
                "",
                "| Parent | Score | Matched on | Its subcategories |",
                "|---|---|---|---|",
            ]
        )
        for match in top_parents:
            subs = ", ".join(_named(s) for s in _subcategories(match.item)) or "—"
            lines.append(
                f"| {_cell(_named(match.item))} — parent, not usable for expenses | {match.score:.3f} | "
                f"{_cell(match.matched_on)} | {_cell(subs)} |"
            )
        lines.extend(["", _source_note(from_cache)])
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)
