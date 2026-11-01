import json

import pytest

from tests.test_event_ui import run_ui

SETUP = """
handlers.game_state(state());
reply('list_events', {accepted: true, events: [], revision: 1});
el('eventName').value = ' Patch night ';
el('eventStart').value = '2026-11-01T11:00';
el('eventEnd').value = '2026-11-01T13:00';
el('eventStartNow').checked = false;
"""


def test_create_uses_berlin_wall_input_without_browser_zone_conversion():
    result = run_ui(
        SETUP
        + """
el('eventForm').fire('submit');
output({request: lastRequest('save_event').data, disabled: el('saveEvent').disabled});
""",
        "control",
    )
    assert result == {
        "request": {
            "event": {
                "name": "Patch night",
                "starts_at": "2026-11-01T11:00",
                "ends_at": "2026-11-01T13:00",
                "start_now": False,
            },
            "revision": 1,
        },
        "disabled": True,
    }


def test_start_now_uses_server_time_and_success_refreshes_catalog():
    result = run_ui(
        SETUP
        + """
el('eventStartNow').checked = true; el('eventStartNow').fire('change');
el('eventForm').fire('submit'); const request = lastRequest('save_event').data;
reply('save_event', {accepted: true, event: eventBoard, revision: 2});
output({request, lists: requests.filter(r => r.event === 'list_events').length, name: el('eventName').value});
""",
        "control",
    )
    assert result["request"]["event"]["start_now"] is True
    assert "starts_at" not in result["request"]["event"]
    assert result["lists"] == 2 and result["name"] == ""


@pytest.mark.parametrize(
    "message",
    [
        "Name must be printable",
        "End must be later than start",
        "Ambiguous Europe/Berlin time",
        "Event overlaps another event",
        "Configuration changed; reload and try again",
    ],
)
def test_rejected_edits_keep_inputs_and_display_server_validation(message):
    result = run_ui(
        SETUP
        + """
el('eventForm').fire('submit');
reply('save_event', {accepted: false, error: MESSAGE});
output({error: el('eventError').textContent, hidden: el('eventError').hidden, name: el('eventName').value, disabled: el('saveEvent').disabled});
""".replace("MESSAGE", json.dumps(message)),
        "control",
    )
    assert result == {
        "error": message,
        "hidden": False,
        "name": " Patch night ",
        "disabled": False,
    }


@pytest.mark.parametrize("action", ["end_event", "cancel_event"])
def test_destructive_actions_require_confirmation_and_use_loaded_revision(action):
    result = run_ui(
        """
handlers.game_state(state()); reply('list_events', {accepted: true, events: [eventBoard], revision: 1});
editEvent(eventBoard);
window.confirm = () => false; mutateEvent(ACTION); const cancelled = requests.length;
window.confirm = () => true; mutateEvent(ACTION);
output({cancelled, count: requests.length, request: lastRequest(ACTION).data});
""".replace("ACTION", repr(action)),
        "control",
    )
    assert result["count"] == result["cancelled"] + 1
    assert result["request"] == {"id": 210, "revision": 1}


def test_rename_keeps_original_offsets_and_stale_edit_revision():
    result = run_ui(
        """
handlers.game_state(state()); reply('list_events', {accepted: true, events: [archive], revision: 1});
editEvent(archive); el('eventName').value = 'Renamed';
handlers.game_state(state({revision: 2, leaderboard_policy: {...state().leaderboard_policy, revision: 2}}));
el('eventForm').fire('submit');
output({request: lastRequest('save_event').data, start: el('eventStart').value});
""",
        "control",
    )
    assert result["request"] == {
        "event": {
            "id": 209,
            "name": "Renamed",
            "starts_at": "2026-10-31T10:00:00+00:00",
            "ends_at": "2026-10-31T12:00:00+00:00",
            "start_now": False,
        },
        "revision": 1,
    }
    assert result["start"] == "2026-10-31T11:00"


def test_timeout_and_storage_retry_remain_actionable_without_restarting():
    result = run_ui(
        SETUP
        + """
el('eventForm').fire('submit'); reply('save_event', null, new Error('timeout'));
const timedout = {error: el('eventError').textContent, disabled: el('saveEvent').disabled};
el('retryEventStorage').fire('click');
const retry = lastRequest('retry_leaderboard_storage').data;
reply('retry_leaderboard_storage', {accepted: false, error: 'Still unavailable'});
output({timedout, retry, error: el('eventError').textContent, disabled: el('retryEventStorage').disabled});
""",
        "control",
    )
    assert result["timedout"]["error"] and not result["timedout"]["disabled"]
    assert result["retry"] == {}
    assert result["error"] == "Still unavailable" and not result["disabled"]


@pytest.mark.parametrize("name", ["", "   ", "x" * 61])
def test_invalid_name_does_not_issue_mutation(name):
    result = run_ui(
        SETUP
        + """
el('eventName').value = NAME; el('eventForm').fire('submit');
output({sent: Boolean(lastRequest('save_event')), error: el('eventError').textContent});
""".replace("NAME", json.dumps(name)),
        "control",
    )
    assert not result["sent"] and "1-60" in result["error"]


def test_names_count_unicode_codepoints_not_utf16_units():
    result = run_ui(
        SETUP
        + """
el('eventName').value = String.fromCodePoint(0x1f600).repeat(60); el('eventForm').fire('submit');
output({sent: Boolean(lastRequest('save_event'))});
""",
        "control",
    )
    assert result["sent"]


@pytest.mark.parametrize("kind", ["Upcoming", "Active", "Past"])
def test_window_editability_and_actions_follow_event_status(kind):
    result = run_ui(
        """
const board = KIND === 'Upcoming' ? {...eventBoard, starts_at: '2099-11-01T10:00:00Z', ends_at: '2099-11-01T12:00:00Z'} : archive;
handlers.game_state(state({leaderboard_policy: {...state().leaderboard_policy, active_event: KIND === 'Active' ? board : null}}));
reply('list_events', {accepted: true, events: [board], revision: 1}); editEvent(board);
output({start: el('eventStart').disabled, end: el('eventEnd').disabled,
    endNow: !el('endEvent').hidden, cancel: !el('cancelEvent').hidden, name: el('eventName').disabled});
""".replace("KIND", json.dumps(kind)),
        "control",
    )
    assert result == {
        "start": kind != "Upcoming",
        "end": kind == "Past",
        "endNow": kind == "Active",
        "cancel": kind == "Upcoming",
        "name": False,
    }


def test_successful_reload_refreshes_stale_edit_without_creating_new_identity():
    result = run_ui(
        """
handlers.game_state(state()); reply('list_events', {accepted: true, events: [archive], revision: 1}); editEvent(archive);
el('reloadEvents').fire('click'); reply('list_events', {accepted: true, events: [{...archive, name: 'Updated'}], revision: 3});
el('eventName').value = 'New spelling'; el('eventForm').fire('submit');
output(lastRequest('save_event').data);
""",
        "control",
    )
    assert result["revision"] == 3 and result["event"]["id"] == 209
    assert result["event"]["name"] == "New spelling"


def test_unknown_policy_offers_storage_retry_and_ignores_late_disconnected_reply():
    result = run_ui(
        """
handlers.game_state(state({leaderboard_policy: {available: false, error: 'Cannot read schedule', revision: null, next: null, active_event: null, numbered: []}}));
const unavailable = {save: el('saveEvent').disabled, retry: el('retryEventStorage').disabled, status: el('eventDestination').textContent};
el('retryEventStorage').fire('click');
handlers.game_state(state({revision: 2}));
reply('retry_leaderboard_storage', {accepted: true});
const request = lastRequest('list_events'); handlers.disconnect();
request.callback(null, {accepted: true, events: [archive], revision: 1});
output({unavailable, count: el('eventList').children.length, disabled: el('saveEvent').disabled});
""",
        "control",
    )
    assert result == {
        "unavailable": {"save": True, "retry": False, "status": "Cannot read schedule"},
        "count": 0,
        "disabled": True,
    }


@pytest.mark.parametrize("browser_year", [1990, 2099])
@pytest.mark.parametrize(
    "server_time,expected",
    [("09:59:59", "Upcoming"), ("10:00:00", "Active"), ("12:00:00", "Past")],
)
def test_operator_status_and_eligibility_ignore_browser_clock_skew(
    browser_year, server_time, expected
):
    result = run_ui(
        """
Date.now = () => Date.parse('YEAR-01-01T00:00:00Z');
handlers.game_state(state({server_now: '2026-11-01TTIME+00:00', leaderboard_policy: {...state().leaderboard_policy, active_event: KIND === 'Active' ? eventBoard : null}}));
reply('list_events', {accepted: true, events: [eventBoard], revision: 1}); editEvent(eventBoard);
output({label: el('eventList').children[0].children[0].textContent, start: el('eventStart').disabled, end: el('eventEnd').disabled,
    endNow: !el('endEvent').hidden, cancel: !el('cancelEvent').hidden});
""".replace("YEAR", str(browser_year))
        .replace("TIME", server_time)
        .replace("KIND", json.dumps(expected)),
        "control",
    )
    assert result == {
        "label": "Patch night - " + expected,
        "start": expected != "Upcoming",
        "end": expected == "Past",
        "endNow": expected == "Active",
        "cancel": expected == "Upcoming",
    }


def test_server_boundaries_and_clock_corrections_update_list_without_replacing_focus():
    result = run_ui(
        """
Date.now = () => Date.parse('1990-01-01T00:00:00Z');
handlers.game_state(state()); reply('list_events', {accepted: true, events: [eventBoard], revision: 1}); editEvent(eventBoard);
el('eventName').value = 'Draft'; el('eventName').focus();
const button = el('eventList').children[0], labels = [], windows = [];
for (const [time, active] of [['09:00:01', false], ['10:00:00', true], ['12:00:00', false], ['11:00:00', true], ['09:00:00', false]]) {
    handlers.game_state(state({server_now: '2026-11-01T' + time + '+00:00', leaderboard_policy: {...state().leaderboard_policy, active_event: active ? eventBoard : null}}));
    labels.push(el('eventList').children[0].children[0].textContent);
    windows.push([el('eventStart').disabled, el('eventEnd').disabled]);
}
output({labels, windows, sameButton: el('eventList').children[0] === button,
    focus: document.activeElement.id, name: el('eventName').value, catalogs: requests.filter(r => r.event === 'list_events').length});
""",
        "control",
    )
    assert result == {
        "labels": [
            "Patch night - Upcoming",
            "Patch night - Active",
            "Patch night - Past",
            "Patch night - Active",
            "Patch night - Upcoming",
        ],
        "windows": [
            [False, False],
            [True, False],
            [True, True],
            [True, False],
            [False, False],
        ],
        "sameButton": True,
        "focus": "eventName",
        "name": "Draft",
        "catalogs": 1,
    }
