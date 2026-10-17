"""SQL text utilities shared by producer connectors that patch SQL files."""

import re

# A quoted Jinja string, so a closing delimiter inside it ("}}", "%}") does not
# end the span. Unquoted characters exclude quotes, which keeps the pattern
# unambiguous and free of catastrophic backtracking.
_JINJA_STRING = r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*\""""
_JINJA_BODY = rf"""(?:{_JINJA_STRING}|[^'"])*?"""

# Capture group makes re.split keep the spans, so callers can tell them apart.
# Comments end at the first "#}" regardless of quotes, as in Jinja itself.
_JINJA_SPAN = re.compile(
    rf"(\{{\{{{_JINJA_BODY}\}}\}}|\{{%{_JINJA_BODY}%\}}|\{{#.*?#\}})", re.DOTALL
)


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def rename_column_in_sql(sql: str, old_name: str, new_name: str) -> str:
    """Replace references to old_name with new_name in a SQL string.

    Handles common patterns:
    - bare column references:       old_name → new_name
    - aliased selections:           old_name as alias → new_name as alias
    - quoted identifiers:           `old_name`, "old_name", [old_name] → same quoting, new_name
    - qualified refs (alias.old_name): left completely unchanged

    Qualified references are deliberately not touched: at the SQL text level
    we cannot know which table alias maps to the source model, so renaming
    them risks both wrong renames (s.type where s is a different table) and
    broken SQL. A source model whose only references are qualified is instead
    reported as unverified so the PR flags it for manual review.

    Jinja spans ({{ }}, {% %}, {# #}) are never modified. Callers that cannot
    accept a partial rename should check mentions_in_jinja first.

    String literals and SQL comments (-- and /* */) are never modified, and
    quoted identifiers (`x`, "x", [x]) are renamed only on an exact match.
    Bare references match case-insensitively and keep their case style.

    Uses word-boundary matching to avoid partial replacements
    (e.g. 'recorded_date' should not match 'date').

    Raises ValueError if new_name is not a plain SQL identifier.
    """
    if not _IDENTIFIER.fullmatch(new_name):
        raise ValueError(f"new column name is not a valid SQL identifier: {new_name!r}")
    spans: list[str] = []

    def stash(m: re.Match) -> str:
        spans.append(m.group(0))
        return f"\x00{len(spans) - 1}\x00"

    # Jinja spans become quote-free placeholders so a literal or comment that
    # contains one ('{{ var("x") }}') stays a single token for the rename.
    masked = _JINJA_SPAN.sub(stash, sql)
    renamed = _rename_in_plain_sql(masked, old_name, new_name)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], renamed)


def mentions_in_jinja(sql: str, name: str) -> bool:
    """Return True if name appears as a whole word inside a Jinja span.

    Such a reference (a macro argument, a {% set %} list, a comment) cannot be
    renamed safely at the text level, so the model needs manual review.
    """
    pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
    return any(pattern.search(span) for span in _JINJA_SPAN.findall(sql))


def _rename_in_plain_sql(sql: str, old_name: str, new_name: str) -> str:
    # Qualified references (alias.col, table.col) are left completely unchanged.
    # We cannot determine from SQL text alone which table alias maps to the
    # source model being renamed, so touching qualified refs risks both wrong
    # renames (s.type where s is a different table) and broken SQL (AS alias
    # in WHERE clauses is invalid). Bare references are unambiguous within a
    # model that is already scoped to the affected entity set.
    old = re.escape(old_name)
    pattern = re.compile(
        rf"""(?P<skip>'(?:''|[^'])*'|--[^\n]*|/\*.*?\*/)"""
        rf"""|(?P<quoted>`[^`]*`|"(?:""|[^"])*"|(?<![\w\])])\[[^\]\[,()'"+\-*/<>=!|&%.:]*\])"""
        rf"""|(?<![\w"`\]]\.)\b(?P<bare>{old})\b""",
        re.DOTALL | re.IGNORECASE,
    )

    def replace(m: re.Match) -> str:
        if m.group("skip") is not None:
            return m.group(0)
        if m.group("quoted") is not None:
            # Quoted identifiers are case-sensitive: exact match only.
            text = m.group(0)
            if m.start() > 0 and m.string[m.start() - 1] == ".":
                return text  # qualified reference: leave unchanged
            open_, inner, close = text[0], text[1:-1], text[-1]
            if inner != old_name:
                return text
            return f"{open_}{new_name}{close}"
        return _match_case(m.group("bare"), old_name, new_name)

    return pattern.sub(replace, sql)


def _match_case(found: str, old_name: str, new_name: str) -> str:
    """Write new_name in the case style the bare reference used.

    Unquoted identifiers are case-insensitive, so ORDER_DATE and order_date
    both refer to the column. An exact match gets new_name as given; an
    all-upper or all-lower variant gets new_name in the same style.
    """
    if found == old_name:
        return new_name
    if found.isupper():
        return new_name.upper()
    if found.islower():
        return new_name.lower()
    return new_name
