# Post-Race Results and Leaderboard Design

## Goal

After both players complete a race, show a combined result and name-entry screen, collect optional player names using the game PC's keyboard, persist named results in SQLite, and show a top-10 leaderboard before returning to the ready screen.

The result screen must display the exact message `you were able to success`.

## Scope

This design adds:

- Authoritative millisecond race timing.
- Winner, second-place, and tie results.
- Sequential keyboard name entry for both players.
- Optional leaderboard participation by leaving a name empty.
- A local persistent top-10 leaderboard.
- Automatic and keyboard-driven post-race transitions.

Leaderboard administration, remote synchronization, multiple simultaneous races, and profanity filtering are out of scope.

## Architecture

The Flask server owns one kiosk state machine:

```text
ready -> racing -> name_entry -> leaderboard -> ready
            |
            +-> stopped -> ready
```

Only a race in `racing` may accept player completion. Both players completing transitions the race to `name_entry`. Space while racing stops the race without creating a result and enters `stopped`; Space from `stopped` resets to `ready`.

The control page's per-player stop actions remain diagnostic controls. They freeze that player's displayed clock but do not mark the player as verified complete. The race remains in `racing` while at least one player clock is active and enters `stopped` if both players are manually stopped. A later valid serial completion may replace an individual manual stop while the race remains in `racing`, using the actual monotonic completion time. Reset from any phase abandons the active or partially entered race and returns to `ready`.

The backend is divided into three responsibilities:

- `app.py` owns Flask routes, Socket.IO wiring, and serial integration.
- `race.py` owns the locked race state machine, timing, result ordering, name-entry state, and deadlines.
- `leaderboard.py` owns SQLite initialization, insertion, and queries.

The server emits a `game_state` Socket.IO payload after every state change and immediately when a browser connects. The payload contains enough information to render the current phase after a refresh without reconstructing state in the browser.

## Race Timing and Completion

Race start and each player's first valid completion are recorded with `time.monotonic_ns()`. Stored and displayed durations use integer floor division: `(completed_ns - started_ns) // 1_000_000`.

A serial update completes a player only when it contains exactly 48 cells and every cell has verified state `2`. Open state `0`, wrong-cable state `1`, incomplete messages, and duplicate complete updates do not complete a player. Completion is idempotent: the first valid completion timestamp wins.

The result order is ascending by duration. If both durations are equal to the millisecond, the result is a tie. Tied players receive equal visual treatment; Player 1 is prompted first only to make sequential keyboard entry deterministic.

The existing periodic clock updates continue to drive the live display, but the final result and leaderboard always use the server's recorded duration.

## Result and Name-Entry Screen

The race dashboard is replaced by one combined result and name-entry screen when both players complete. It shows:

- The exact message `you were able to success`.
- The winner and second-place player with `mm:ss.mmm` times.
- Equal tie labels instead of an invented winner when times match.
- Two visible name fields associated with the displayed results.
- Actual keyboard focus and a clear visual highlight on the currently active field.
- A subtle name-entry countdown.

Name entry follows result order. The active player can enter up to 12 printable Unicode characters. Leading and trailing whitespace is removed. A name containing only whitespace is empty.

Pressing Enter confirms the active field and advances to the next player. Enter on an empty field skips that player. Repeated Enter therefore skips both players and advances immediately. Space is ordinary name input during this phase and must not trigger race controls.

A 120-second idle deadline starts when the server enters `name_entry`. Draft changes are sent to the server and reset that deadline. Confirming the first player starts a fresh 120-second deadline for the second and moves actual keyboard focus to that field. When the deadline expires:

- Already confirmed non-empty names remain eligible for insertion.
- Confirmed empty names remain skipped.
- The active draft and all unresolved players are skipped.
- The kiosk advances to the leaderboard.

The countdown uses restrained footer copy: `Leaderboard in 1:42 - empty names are skipped`.

## Leaderboard Screen

Each non-empty player submission creates an individual leaderboard entry. Winners and second-place players are treated identically for ranking; only completion time determines leaderboard position.

The screen displays the top 10 entries with:

- Rank.
- Name.
- Completion time in `mm:ss.mmm`.

Equal millisecond durations use competition ranking, such as `1, 1, 3`. Ordering among equal times is stable by creation time and database ID. Entries from the race that just finished are highlighted when they appear in the top 10.

The leaderboard is shown even when both players skip name entry. Its 60-second deadline starts when the server enters `leaderboard`. A subtle footer displays `Next race in 1:00 - press Space to continue` and counts down. The screen returns to `ready` when that deadline expires or immediately when Space is pressed.

## Server State and Events

Every race has a unique `race_id`. Client events include the current `race_id`; stale or duplicate events are ignored.

The server-to-client `game_state` payload includes:

- Current phase and `race_id`.
- Live or final player durations.
- Completion and result order.
- Active name-entry player, confirmed/skipped state, and current drafts.
- Remaining seconds for the active deadline.
- Top-10 rows and IDs belonging to the current race.
- A non-fatal persistence error message when needed.

The browser sends:

- `name_draft` with the active player's full draft after an edit.
- `submit_name` with the active player's full draft when Enter is pressed.
- `dismiss_leaderboard` when Space is pressed on the leaderboard.

The server validates player order, phase, race ID, printability, and length before changing state. Accepted draft activity extends the idle deadline. Invalid data leaves state unchanged.

A Socket.IO background watchdog evaluates monotonic deadlines and emits countdown updates. Deadlines continue while the browser is disconnected. Reconnecting receives the current phase and remaining time.

## Persistence

The database defaults to:

```text
~/.local/share/patchmeifyoucan/leaderboard.sqlite3
```

The default path is resolved from `Path.home()` for the service account (`patchme` on the deployed PC). `LEADERBOARD_DB` may override it. The parent directory is created at startup. Keeping the database outside the checkout prevents `just sync --delete` from removing event data.

Python's built-in `sqlite3` module is sufficient. Each operation uses its own connection with a busy timeout. Schema initialization is idempotent.

`created_at` is an ISO 8601 UTC timestamp.

```sql
CREATE TABLE leaderboard_entries (
    id INTEGER PRIMARY KEY,
    race_id TEXT NOT NULL,
    player_number INTEGER NOT NULL CHECK (player_number IN (1, 2)),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 12),
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (race_id, player_number)
);

CREATE INDEX leaderboard_time_idx
    ON leaderboard_entries (duration_ms, created_at, id);
```

When all players are confirmed, skipped, or timed out, the server inserts all non-empty confirmed names in one transaction. The unique key makes retries idempotent. The server then queries the top 10 and enters `leaderboard`.

## Failure Handling

Database failures must not trap the kiosk. An insertion or query error is logged, the state still advances to `leaderboard`, and the screen shows a restrained `Scores could not be saved` notice. Readable existing rows may still be displayed.

Malformed or stale Socket.IO events are rejected without changing state. Duplicate serial completion messages do not alter final times. Reset cancels active post-race deadlines and clears in-memory drafts.

If the service restarts during a race or name entry, the in-progress race is lost and starts in `ready`; completed leaderboard rows remain in SQLite. Durable recovery of an unfinished race is out of scope.

## Testing

### Race State Tests

Use a fake monotonic clock and direct completion calls without serial hardware. Cover:

- All valid state transitions.
- Global and per-player manual stops never creating a result.
- Millisecond timing.
- Exactly 48 verified cells being required for completion.
- Wrong, open, and incomplete states not completing a player.
- First-completion idempotency.
- Winner ordering and exact ties.
- Sequential name submission.
- Empty Enter and repeated Enter skips.
- Confirmed first-player data surviving second-player timeout.
- Draft activity extending the idle deadline.
- Name-entry timeout behavior.
- Leaderboard Space dismissal and 60-second timeout.
- Reset abandoning unfinished results.

### Leaderboard Tests

Use a temporary SQLite database. Cover:

- Schema initialization and reopening persistence.
- Atomic insertion of zero, one, or two player entries.
- Duplicate race/player protection.
- Name and duration validation.
- Top-10 ordering and truncation.
- Competition ranks (`1, 1, 3`) for equal times.

### Integration and UI Tests

Flask-SocketIO tests cover initial connection state, reconnect during each phase, stale `race_id` rejection, synchronized state across clients, and non-fatal database errors.

Browser acceptance at the deployed display size verifies:

- The exact success message and correct result ordering.
- No overflow or overlapping text.
- Obvious sequential keyboard focus.
- The 12-character limit and printable input.
- Empty Enter and repeated Enter behavior.
- Both subtle countdowns.
- New-row highlighting.
- Phase-specific Space behavior.
- Return to a clean ready screen.

## Acceptance Criteria

1. Both verified boards completing automatically show the combined result and name-entry screen.
2. Final times are server-recorded to milliseconds and survive browser refresh during post-race phases.
3. The exact text `you were able to success` is visible.
4. Empty Enter skips one player; repeated Enter skips both.
5. A 120-second idle timeout saves confirmed names and skips unresolved players.
6. Both name-entry and leaderboard deadlines are shown subtly.
7. Named players are stored individually and atomically in SQLite.
8. The top 10 persists across service restarts and deployments.
9. The leaderboard dismisses after 60 seconds or immediately on Space.
10. Any database failure leaves the kiosk able to continue to the next race.
