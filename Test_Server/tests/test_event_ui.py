import json
import subprocess
from pathlib import Path

import pytest

RUNNER = Path(__file__).with_name("event_ui_runner.js")


def run_ui(script, page="index"):
    result = subprocess.run(
        ["node", str(RUNNER), page, script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "phase", ["ready", "racing", "stopped", "name_entry", "leaderboard"]
)
def test_header_shows_active_event_not_captured_race_event(phase):
    result = run_ui(
        """
const current = {...eventBoard, name: 'DHCP26 <patch & play>'};
handlers.game_state(state({phase: PHASE, race_id: 'race', race_leaderboard: archive,
    leaderboard_policy: {...state().leaderboard_policy, next: current, active_event: current}}));
output({text: el('currentEventTitle').textContent, hidden: el('currentEventTitle').hidden,
    children: el('currentEventTitle').children.length});
""".replace("PHASE", json.dumps(phase))
    )
    assert result == {
        "text": "DHCP26 <patch & play>",
        "hidden": False,
        "children": 0,
    }


def test_header_tracks_event_start_rename_and_expiry_while_browsing_archive():
    result = run_ui("""
const title = () => ({text: el('currentEventTitle').textContent, hidden: el('currentEventTitle').hidden});
handlers.game_state(state());
const before = title();
openLeaderboard(archive);
handlers.game_state(state({revision: 2, leaderboard_policy: {...state().leaderboard_policy, next: eventBoard, active_event: eventBoard}}));
const active = title();
const renamed = {...eventBoard, name: 'DHCP26'};
handlers.game_state(state({revision: 3, leaderboard_policy: {...state().leaderboard_policy, next: renamed, active_event: renamed}}));
const renamedTitle = title();
handlers.game_state(state({revision: 4}));
output({before, active, renamed: renamedTitle, ended: title(), viewed: browsing.leaderboardId});
""")
    assert result == {
        "before": {"text": "", "hidden": True},
        "active": {"text": "Patch night", "hidden": False},
        "renamed": {"text": "DHCP26", "hidden": False},
        "ended": {"text": "", "hidden": True},
        "viewed": 209,
    }


def test_header_clears_unconfirmed_event_on_disconnect_and_refreshes_on_reconnect():
    result = run_ui("""
handlers.game_state(state({leaderboard_policy: {...state().leaderboard_policy, next: eventBoard, active_event: eventBoard}}));
handlers.disconnect();
const disconnected = {text: el('currentEventTitle').textContent, hidden: el('currentEventTitle').hidden};
managerHandlers.open();
handlers.game_state(state({revision: 2}));
output({disconnected, reconnected: {text: el('currentEventTitle').textContent, hidden: el('currentEventTitle').hidden}});
""")
    assert result == {
        "disconnected": {"text": "", "hidden": True},
        "reconnected": {"text": "", "hidden": True},
    }


def test_l_opens_active_event_and_zero_returns_from_numbered_browse():
    result = run_ui("""
handlers.game_state(state({leaderboard_policy: {...state().leaderboard_policy, next: eventBoard, active_event: eventBoard}}));
key('l');
const opened = {title: el('leaderboardTitle').textContent, request: lastRequest('request_leaderboard').data};
key('3');
const numberedTitle = el('leaderboardTitle').textContent;
key('0', {code: 'Numpad0'});
output({opened, numberedTitle, viewed: lastRequest('request_leaderboard').data.leaderboard_id,
    scoring: requests.filter(r => r.event === 'select_leaderboard_slot').length});
""")
    assert result["opened"]["title"] == "Leaderboard - Patch night"
    assert result["opened"]["request"]["leaderboard_id"] == 210
    assert result["numberedTitle"] == "Leaderboard"
    assert result["viewed"] == 210
    assert result["scoring"] == 0


@pytest.mark.parametrize("phase", ["ready", "stopped", "leaderboard"])
def test_digits_browse_locally_when_event_locked_or_results_open(phase):
    result = run_ui(
        """
handlers.game_state(state({phase: PHASE, race_id: 'race', race_leaderboard: eventBoard,
    leaderboard_policy: {...state().leaderboard_policy, next: eventBoard, active_event: eventBoard}}));
key('4', {code: 'Numpad4'});
output({request: lastRequest('request_leaderboard')?.data,
    scoring: requests.filter(r => r.event === 'select_leaderboard_slot').length,
    destination: el('scoreDestination').textContent});
""".replace("PHASE", json.dumps(phase))
    )
    assert result["request"]["leaderboard_id"] == 104
    assert result["scoring"] == 0
    assert "Patch night" in result["destination"]


def test_same_stored_slot_reselection_after_expiry_still_sends_command():
    result = run_ui("""
handlers.game_state(state());
key('4');
reply('select_leaderboard_slot', {accepted: true, leaderboard_slot: 4});
handlers.game_state(state({revision: 2, leaderboard_policy: {...state().leaderboard_policy, revision: 2, next: numbered[3]}}));
key('4');
output(requests.filter(r => r.event === 'select_leaderboard_slot').map(r => r.data));
""")
    assert result == [{"leaderboard_slot": 4}, {"leaderboard_slot": 4}]


@pytest.mark.parametrize("key_name", ["l", "0", "4", "e"])
@pytest.mark.parametrize(
    "guard",
    [
        "racing",
        "name_entry",
        "maintenance",
        "mode",
        "input",
        "ctrl",
        "meta",
        "alt",
        "compose",
        "repeat",
    ],
)
def test_event_shortcuts_respect_existing_guards(key_name, guard):
    result = run_ui(
        """
handlers.game_state(state({phase: ['racing','name_entry','maintenance'].includes(GUARD) ? GUARD : 'ready',
    leaderboard_policy: {...state().leaderboard_policy, next: eventBoard, active_event: eventBoard}}));
if (GUARD === 'mode') el('modeDialog').open = true;
const patch = {ctrlKey: GUARD === 'ctrl', metaKey: GUARD === 'meta', altKey: GUARD === 'alt', isComposing: GUARD === 'compose', repeat: GUARD === 'repeat'};
if (GUARD === 'input') patch.target = new Element('input');
requests.length = 0;
key(KEY, patch);
output(requests);
""".replace("GUARD", json.dumps(guard)).replace("KEY", json.dumps(key_name))
    )
    assert result == []


def test_zero_without_event_and_e_outside_leaderboard_do_nothing():
    assert (
        run_ui("""
handlers.game_state(state()); key('0'); key('e'); output(requests);
""")
        == []
    )


def test_archive_dialog_generation_focus_keyboard_and_frozen_list():
    result = run_ui("""
handlers.game_state(state()); key('l'); el('pastEventsButton').focus(); key('E');
const first = lastRequest('list_events'); key('e'); key('e');
first.callback(null, {accepted: true, events: [archive], revision: 1});
const lateCount = el('pastEventOptions').children.length;
reply('list_events', {accepted: true, events: [archive, {...archive, id: 208, name: 'Older'}], revision: 1});
const newestFocused = document.activeElement.dataset.eventId;
key('ArrowDown'); key('Enter');
const picked = {id: browsing.leaderboardId, open: el('pastEventsDialog').open, title: el('leaderboardTitle').textContent};
key('e'); reply('list_events', {accepted: true, events: [archive, {...archive, id: 208}], revision: 1});
const currentFocused = document.activeElement.dataset.eventId;
key('Escape');
output({request: first.data, lateCount, newestFocused, picked, currentFocused, focus: document.activeElement.id});
""")
    assert result == {
        "request": {"past_only": True},
        "lateCount": 0,
        "newestFocused": "209",
        "picked": {"id": 208, "open": False, "title": "Leaderboard - Older"},
        "currentFocused": "208",
        "focus": "pastEventsButton",
    }


@pytest.mark.parametrize("outcome", ["empty", "error", "timeout"])
def test_archive_loading_errors_allow_retry_and_close(outcome):
    result = run_ui(
        """
handlers.game_state(state()); key('l'); key('e');
const loading = el('pastEventsStatus').textContent;
reply('list_events', OUTCOME === 'empty' ? {accepted: true, events: [], revision: 1} : {accepted: false, error: 'Catalog unavailable'}, OUTCOME === 'timeout' ? new Error('timeout') : null);
const message = el('pastEventsStatus').textContent;
const retry = !el('retryPastEvents').hidden;
key('Escape'); output({loading, message, retry, open: el('pastEventsDialog').open});
""".replace("OUTCOME", json.dumps(outcome))
    )
    assert "Loading" in result["loading"]
    assert result["message"]
    assert result["retry"] is (outcome != "empty")
    assert not result["open"]
    if outcome == "empty":
        assert result["message"] == "No past events yet"


def test_archive_modal_blocks_underlying_commands_and_does_not_suspend_expiry():
    result = run_ui("""
handlers.game_state(state()); key('l'); key('e');
const before = requests.length;
for (const k of ['l', '0', '4', ' ', 'm', 'v', 's']) key(k);
const added = requests.length - before;
browsing.deadline = 0; updateLeaderboardCountdown();
output({added, browsing: Boolean(browsing), open: el('pastEventsDialog').open});
""")
    assert result == {"added": 0, "browsing": False, "open": False}


def test_policy_boundary_and_rename_preserve_explicit_archive_snapshot_and_focus():
    result = run_ui("""
handlers.game_state(state()); openLeaderboard(archive);
browsing.boundary = 55; browsing.rows.set(17, new Element('tr'));
el('loadLeaderboardPage').focus();
handlers.game_state(state({revision: 2, leaderboard_policy: {...state().leaderboard_policy, next: eventBoard, active_event: eventBoard}}));
handlers.leaderboard_changed({leaderboard: {...archive, name: 'Renamed'}, revision: 2});
output({id: browsing.leaderboardId, boundary: browsing.boundary, rows: browsing.rows.size,
    title: el('leaderboardTitle').textContent, focus: document.activeElement.id,
    requests: requests.filter(r => r.event === 'request_leaderboard').length});
""")
    assert result == {
        "id": 209,
        "boundary": 55,
        "rows": 1,
        "title": "Leaderboard - Renamed",
        "focus": "loadLeaderboardPage",
        "requests": 1,
    }


@pytest.mark.parametrize("archive_view", [False, True])
def test_global_manual_selection_follows_numbered_views_but_keeps_archives(
    archive_view,
):
    result = run_ui(
        """
handlers.game_state(state()); openLeaderboard(ARCHIVE ? archive : numbered[0]);
handlers.game_state(state({revision: 2, leaderboard_policy: {...state().leaderboard_policy, revision: 2, next: numbered[3]}}));
handlers.leaderboard_selected({leaderboard: numbered[3], revision: 2});
const otherDisplay = browsing.leaderboardId;
key('4'); reply('select_leaderboard_slot', {accepted: true, leaderboard_slot: 4});
output({otherDisplay, local: browsing.leaderboardId});
""".replace("ARCHIVE", json.dumps(archive_view))
    )
    assert result == {"otherDisplay": 209 if archive_view else 104, "local": 104}


def test_post_race_defaults_to_captured_board_and_save_refresh_keeps_other_view():
    result = run_ui("""
const pending = state({phase: 'leaderboard', race_id: 'race', race_leaderboard: eventBoard,
    persistence_status: 'pending', remaining_seconds: null});
handlers.game_state(pending);
const defaultId = browsing.leaderboardId;
openLeaderboard(archive); const old = lastRequest('request_leaderboard').data;
handlers.game_state({...pending, revision: 2, browse_epoch: 2, persistence_status: 'error', persistence_error: 'Save failed'});
const error = {visible: !el('persistenceError').hidden, retry: !el('retryScores').hidden, id: browsing.leaderboardId};
handlers.game_state({...pending, revision: 3, browse_epoch: 3, persistence_status: 'saved', remaining_seconds: 60});
handlers.leaderboard_snapshot({...old, boundary: 5, rows: [], next_offset: null, offset: 0});
output({defaultId, error, id: browsing.leaderboardId, staleBoundary: browsing.boundary,
    request: lastRequest('request_leaderboard').data, title: el('leaderboardTitle').textContent});
""")
    assert result["defaultId"] == 210
    assert result["error"] == {"visible": True, "retry": True, "id": 209}
    assert result["id"] == 209 and result["staleBoundary"] is None
    assert result["request"]["leaderboard_id"] == 209
    assert result["request"]["race_id"] == "race"
    assert result["title"] == "Leaderboard - Old <night>"


def test_initial_pending_save_blocks_dismissal_without_recovery_error():
    result = run_ui("""
handlers.game_state(state({phase: 'leaderboard', race_id: 'race', race_leaderboard: eventBoard, persistence_status: 'pending'}));
key('Escape'); key('l'); key(' ');
output({dismissals: requests.filter(r => r.event === 'dismiss_leaderboard').length, disabled: el('closeLeaderboard').disabled});
""")
    assert result == {"dismissals": 0, "disabled": True}


def test_reconnect_keeps_explicit_archive_when_race_context_still_matches():
    result = run_ui("""
handlers.game_state(state()); openLeaderboard(archive);
handlers.disconnect(); managerHandlers.open(); handlers.game_state(state({revision: 2}));
output({id: browsing?.leaderboardId, title: el('leaderboardTitle').textContent});
""")
    assert result == {"id": 209, "title": "Leaderboard - Old <night>"}


@pytest.mark.parametrize("button", ["currentEventButton", "pastEventsButton"])
def test_space_activates_event_buttons_without_dismissing_results(button):
    result = run_ui(
        """
handlers.game_state(state({phase: 'leaderboard', race_id: 'race', race_leaderboard: archive,
    leaderboard_policy: {...state().leaderboard_policy, next: eventBoard, active_event: eventBoard}}));
el(BUTTON).focus(); key(' ');
output({dismissals: requests.filter(r => r.event === 'dismiss_leaderboard').length,
    active: browsing.leaderboardId, picker: el('pastEventsDialog').open});
""".replace("BUTTON", json.dumps(button))
    )
    assert result == {
        "dismissals": 0,
        "active": 210 if button == "currentEventButton" else 209,
        "picker": button == "pastEventsButton",
    }


def test_current_event_navigation_counts_as_result_activity():
    result = run_ui("""
handlers.game_state(state({phase: 'leaderboard', race_id: 'race', race_leaderboard: archive,
    leaderboard_policy: {...state().leaderboard_policy, next: eventBoard, active_event: eventBoard}}));
key('0'); output(requests.filter(r => r.event === 'leaderboard_activity').map(r => r.data));
""")
    assert result == [{"race_id": "race"}]


def test_result_page_does_not_move_focus_out_of_open_archive_picker():
    result = run_ui("""
handlers.game_state(state({phase: 'leaderboard', race_id: 'race', race_leaderboard: eventBoard}));
const request = lastRequest('request_leaderboard').data;
key('e');
handlers.leaderboard_snapshot({...request, leaderboard: eventBoard, boundary: 1, offset: 0, next_offset: null, target_score_id: 1,
    rows: [{id: 1, rank: 1, name: 'Ada', duration_ms: 1000, attempts: 1, created_at: null, current_race: true}]});
output({focus: document.activeElement.id, picker: el('pastEventsDialog').open});
""")
    assert result == {"focus": "closePastEvents", "picker": True}


def test_expired_picker_callback_and_maintenance_close_cannot_reopen_it():
    result = run_ui("""
handlers.game_state(state()); key('l'); key('e');
const request = lastRequest('list_events');
handlers.game_state(state({revision: 2, phase: 'maintenance', browse_epoch: 2}));
request.callback(null, {accepted: true, events: [archive], revision: 1});
output({open: el('pastEventsDialog').open, items: el('pastEventOptions').children.length, view: Boolean(browsing)});
""")
    assert result == {"open": False, "items": 0, "view": False}


def test_archive_tabs_wrap_and_range_is_always_berlin():
    result = run_ui("""
handlers.game_state(state()); key('l'); key('e');
reply('list_events', {accepted: true, events: [archive], revision: 1});
const first = document.activeElement;
key('Tab', {shiftKey: true}); const last = document.activeElement.id;
key('Tab'); output({last, first: document.activeElement === first, range: first.children[1].textContent});
""")
    assert result["last"] == "closePastEvents" and result["first"]
    assert "11:00" in result["range"] and "13:00" in result["range"]
    assert "Europe/Berlin" in result["range"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("leaderboard_id", 102),
        ("mode_id", "quarter-field"),
        ("race_id", "another"),
        ("request_id", 20),
        ("offset", 50),
    ],
)
def test_page_identity_checks_keep_wrong_board_mode_and_generation_out(field, value):
    result = run_ui(
        """
handlers.game_state(state()); openLeaderboard(archive);
const request = lastRequest('request_leaderboard').data;
handlers.leaderboard_snapshot({...request, [FIELD]: VALUE, boundary: 42, rows: [], next_offset: null});
output({boundary: browsing.boundary, pending: browsing.pendingOffset});
""".replace("FIELD", json.dumps(field)).replace("VALUE", json.dumps(value))
    )
    assert result == {"boundary": None, "pending": 0}


def test_pagination_timeout_restarts_same_archive_not_default_board():
    result = run_ui("""
handlers.game_state(state()); openLeaderboard(archive);
timers.get(browsing.pendingTimer)(); el('loadLeaderboardPage').fire('click');
output({id: browsing.leaderboardId, requestId: browsing.requestId, boundary: browsing.boundary});
""")
    assert result == {"id": 209, "requestId": 2, "boundary": None}


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize(
    "mode", ["full-field", "half-field", "quarter-field", "custom-staged"]
)
def test_schedule_boundaries_preserve_explicit_view_in_every_mode(named, mode):
    result = run_ui(
        """
const base = state({mode: {...state().mode, id: MODE}});
handlers.game_state(base); openLeaderboard(NAMED ? archive : numbered[3]);
browsing.boundary = 77;
const requestId = browsing.requestId;
handlers.game_state({...base, revision: 2, leaderboard_policy: {...base.leaderboard_policy, next: eventBoard, active_event: eventBoard}});
handlers.game_state({...base, revision: 3});
output({id: browsing.leaderboardId, sameRequest: requestId === browsing.requestId,
    boundary: browsing.boundary, mode: lastRequest('request_leaderboard').data.mode_id});
""".replace("MODE", json.dumps(mode)).replace("NAMED", json.dumps(named))
    )
    assert result == {
        "id": 209 if named else 104,
        "sameRequest": True,
        "boundary": 77,
        "mode": mode,
    }


@pytest.mark.parametrize("publish_expiry", [False, True])
@pytest.mark.parametrize("slot", [1, 4])
def test_explicit_manual_signal_follows_same_slot_or_first_update_after_expiry(
    publish_expiry, slot
):
    result = run_ui(
        """
handlers.game_state(state({leaderboard_policy: {...state().leaderboard_policy, next: eventBoard, active_event: eventBoard}}));
key('3');
if (PUBLISHED) handlers.game_state(state({revision: 2}));
handlers.game_state(state({revision: 3, leaderboard_policy: {...state().leaderboard_policy, revision: 2, next: numbered[SLOT - 1]}}));
const beforeSignal = browsing.leaderboardId;
handlers.leaderboard_selected({leaderboard: numbered[SLOT - 1], revision: 2});
const afterSignal = browsing.leaderboardId;
const requestId = browsing.requestId;
handlers.leaderboard_selected({leaderboard: numbered[SLOT - 1], revision: 2});
output({beforeSignal, afterSignal, duplicateKeptSnapshot: browsing.requestId === requestId});
""".replace("PUBLISHED", json.dumps(publish_expiry)).replace("SLOT", str(slot))
    )
    assert result == {
        "beforeSignal": 103,
        "afterSignal": 100 + slot,
        "duplicateKeptSnapshot": True,
    }


@pytest.mark.parametrize("view", ["numbered", "archive"])
def test_metadata_edits_and_boundaries_keep_explicit_views_without_manual_signal(view):
    result = run_ui(
        """
handlers.game_state(state()); openLeaderboard(VIEW === 'archive' ? archive : numbered[2]);
const requestId = browsing.requestId;
handlers.game_state(state({revision: 2, leaderboard_policy: {...state().leaderboard_policy, revision: 2}}));
handlers.leaderboard_changed({leaderboard: {...archive, name: 'Renamed'}, revision: 2});
handlers.game_state(state({revision: 3, leaderboard_policy: {...state().leaderboard_policy, revision: 2, next: eventBoard, active_event: eventBoard}}));
handlers.game_state(state({revision: 4, leaderboard_policy: {...state().leaderboard_policy, revision: 2}}));
output({id: browsing.leaderboardId, sameSnapshot: browsing.requestId === requestId});
""".replace("VIEW", json.dumps(view))
    )
    assert result == {"id": 209 if view == "archive" else 103, "sameSnapshot": True}


@pytest.mark.parametrize(
    "guard",
    [
        "stale",
        "future",
        "wrong-board",
        "event",
        "stopped",
        "leaderboard",
        "disconnected",
        "reconnected",
        "new-context",
        "archive",
    ],
)
def test_manual_signal_rejects_stale_or_incompatible_context(guard):
    result = run_ui(
        """
handlers.game_state(state()); openLeaderboard(GUARD === 'archive' ? archive : numbered[2]);
const patch = {revision: 2, leaderboard_policy: {...state().leaderboard_policy, revision: 2, next: numbered[3]}};
if (GUARD === 'event') patch.leaderboard_policy = {...patch.leaderboard_policy, active_event: eventBoard, next: eventBoard};
if (['stopped','leaderboard'].includes(GUARD)) { patch.phase = GUARD; patch.race_id = 'race'; patch.race_leaderboard = eventBoard; }
if (GUARD === 'new-context') patch.browse_epoch = 2;
handlers.game_state(state(patch));
if (GUARD === 'stopped' || GUARD === 'new-context') openLeaderboard(numbered[2]);
if (GUARD === 'reconnected') { handlers.disconnect(); managerHandlers.open(); handlers.game_state(state(patch)); }
if (GUARD === 'disconnected') handlers.disconnect();
const before = browsing?.leaderboardId, requestCount = requests.length;
handlers.leaderboard_selected({leaderboard: numbered[GUARD === 'wrong-board' ? 1 : 3], revision: GUARD === 'stale' ? 1 : GUARD === 'future' ? 3 : 2});
output({sameBoard: before === browsing?.leaderboardId, sameRequests: requests.length === requestCount});
""".replace("GUARD", json.dumps(guard))
    )
    assert result == {"sameBoard": True, "sameRequests": True}


def test_rejected_manual_ack_preserves_archive_and_policy():
    result = run_ui("""
handlers.game_state(state()); openLeaderboard(archive); key('4');
reply('select_leaderboard_slot', {accepted: false, error: 'Write rejected'});
output({id: browsing.leaderboardId, next: gameState.leaderboard_policy.next.id, error: el('leaderboardSlotError').textContent});
""")
    assert result == {"id": 209, "next": 101, "error": "Write rejected"}


def test_local_ack_leaves_archive_after_ordered_state_and_same_slot_signal():
    result = run_ui("""
handlers.game_state(state()); openLeaderboard(archive); key('1');
handlers.game_state(state({revision: 2, leaderboard_policy: {...state().leaderboard_policy, revision: 2}}));
handlers.leaderboard_selected({leaderboard: numbered[0], revision: 2});
const beforeAck = browsing.leaderboardId;
reply('select_leaderboard_slot', {accepted: true, leaderboard_slot: 1});
output({beforeAck, afterAck: browsing.leaderboardId, focus: document.activeElement.id, pending: pendingLeaderboardSlot});
""")
    assert result == {
        "beforeAck": 209,
        "afterAck": 101,
        "focus": "leaderboardTitle",
        "pending": None,
    }


@pytest.mark.parametrize("slot", [1, 4])
@pytest.mark.parametrize("close", ["Escape", "e", "button", "cancel"])
def test_picker_close_reconciles_accepted_manual_selection(slot, close):
    result = run_ui(
        """
handlers.game_state(state()); openLeaderboard(numbered[2]); key('e');
const requestId = browsing.requestId;
handlers.game_state(state({revision: 2, leaderboard_policy: {...state().leaderboard_policy, revision: 2, next: numbered[SLOT - 1]}}));
handlers.leaderboard_selected({leaderboard: numbered[SLOT - 1], revision: 2});
const whileOpen = {id: browsing.leaderboardId, sameRequest: browsing.requestId === requestId, focus: document.activeElement.id};
if (CLOSE === 'button') el('closePastEvents').fire('click');
else if (CLOSE === 'cancel') el('pastEventsDialog').fire('cancel');
else key(CLOSE);
output({whileOpen, id: browsing.leaderboardId, picker: el('pastEventsDialog').open,
    pages: requests.filter(r => r.event === 'request_leaderboard').map(r => r.data.leaderboard_id), focus: document.activeElement.id});
""".replace("SLOT", str(slot)).replace("CLOSE", json.dumps(close))
    )
    assert result == {
        "whileOpen": {"id": 103, "sameRequest": True, "focus": "closePastEvents"},
        "id": 100 + slot,
        "picker": False,
        "pages": [103, 100 + slot],
        "focus": "pastEventsButton",
    }


@pytest.mark.parametrize("existing_archive", [False, True])
def test_picker_manual_selection_preserves_existing_or_newly_chosen_archive(
    existing_archive,
):
    result = run_ui(
        """
handlers.game_state(state()); openLeaderboard(EXISTING ? archive : numbered[2]); key('e');
reply('list_events', {accepted: true, events: [archive], revision: 1});
handlers.game_state(state({revision: 2, leaderboard_policy: {...state().leaderboard_policy, revision: 2, next: numbered[3]}}));
handlers.leaderboard_selected({leaderboard: numbered[3], revision: 2});
key(EXISTING ? 'Escape' : 'Enter');
key('e'); key('Escape');
output({id: browsing.leaderboardId, pages: requests.filter(r => r.event === 'request_leaderboard').map(r => r.data.leaderboard_id)});
""".replace("EXISTING", json.dumps(existing_archive))
    )
    assert result == {
        "id": 209,
        "pages": [209] if existing_archive else [103, 209],
    }


@pytest.mark.parametrize(
    "invalidation",
    ["phase", "mode", "epoch", "event-lock", "unavailable", "reconnect", "expiry"],
)
def test_picker_discards_manual_selection_after_context_invalidation(invalidation):
    result = run_ui(
        """
handlers.game_state(state()); openLeaderboard(numbered[2]); key('e');
const selected = state({revision: 2, leaderboard_policy: {...state().leaderboard_policy, revision: 2, next: numbered[3]}});
handlers.game_state(selected); handlers.leaderboard_selected({leaderboard: numbered[3], revision: 2});
if (INVALIDATION === 'phase') handlers.game_state({...selected, revision: 3, phase: 'racing', race_id: 'race'});
if (INVALIDATION === 'mode') handlers.game_state({...selected, revision: 3, mode: {...selected.mode, id: 'half-field'}});
if (INVALIDATION === 'epoch') handlers.game_state({...selected, revision: 3, browse_epoch: 2});
if (INVALIDATION === 'event-lock') handlers.game_state({...selected, revision: 3, leaderboard_policy: {...selected.leaderboard_policy, next: eventBoard, active_event: eventBoard}});
if (INVALIDATION === 'unavailable') handlers.game_state({...selected, revision: 3, leaderboard_policy: {...selected.leaderboard_policy, available: false, next: null}});
if (INVALIDATION === 'reconnect') { handlers.disconnect(); managerHandlers.open(); }
if (INVALIDATION === 'expiry') { browsing.deadline = 0; el('closePastEvents').fire('click'); }
handlers.game_state({...selected, revision: 4});
const restored = browsing?.leaderboardId ?? null;
if (el('pastEventsDialog').open) key('Escape');
if (!browsing) openLeaderboard(numbered[2]);
key('e'); key('Escape');
output({restored, id: browsing.leaderboardId, pages: requests.filter(r => r.event === 'request_leaderboard').map(r => r.data.leaderboard_id)});
""".replace("INVALIDATION", json.dumps(invalidation))
    )
    kept_view = invalidation in ("event-lock", "unavailable", "reconnect")
    assert result == {
        "restored": 103 if kept_view else None,
        "id": 103,
        "pages": [103] if invalidation in ("event-lock", "unavailable") else [103, 103],
    }


@pytest.mark.parametrize("last_slot", [1, 3])
def test_picker_reconciles_latest_choice_without_replaying_older_signal(last_slot):
    result = run_ui(
        """
handlers.game_state(state()); openLeaderboard(numbered[2]); key('e');
for (const [slot, revision] of [[4, 2], [LAST_SLOT, 3]]) {
    handlers.game_state(state({revision, leaderboard_policy: {...state().leaderboard_policy, revision, next: numbered[slot - 1]}}));
    handlers.leaderboard_selected({leaderboard: numbered[slot - 1], revision});
}
handlers.leaderboard_selected({leaderboard: numbered[3], revision: 2});
key('Escape');
output({id: browsing.leaderboardId, pages: requests.filter(r => r.event === 'request_leaderboard').map(r => r.data.leaderboard_id)});
""".replace("LAST_SLOT", str(last_slot))
    )
    assert result == {
        "id": 100 + last_slot,
        "pages": [103] if last_slot == 3 else [103, 101],
    }


def test_picker_keeps_accepted_choice_across_unrelated_metadata_edit():
    result = run_ui("""
handlers.game_state(state()); openLeaderboard(numbered[2]); key('e');
handlers.game_state(state({revision: 2, leaderboard_policy: {...state().leaderboard_policy, revision: 2, next: numbered[3]}}));
handlers.leaderboard_selected({leaderboard: numbered[3], revision: 2});
handlers.game_state(state({revision: 3, leaderboard_policy: {...state().leaderboard_policy, revision: 3, next: numbered[3]}}));
handlers.leaderboard_changed({leaderboard: {...archive, name: 'Renamed'}, revision: 3});
key('Escape'); output({id: browsing.leaderboardId});
""")
    assert result == {"id": 104}
