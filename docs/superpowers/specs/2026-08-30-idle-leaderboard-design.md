# Idle Leaderboard Design

## Goal

Make the existing top-10 leaderboard available while the kiosk is idle, display each entry's existing creation timestamp, and preserve the current race controls and ranking behavior.

## Scope

This change adds:

- A read-only idle leaderboard available in `ready` and `stopped`.
- A compact leaderboard control below the two player panels.
- `L` and `Escape` keyboard behavior plus a 60-second idle-view timeout.
- A `Set at` column based on the existing `created_at` value.
- `L` and `Escape` dismissal for the existing post-race leaderboard.

Starting remains unchanged. Cached cable state does not prevent `/start-clock` or Space from starting a ready game.

Leaderboard administration, deleting records, pagination, schema changes, and changing the top-10 ranking rules are out of scope.

## Architecture

The idle leaderboard is a client-local view backed by a targeted Socket.IO request. The server race phase remains `ready` or `stopped` while the view is open. A read-only screen therefore cannot participate in start, reset, completion, or post-race transitions.

The existing post-race leaderboard markup and row renderer are reused. The server reads through `LeaderboardStore.top_entries()` and emits results only to the requesting Socket.IO client.

## Idle Behavior

The leaderboard can be opened only while the authoritative phase is `ready` or `stopped`:

- Pressing `L` requests current rows and opens the view.
- Activating the visible `Leaderboard` control below the player panels does the same.
- Pressing `L` again closes or cancels the pending request.
- Pressing `Escape` closes or cancels the pending request.
- A client-side 60-second deadline closes an open idle view automatically.

Each request carries a monotonically increasing client request ID. The targeted response echoes that ID. The browser ignores responses that no longer match its pending request, so a delayed response cannot reopen a cancelled view.

Opening and closing the idle view does not change race state or broadcast to other clients. A `game_state` update that enters an active phase closes the view immediately. Space does nothing while the idle view is open, preventing a race from starting behind hidden player panels.

The existing post-race leaderboard keeps its server-owned 60-second deadline. It additionally dismisses with `L` or `Escape`; Space remains supported.

## UI and Layout

The compact leaderboard control sits below the two player panels inside `raceView`. It is visible in `ready` and `stopped` and hidden while racing. Both player panels, the control, and the shared leaderboard view must fit a 1920x1080 fullscreen viewport without scrolling or overlap.

The leaderboard table has four columns:

1. Rank
2. Name
3. Time
4. Set at

The server already stores `created_at` as an ISO 8601 UTC timestamp on every entry. The browser displays it in its local timezone as `DD.MM.YYYY HH:MM`. A malformed value displays `-` defensively.

The shared table preserves current-race highlighting for post-race rows. An empty leaderboard displays one four-column row with `No records yet`.

The leaderboard heading receives focus when the idle view opens. Closing restores focus to the visible leaderboard button. Keyboard-triggered open and close are immediate and do not animate.

## Server Events

The browser sends:

```text
request_leaderboard { request_id }
```

When phase is `ready` or `stopped`, the server replies only to the requesting connection:

```text
leaderboard_snapshot { request_id, rows, error }
```

Requests in `racing`, `name_entry`, or the authoritative post-race `leaderboard` phase are ignored. The request ID is an opaque correlation value and does not authorize access; phase validation remains server-side.

## Failure Handling

An idle leaderboard read failure is logged and returned as `Leaderboard could not be loaded`. The view remains dismissible and the race remains idle.

Malformed event payloads may use a null correlation value but cannot change race state. Stale responses are ignored by the browser. Timers are cleared whenever the view closes or reopens.

## Testing

Integration tests cover:

- Idle rows are returned only to the requesting client in `ready` and `stopped`.
- Responses echo the request ID.
- Requests during an active game are ignored.
- Read failures return a non-fatal targeted error.
- Existing start routes continue to start with clear or connected cached board states.

UI and browser acceptance cover:

- The integrated control is phase-aware.
- `L` and the button open the same view.
- A second `L` or `Escape` cancels a pending request and ignores its delayed response.
- `L`, `Escape`, and the 60-second deadline close an open view.
- Active game-state updates close the idle view.
- Post-race `L` and `Escape` dismiss the authoritative leaderboard.
- The `Set at` column renders `created_at` in local time.
- Focus enters the leaderboard and returns to the control.
- Race and leaderboard views fit at 1920x1080 without overlap or scrolling.

## Acceptance Criteria

1. In `ready` or `stopped`, `L` and the integrated control show the current top 10 without changing server race state.
2. The idle leaderboard closes on `L`, `Escape`, an active phase update, or 60 seconds.
3. A delayed response cannot reopen a cancelled idle leaderboard.
4. The post-race leaderboard dismisses on `L` or `Escape` and retains its automatic timeout and Space behavior.
5. Every row shows its existing `created_at` date and time in the `Set at` column.
6. Starting behavior remains unchanged regardless of cached cable states.
7. Both race cards, the integrated control, and all leaderboard columns fit a 1920x1080 fullscreen display.
8. A database read failure does not make the kiosk unable to run a race.
