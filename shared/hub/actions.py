"""Signed action descriptors.

A bot's page tells the hub which buttons it offers (method, path, body,
confirm text). The hub signs each one when it renders the page, and only a
correctly signed action is ever sent to a bot. So a request to the hub can
only replay what a bot offered on a page; it cannot make the hub call an
arbitrary bot address.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .bots import API_PREFIX

METHODS = {"POST", "PUT", "PATCH", "DELETE"}
FIELD_KINDS = {"text", "number", "select"}
RESERVED_FIELDS = {"action", "csrf", "confirmed"}  # the hub's own form inputs


@dataclass(frozen=True)
class Action:
    bot: str
    method: str
    path: str
    body: dict[str, Any]
    label: str
    confirm: str | None = None
    danger: bool = False
    # Form inputs the owner fills in: [{"name": ..., "kind": "text|number|select"}].
    fields: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot": self.bot,
            "method": self.method,
            "path": self.path,
            "body": self.body,
            "label": self.label,
            "confirm": self.confirm,
            "danger": self.danger,
            "fields": self.fields,
        }


class ActionError(ValueError):
    pass


def action_from_block(bot: str, item: dict[str, Any], default_label: str = "") -> Action:
    """Build an Action from a bot's description, refusing anything off-contract."""
    method = str(item.get("method", "")).upper()
    path = str(item.get("path", ""))
    body = item.get("body") or {}
    if method not in METHODS:
        raise ActionError(f"unsupported method {method!r}")
    if not path.startswith(API_PREFIX) or ".." in path:
        raise ActionError(f"path {path!r} is outside the bot API")
    if not isinstance(body, dict):
        raise ActionError("body must be an object")
    confirm = item.get("confirm")
    fields = []
    for spec in item.get("fields") or []:
        name = str(spec.get("name", ""))
        kind = str(spec.get("kind", "text"))
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) or name in RESERVED_FIELDS or kind not in FIELD_KINDS:
            raise ActionError(f"bad form field {name!r}")
        fields.append({"name": name, "kind": kind})
    return Action(
        bot=bot,
        method=method,
        path=path,
        body=body,
        label=str(item.get("label") or default_label),
        confirm=str(confirm) if confirm else None,
        danger=bool(item.get("danger")),
        fields=fields,
    )


def with_form_values(action: Action, form: dict[str, str]) -> dict[str, Any]:
    """The body to send: the signed body plus the owner's declared inputs."""
    body = dict(action.body)
    for spec in action.fields:
        raw = (form.get(spec["name"]) or "").strip()
        if spec["kind"] == "number":
            try:
                body[spec["name"]] = int(raw)
            except ValueError:
                raise ActionError(f"{spec['name']} must be a whole number") from None
        else:
            body[spec["name"]] = raw[:500]
    return body


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(key: bytes, action: Action) -> str:
    payload = json.dumps(action.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    mac = hmac.new(key, payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(mac)}"


def verify(key: bytes, token: str) -> Action:
    try:
        payload_part, mac_part = token.split(".", 1)
        payload = _unb64(payload_part)
        mac = _unb64(mac_part)
    except (ValueError, TypeError) as exc:
        raise ActionError("malformed action") from exc
    expected = hmac.new(key, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise ActionError("bad signature")
    data = json.loads(payload)
    return Action(**data)
