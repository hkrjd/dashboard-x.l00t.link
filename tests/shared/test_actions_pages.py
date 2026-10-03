import pytest

from hub import actions
from hub.actions import ActionError
from hub.pages import PageBuilder, job_view

KEY = b"k" * 32


def make(item, bot="dealops"):
    return actions.action_from_block(bot, item)


def test_sign_and_verify_round_trip():
    action = make({"method": "put", "path": "/api/v1/x", "body": {"on": True}, "label": "Turn on"})
    again = actions.verify(KEY, actions.sign(KEY, action))
    assert again == action and again.method == "PUT"


@pytest.mark.parametrize(
    "token_change",
    [
        lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"),
        lambda t: "x" + t,
        lambda t: "nonsense",
    ],
)
def test_tampered_action_is_refused(token_change):
    token = actions.sign(KEY, make({"method": "POST", "path": "/api/v1/x", "label": "Go"}))
    with pytest.raises(ActionError):
        actions.verify(KEY, token_change(token))


def test_other_key_is_refused():
    token = actions.sign(KEY, make({"method": "POST", "path": "/api/v1/x", "label": "Go"}))
    with pytest.raises(ActionError):
        actions.verify(b"z" * 32, token)


@pytest.mark.parametrize(
    "item",
    [
        {"method": "GET", "path": "/api/v1/x"},
        {"method": "POST", "path": "/admin"},
        {"method": "POST", "path": "/api/v1/../admin"},
        {"method": "POST", "path": "/api/v1/x", "body": [1]},
        {"method": "POST", "path": "/api/v1/x", "fields": [{"name": "csrf"}]},
        {"method": "POST", "path": "/api/v1/x", "fields": [{"name": "Bad Name"}]},
        {"method": "POST", "path": "/api/v1/x", "fields": [{"name": "n", "kind": "file"}]},
    ],
)
def test_off_contract_actions_are_refused(item):
    with pytest.raises(ActionError):
        make(item)


def test_form_values_only_declared_fields_and_numbers_checked():
    action = make(
        {
            "method": "PUT",
            "path": "/api/v1/window",
            "body": {"scope": "all"},
            "fields": [{"name": "minutes", "kind": "number"}, {"name": "label", "kind": "text"}],
        }
    )
    body = actions.with_form_values(action, {"minutes": " 30 ", "label": "x", "scope": "one", "extra": "y"})
    assert body == {"scope": "all", "minutes": 30, "label": "x"}
    with pytest.raises(ActionError, match="whole number"):
        actions.with_form_values(action, {"minutes": "ten", "label": "x"})


def test_page_builder_signs_switches_and_drops_bad_parts():
    page = {
        "title": "Features",
        "blocks": [
            {
                "type": "switches",
                "items": [
                    {
                        "label": "Fast Delete",
                        "on": False,
                        "turn_on": {"method": "PUT", "path": "/api/v1/f", "body": {"on": True}, "confirm": "Sure?"},
                        "turn_off": {"method": "PUT", "path": "/api/v1/f", "body": {"on": False}},
                    },
                    {"label": "Broken", "on": True, "turn_off": {"method": "GET", "path": "/x"}},
                ],
            },
            {"type": "script", "text": "nope"},
            "not a block",
            {"type": "links", "items": [{"label": "ok", "page": "channel:1"}, {"label": "bad", "page": "../x"}]},
        ],
    }
    built = PageBuilder("dealops", KEY).build(page)
    assert [b["type"] for b in built["blocks"]] == ["switches", "links"]
    on, broken = built["blocks"][0]["items"]
    action = actions.verify(KEY, on["action"]["token"])
    assert action.body == {"on": True} and action.confirm == "Sure?" and on["action"]["label"] == "Turn on"
    assert action.label == "Fast Delete: Turn on"
    assert broken["action"] is None
    assert [item["page"] for item in built["blocks"][1]["items"]] == ["channel:1"]


def test_job_view_progress():
    view = job_view({"id": "j1", "kind": "cleanup", "state": "running", "progress": {"done": 3, "total": 4}})
    assert view["percent"] == 75 and not view["finished"]
    early = job_view({"id": "j1", "state": "running", "result": {"message": "not yet"}})
    assert early["message"] == ""
    assert job_view({"id": "j1", "state": "weird"})["state"] == "running"
    done = job_view({"id": "j1", "state": "done", "result": {"message": "12 deleted"}})
    assert done["finished"] and done["message"] == "12 deleted"


def test_a_form_names_what_it_saves():
    """The audit log and the confirm box said only "Save" (seen live, 2026-10-03)."""
    page = {
        "blocks": [
            {
                "type": "form",
                "title": "Duplicate time limit",
                "fields": [{"name": "minutes", "label": "Minutes", "kind": "number"}],
                "submit": {"label": "Save", "method": "PUT", "path": "/api/v1/window"},
            }
        ]
    }
    [form] = PageBuilder("dealops", KEY).build(page)["blocks"]
    assert form["submit"]["label"] == "Save"
    assert actions.verify(KEY, form["submit"]["token"]).label == "Duplicate time limit: Save"
