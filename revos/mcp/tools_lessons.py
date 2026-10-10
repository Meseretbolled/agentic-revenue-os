import re
from typing import Any

from psycopg.rows import dict_row

from revos.mcp.registry import ToolContext, ToolSpec, fence
from revos.mcp.scope import RunScope, ToolBlocked

MAX_RESULTS = 10


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", text.casefold()))


def _present(ctx: ToolContext, scope: RunScope, row: dict[str, Any]) -> dict[str, Any] | None:
    evidence = fence(ctx, scope, "transcript", f"lesson-{row['id']}", row["evidence"])
    if evidence is None:                       # the stored quote now looks like an attack: hold it
        return None
    return {"id": row["id"], "text": row["text"], "evidence": evidence, "tags": row["tags"],
            "confidence": float(row["confidence"]), "uses": row["uses"], "wins": row["wins"],
            "losses": row["losses"], "source": row["source"],
            "scope": "global" if row["segment_id"] is None else "segment"}


_COLUMNS = "id, segment_id, text, evidence, tags, confidence, uses, wins, losses, source"


def search(ctx: ToolContext, scope: RunScope, args: dict[str, Any]) -> dict[str, Any]:
    """Lessons for this segment and global ones. Ranked by how well tags and keywords match, then
    by confidence, then by how they have fared. Retired lessons never appear."""
    query = str(args.get("query", ""))
    tags = {str(t).casefold() for t in args.get("tags", [])}
    limit = min(int(args.get("limit", 5)), MAX_RESULTS)
    rows = ctx.conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLUMNS} FROM lessons "
        "WHERE retired_at IS NULL AND (segment_id IS NULL OR segment_id = %s)",
        (scope.segment_id,)).fetchall()
    words = _words(query)

    def relevance(row: dict[str, Any]) -> int:
        row_tags = {t.casefold() for t in row["tags"]}
        return 2 * len(tags & row_tags) + len(words & _words(row["text"] + " " + " ".join(row_tags)))

    def fared(row: dict[str, Any]) -> float:
        return (row["wins"] - row["losses"]) / (row["uses"] + 1)

    scored = [(relevance(r), r) for r in rows]
    if words or tags:                          # asked for something: only what matches it
        scored = [(rel, r) for rel, r in scored if rel > 0]
    scored.sort(key=lambda p: (-p[0], -float(p[1]["confidence"]), -fared(p[1]), p[1]["id"]))
    shown = [x for _, r in scored if (x := _present(ctx, scope, r))][:limit]
    return {"lessons": shown, "segment": scope.segment_id}


def get(ctx: ToolContext, scope: RunScope, args: dict[str, Any]) -> dict[str, Any]:
    try:
        lesson_id = int(args["lesson_id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ToolBlocked("lesson_id is missing or not a valid id") from exc
    row = ctx.conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLUMNS} FROM lessons WHERE id = %s "
        "AND retired_at IS NULL AND (segment_id IS NULL OR segment_id = %s)",
        (lesson_id, scope.segment_id)).fetchone()
    shown = _present(ctx, scope, row) if row else None
    if shown is None:
        raise ToolBlocked("lesson_id is not available in this run")     # foreign, retired or unknown
    return shown


SPECS = [
    ToolSpec("lessons", "search", "Find lessons for this segment (and global ones), best first.",
             {"query": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
              "limit": {"type": "integer"}}, ("query",), search),
    ToolSpec("lessons", "get", "Read one lesson by id.",
             {"lesson_id": {"type": "integer"}}, ("lesson_id",), get),
]
