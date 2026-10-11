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


def rename_column_in_sql(sql: str, old_name: str, new_name: str) -> str:
    """Replace references to old_name with new_name in a SQL string.

    Handles common patterns:
    - bare column references:       old_name → new_name
    - aliased selections:           old_name as alias → new_name as alias
    - quoted identifiers:           `old_name`, "old_name" → `new_name`, "new_name"
    - qualified refs (alias.old_name): left completely unchanged

    Qualified references are deliberately not touched: at the SQL text level
    we cannot know which table alias maps to the source model, so renaming
    them risks both wrong renames (s.type where s is a different table) and
    broken SQL. A source model whose only references are qualified is instead
    reported as unverified so the PR flags it for manual review.

    Jinja spans ({{ }}, {% %}, {# #}) are never modified. Callers that cannot
    accept a partial rename should check mentions_in_jinja first.

    Uses word-boundary matching to avoid partial replacements
    (e.g. 'recorded_date' should not match 'date').
    """
    # re.split with a capture group alternates plain SQL (even indexes) and
    # Jinja spans (odd indexes); only the plain segments are rewritten.
    parts = _JINJA_SPAN.split(sql)
    parts[::2] = [_rename_in_plain_sql(p, old_name, new_name) for p in parts[::2]]
    return "".join(parts)


def mentions_in_jinja(sql: str, name: str) -> bool:
    """Return True if name appears as a whole word inside a Jinja span.

    Such a reference (a macro argument, a {% set %} list, a comment) cannot be
    renamed safely at the text level, so the model needs manual review.
    """
    pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
    return any(pattern.search(span) for span in _JINJA_SPAN.findall(sql))


def _rename_in_plain_sql(sql: str, old_name: str, new_name: str) -> str:
    # Backtick-quoted (BigQuery style) — direct rename
    sql = re.sub(rf"`{re.escape(old_name)}`", f"`{new_name}`", sql)

    # Double-quoted (standard SQL) — direct rename
    sql = re.sub(rf'"{re.escape(old_name)}"', f'"{new_name}"', sql)

    # Qualified references (alias.col, table.col) — leave completely unchanged.
    # We cannot determine from SQL text alone which table alias maps to the
    # source model being renamed, so touching qualified refs risks both wrong
    # renames (s.type where s is a different table) and broken SQL (AS alias
    # in WHERE clauses is invalid). Bare references are unambiguous within a
    # model that is already scoped to the affected entity set.

    # Bare identifier: not preceded by a qualifier (word + dot)
    # word-boundary on both sides, case-insensitive
    return re.sub(
        rf"(?<!\w\.)\b{re.escape(old_name)}\b",
        new_name,
        sql,
        flags=re.IGNORECASE,
    )
