"""Splitwise expense comments: list, create and delete (inventory E1–E3).

Comments hang off one expense (`relation_type: ExpenseComment`). Splitwise writes
`System` comments itself (e.g. "cost changed from … to …") and people write `User`
comments. Creating and deleting are writes, refused by the client unless
`SPLITWISE_ALLOW_WRITES=1`.
"""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from splitwise_mcp.client import get_client
from splitwise_mcp.errors import handle_api_error
from splitwise_mcp.formatters import ResponseFormat, clip_response, fmt_person, iso_to_human, to_json
from splitwise_mcp.server import mcp

MAX_DISPLAY_ROWS = 50


def _cell(text: Any) -> str:
    """Make a value safe inside a markdown table cell (escape pipes, keep line breaks as `<br>`)."""
    flat = str(text if text is not None else "").replace("\r\n", "\n").strip()
    return flat.replace("|", "\\|").replace("\n", "<br>")


def _author(comment: dict[str, Any]) -> str:
    user = comment.get("user")
    return fmt_person(user) if user else "—"


def _comment_row(comment: dict[str, Any]) -> str:
    content = _cell(comment.get("content"))
    if comment.get("deleted_at"):
        content = f"_[deleted {iso_to_human(comment['deleted_at'])}]_ {content}"
    return (
        f"| {comment.get('id', 'N/A')} | {_cell(_author(comment))} | {comment.get('comment_type') or 'N/A'} "
        f"| {iso_to_human(comment.get('created_at'))} | {content} |"
    )


def _comment_block(comment: dict[str, Any]) -> list[str]:
    """Render one comment object (as Splitwise returned it) as a bullet list."""
    lines = [
        f"- **comment id**: {comment.get('id', 'N/A')}",
        f"- **expense id**: {comment.get('relation_id', 'N/A')}"
        + (f" ({comment['relation_type']})" if comment.get("relation_type") else ""),
        f"- **author**: {_author(comment)}",
        f"- **type**: {comment.get('comment_type') or 'N/A'}",
        f"- **created**: {iso_to_human(comment.get('created_at'))}",
    ]
    if comment.get("deleted_at"):
        lines.append(f"- **deleted**: {iso_to_human(comment.get('deleted_at'))}")
    lines.append(f"- **content**: {comment.get('content') or ''}")
    return lines


class GetCommentsInput(BaseModel):
    """Input for `splitwise_get_comments`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    expense_id: int = Field(..., ge=1, description="Id of the expense whose comments to list.")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' (a readable table, default) or 'json' (the raw comment objects).",
    )


class CreateCommentInput(BaseModel):
    """Input for `splitwise_create_comment`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    expense_id: int = Field(..., ge=1, description="Id of the expense to comment on.")
    content: str = Field(..., min_length=1, description="The comment text (plain text; must not be blank).")


class DeleteCommentInput(BaseModel):
    """Input for `splitwise_delete_comment`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    comment_id: int = Field(..., ge=1, description="Id of the comment to delete (from `splitwise_get_comments`).")


@mcp.tool(
    name="splitwise_get_comments",
    annotations=ToolAnnotations(
        title="Get Splitwise Expense Comments",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_get_comments(params: GetCommentsInput) -> str:
    """List the comments on one Splitwise expense.

    Calls `GET /get_comments?expense_id=…` and renders one row per comment: its id, the
    author as `First Last (id N)`, the type (`System` = written by Splitwise, e.g. an
    edit log; `User` = written by a person), when it was created, and the text.

    When to Use:
    - To read the discussion or the edit history on an expense.
    - To find a comment's id before deleting it with `splitwise_delete_comment`.

    When NOT to Use:
    - To read the expense itself (cost, shares) — use `splitwise_get_expense`.
    - To see recent activity across all expenses and groups — use `splitwise_get_notifications`.
    - To find an expense id from a description — use `splitwise_get_expenses`.

    Returns:
    Markdown: a table `| Id | Author | Type | Created | Content |` (at most 50 rows; the
    rest are counted). JSON: `{"expense_id", "count", "comments": [raw comment objects]}`.

    Examples:
    - `params = {"expense_id": 51023}`
    - `params = {"expense_id": 51023, "response_format": "json"}`

    Error Handling:
    404 means the expense id does not exist or was deleted; 403 means you are not part of
    that expense. Errors come back as an `Error ...` string, never raised.
    """
    try:
        client = get_client()
        resp = await client.request("GET", "/get_comments", params={"expense_id": params.expense_id})
        comments: list[dict[str, Any]] = resp.json().get("comments") or []

        if params.response_format is ResponseFormat.JSON:
            return clip_response(
                to_json({"expense_id": params.expense_id, "count": len(comments), "comments": comments})
            )

        lines = [f"# Comments on expense {params.expense_id}", ""]
        if not comments:
            lines.append("_No comments on this expense._")
            return clip_response("\n".join(lines))
        shown = comments[:MAX_DISPLAY_ROWS]
        lines.append(f"{len(comments)} comment(s). `System` = written by Splitwise, `User` = written by a person.")
        lines.extend(["", "| Id | Author | Type | Created | Content |", "|---|---|---|---|---|"])
        lines.extend(_comment_row(comment) for comment in shown)
        if len(comments) > len(shown):
            lines.extend(
                [
                    "",
                    f"_Showing the first {len(shown)} of {len(comments)} comments — "
                    "use `response_format='json'` for all of them._",
                ]
            )
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="splitwise_create_comment",
    annotations=ToolAnnotations(
        title="Create Splitwise Expense Comment",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def splitwise_create_comment(params: CreateCommentInput) -> str:
    """Add a comment to a Splitwise expense.

    Write — refused unless SPLITWISE_ALLOW_WRITES=1. Calls `POST /create_comment` with
    `expense_id` and `content`. Everyone on the expense can see the comment. Not
    idempotent: calling it twice posts the comment twice.

    When to Use:
    - To leave a note on an expense ("paid in cash", "includes the tip").

    When NOT to Use:
    - To change the expense's own description or notes — use `splitwise_update_expense`.
    - To read existing comments — use `splitwise_get_comments`.

    Returns:
    A confirmation echoing the comment Splitwise returned: its id, the expense id, the
    author, the type, the creation time and the text.

    Examples:
    - `params = {"expense_id": 51023, "content": "Paid in cash, settled at dinner."}`

    Error Handling:
    Refused with `Error: … writes are disabled` unless `SPLITWISE_ALLOW_WRITES=1`. A blank
    `content` is rejected before any call. 404 = the expense does not exist or was
    deleted; 403 = you are not part of it. On a timeout or 5xx the outcome is UNKNOWN —
    read the comments back with `splitwise_get_comments` before retrying, or the comment
    may be posted twice. Undo with `splitwise_delete_comment`.
    """
    try:
        client = get_client()
        resp = await client.request(
            "POST",
            "/create_comment",
            data={"expense_id": params.expense_id, "content": params.content},
        )
        comment: dict[str, Any] = resp.json().get("comment") or {}
        if not comment:
            return (
                "Splitwise accepted the request but returned no `comment` object — read it back with "
                f"`splitwise_get_comments` (expense_id={params.expense_id}) to confirm it was posted."
            )
        return clip_response("\n".join(["# Comment created", "", *_comment_block(comment)]))
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="splitwise_delete_comment",
    annotations=ToolAnnotations(
        title="Delete Splitwise Expense Comment",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_delete_comment(params: DeleteCommentInput) -> str:
    """Delete a comment from a Splitwise expense (destructive; comments have no undelete).

    Write — refused unless SPLITWISE_ALLOW_WRITES=1. Calls `POST /delete_comment/{id}`.
    Splitwise answers with the deleted comment, which this tool renders so you can see
    exactly what was removed. There is NO undelete for comments in the Splitwise API — if
    needed, re-post the text with `splitwise_create_comment` (it gets a new id and a new
    timestamp).

    When to Use:
    - To remove a comment you posted by mistake (get its id from `splitwise_get_comments`).

    When NOT to Use:
    - To delete the expense itself — use `splitwise_delete_expense`.
    - When you only know the comment's text — list them with `splitwise_get_comments` first.

    Returns:
    A confirmation echoing the deleted comment as Splitwise returned it: id, expense id,
    author, type, created/deleted times and the text.

    Examples:
    - `params = {"comment_id": 79800950}`

    Error Handling:
    Refused with `Error: … writes are disabled` unless `SPLITWISE_ALLOW_WRITES=1`. 404 =
    the comment id does not exist or is already gone; 403 = you are not allowed to delete
    it (you are not part of that expense). Errors come back as an `Error ...` string, never raised.
    """
    try:
        client = get_client()
        resp = await client.request("POST", f"/delete_comment/{params.comment_id}")
        comment: dict[str, Any] = resp.json().get("comment") or {}
        if not comment:
            return (
                f"Splitwise accepted the delete of comment {params.comment_id} but returned no `comment` object "
                "— check with `splitwise_get_comments` that it is gone."
            )
        return clip_response("\n".join(["# Comment deleted", "", *_comment_block(comment)]))
    except Exception as exc:
        return handle_api_error(exc)
