"""Turn a bot's page description (JSON blocks) into what the templates render.

The page format is the contract in docs/design.md §2a. Anything a bot sends
that does not fit it is dropped with a note, never rendered as markup: the
templates escape every string.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any

from . import actions
from .actions import Action, ActionError

log = logging.getLogger(__name__)

PAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")
MAX_BLOCKS = 50
MAX_ROWS = 500


def _text(value: Any, limit: int = 2000) -> str:
    return "" if value is None else str(value)[:limit]


def _page_link(value: Any) -> str | None:
    page = _text(value, 80)
    return page if PAGE_ID_RE.match(page) else None


class PageBuilder:
    def __init__(self, bot: str, signing_key: bytes):
        self.bot = bot
        self.key = signing_key

    def _signed(self, item: dict[str, Any], default_label: str = "", context: str = "") -> dict[str, Any] | None:
        """Sign one action. `context` names what it acts on (a switch's label),
        so the confirm box and the audit log say "Fast Delete: Turn on", while
        the button itself just says "Turn on"."""
        try:
            action: Action = actions.action_from_block(self.bot, item, default_label)
        except (ActionError, AttributeError) as exc:
            log.warning("Bot %s offered an unusable action: %s", self.bot, exc)
            return None
        button = action.label
        if context:
            action = dataclasses.replace(action, label=f"{context}: {action.label}")
        return {
            "token": actions.sign(self.key, action),
            "label": button,
            "danger": action.danger,
            "confirm": action.confirm,
        }

    def build(self, page: dict[str, Any]) -> dict[str, Any]:
        blocks = []
        for raw in (page.get("blocks") or [])[:MAX_BLOCKS]:
            if not isinstance(raw, dict):
                continue
            block = self._block(raw)
            if block is not None:
                blocks.append(block)
        return {"title": _text(page.get("title"), 200), "blocks": blocks}

    def _block(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        kind = raw.get("type")
        title = _text(raw.get("title"), 200)
        if kind == "text":
            return {"type": "text", "title": title, "text": _text(raw.get("text"), 5000)}
        if kind == "cards":
            items = [
                {
                    "label": _text(item.get("label"), 100),
                    "value": _text(item.get("value"), 100),
                    "note": _text(item.get("note"), 300),
                    "tone": item.get("tone") if item.get("tone") in {"good", "warn", "bad"} else None,
                    "page": _page_link(item.get("page")),
                }
                for item in raw.get("items") or []
                if isinstance(item, dict)
            ]
            return {"type": "cards", "title": title, "items": items}
        if kind == "table":
            columns = [_text(col, 100) for col in raw.get("columns") or []]
            rows = []
            for row in (raw.get("rows") or [])[:MAX_ROWS]:
                if isinstance(row, dict):
                    cells, link = row.get("cells") or [], _page_link(row.get("page"))
                else:
                    cells, link = row, None
                if isinstance(cells, list):
                    rows.append({"cells": [_text(cell, 500) for cell in cells], "page": link})
            pager = raw.get("pager") if isinstance(raw.get("pager"), dict) else {}
            return {
                "type": "table",
                "title": title,
                "columns": columns,
                "rows": rows,
                "empty": _text(raw.get("empty") or "Nothing here.", 200),
                "prev": _page_link(pager.get("prev")),
                "next": _page_link(pager.get("next")),
            }
        if kind == "switches":
            items = []
            for item in raw.get("items") or []:
                if not isinstance(item, dict):
                    continue
                on = bool(item.get("on"))
                target = item.get("turn_off" if on else "turn_on")
                items.append(
                    {
                        "label": _text(item.get("label"), 100),
                        "on": on,
                        "note": _text(item.get("note"), 300),
                        "action": self._signed(
                            target, "Turn off" if on else "Turn on", context=_text(item.get("label"), 100)
                        )
                        if isinstance(target, dict)
                        else None,
                    }
                )
            return {"type": "switches", "title": title, "items": items}
        if kind == "actions":
            items = [
                signed
                for item in raw.get("items") or []
                if isinstance(item, dict) and (signed := self._signed(item)) is not None
            ]
            return {"type": "actions", "title": title, "items": items}
        if kind == "links":
            items = [
                {"label": _text(item.get("label"), 100), "page": page, "note": _text(item.get("note"), 300)}
                for item in raw.get("items") or []
                if isinstance(item, dict) and (page := _page_link(item.get("page")))
            ]
            return {"type": "links", "title": title, "items": items}
        if kind == "form":
            submit = raw.get("submit")
            if not isinstance(submit, dict):
                return None
            fields = []
            for spec in raw.get("fields") or []:
                if not isinstance(spec, dict):
                    continue
                options = [
                    {"value": _text(opt.get("value"), 100), "label": _text(opt.get("label") or opt.get("value"), 100)}
                    for opt in spec.get("options") or []
                    if isinstance(opt, dict)
                ]
                fields.append(
                    {
                        "name": _text(spec.get("name"), 32),
                        "label": _text(spec.get("label"), 100),
                        "kind": _text(spec.get("kind") or "text", 10),
                        "value": _text(spec.get("value"), 500),
                        "options": options,
                        "help": _text(spec.get("help"), 300),
                    }
                )
            signed = self._signed(
                {**submit, "fields": [{"name": f["name"], "kind": f["kind"]} for f in fields]}, "Save"
            )
            if signed is None:
                return None
            return {"type": "form", "title": title, "fields": fields, "submit": signed}
        log.info("Bot %s sent an unknown block type %r", self.bot, kind)
        return None


def job_view(job: dict[str, Any]) -> dict[str, Any]:
    state = job.get("state") if job.get("state") in {"queued", "running", "done", "failed"} else "running"
    progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
    done, total = progress.get("done"), progress.get("total")
    percent = None
    if isinstance(done, int) and isinstance(total, int) and total > 0:
        percent = max(0, min(100, round(done * 100 / total)))
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    error = job.get("error") if isinstance(job.get("error"), dict) else {}
    return {
        "id": _text(job.get("id"), 64),
        "kind": _text(job.get("kind"), 64),
        "state": state,
        "finished": state in {"done", "failed"},
        "done": done,
        "total": total,
        "percent": percent,
        "current": _text(progress.get("current"), 200),
        # A result only means something once the job has finished.
        "message": _text(result.get("message") or error.get("message"), 2000) if state in {"done", "failed"} else "",
    }
