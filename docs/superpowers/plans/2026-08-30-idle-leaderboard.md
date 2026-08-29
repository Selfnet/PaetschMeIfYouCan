# Idle Leaderboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the existing top 10 while idle and display each entry's existing `created_at` timestamp without changing race-start behavior or the database schema.

**Architecture:** Keep idle leaderboard visibility in the browser so it does not become a race phase. Load rows through a targeted Socket.IO event, correlate asynchronous requests with request IDs, and reuse the existing post-race table and `LeaderboardStore.top_entries()` model.

**Tech Stack:** Python 3.11+, Flask, Flask-SocketIO, SQLite, vanilla HTML/CSS/JavaScript, pytest, and agent-browser.

## Global Constraints

- The behavior source is `docs/superpowers/specs/2026-08-30-idle-leaderboard-design.md`.
- Do not change the SQLite schema or existing `created_at` model.
- Do not gate starting on cable state.
- Idle leaderboard access is allowed only in `ready` and `stopped`.
- Idle visibility must not mutate or broadcast race state.
- A cancelled request must not reopen the view when its response arrives.
- Idle and post-race leaderboard views dismiss on `L`/`Escape`; post-race Space dismissal remains supported.
- Idle views automatically dismiss after 60 seconds.
- Display `created_at` in local time as `DD.MM.YYYY HH:MM`.
- Keep ranking, top-10 truncation, current-race highlighting, and tie ordering unchanged.
- Tests must not access serial hardware, the production database, or wall-clock sleeps.
- The final UI must fit 1920x1080 fullscreen without scrolling or overlap.
- Do not deploy; create one local commit after verification.

## File Map

- Modify `Test_Server/app.py`: reusable leaderboard loading and targeted request event with request ID echo.
- Modify `Test_Server/templates/index.html`: integrated control, timestamp column, idle timer, request cancellation, focus, and keyboard handling.
- Modify `Test_Server/tests/test_app.py`: unchanged start behavior and targeted Socket.IO contracts.
- Modify `Test_Server/tests/browser_harness.py`: deterministic rows for visual acceptance.

---

### Task 1: Targeted Idle Leaderboard API

**Files:**
- Modify: `Test_Server/tests/test_app.py`
- Modify: `Test_Server/app.py`

**Interfaces:**
- Consumes: `request_leaderboard {request_id: object}`.
- Produces: targeted `leaderboard_snapshot {request_id, rows, error}`.
- Allows requests only when phase is `ready` or `stopped`.

- [ ] **Step 1: Write targeted event tests**

Insert a known row, connect two clients, emit `request_leaderboard` with `{"request_id": 7}`, and assert only the requester receives rows with the same request ID and `created_at`. Start a race and assert the request is ignored. Make `top_entries()` raise `sqlite3.OperationalError` and assert a targeted non-fatal error with the same request ID.

- [ ] **Step 2: Run the tests to verify failure**

Run: `uv run pytest tests/test_app.py -k 'idle_leaderboard' -q`

Expected: FAIL until the event exists and echoes the correlation value.

- [ ] **Step 3: Implement reusable loading and targeted response**

Add `GameRuntime.load_leaderboard()` returning `(rows, error)`. Register `request_leaderboard`, recheck phase, extract `request_id` from a dictionary payload, and emit `leaderboard_snapshot` with `to=request.sid`.

- [ ] **Step 4: Run integration tests**

Run: `uv run pytest tests/test_app.py -q`

Expected: all app tests pass and the observer receives no snapshot.

---

### Task 2: Idle View and Created Timestamp

**Files:**
- Modify: `Test_Server/tests/test_app.py`
- Modify: `Test_Server/templates/index.html`
- Modify: `Test_Server/tests/browser_harness.py`

**Interfaces:**
- Consumes: `leaderboard_snapshot.rows[].created_at`.
- Produces: a visible `#leaderboardButton` below `.players` in `ready` and `stopped`.
- Produces: `toggleIdleLeaderboard()`, `cancelIdleLeaderboardRequest()`, `closeIdleLeaderboard()`, and `formatCreatedAt()`.

- [ ] **Step 1: Extend the HTML contract test**

Assert the template contains the button, `Set at`, request/snapshot event names, request ID correlation, a 60,000ms deadline, `KeyL`, `Escape`, the four-column empty state, `formatCreatedAt(row.created_at)`, and focus transfer.

- [ ] **Step 2: Run the template test to verify failure**

Run: `uv run pytest tests/test_app.py::test_index_contains_all_phase_views_and_exact_copy -q`

Expected: FAIL until the idle controls and timestamp rendering exist.

- [ ] **Step 3: Implement the shared table and compact control**

Add the footer button under `.players`, a fourth `Set at` header, local timestamp formatting, and a four-column `No records yet` row. Keep the footer under 56px at 1080p.

- [ ] **Step 4: Implement correlated request and timer ownership**

Increment a client request counter for each open request and retain the pending ID. `L` or `Escape` clears the pending ID before a response. Open only when a response ID equals the pending ID. On open, start a fresh 60-second timeout and one-second countdown interval. Clear both on close or active phase change.

- [ ] **Step 5: Implement focus and keyboard behavior**

Make the leaderboard heading programmatically focusable, focus it after opening, and restore the leaderboard button on idle close. Handle `KeyL` before Space, preserve name-entry typing, preserve post-race Space, and ignore Space while the idle view is open.

- [ ] **Step 6: Preserve start behavior explicitly**

Parameterize `/start-clock` and `/space-clock` with cached cell states `0`, `1`, and `2`; all cases must return `Clock started` and enter `racing`.

- [ ] **Step 7: Run all tests and browser acceptance**

Run `uv run pytest -q`, then verify ready and idle views at 1920x1080 with agent-browser. Exercise button, `L`, `Escape`, delayed-response cancellation, focus restoration, Space suppression, timestamp display, viewport dimensions, and console errors.

---

### Task 3: Review and Commit

**Files:**
- Review all modified and new files.

- [ ] **Step 1: Run complete local verification**

Run `uv run ruff check .`, `uv run pytest -q`, `just check`, and `git diff --check`.

- [ ] **Step 2: Dispatch an independent review**

Review targeted delivery, request cancellation, timer cleanup, phase interactions, unchanged start behavior, focus, and 1080p layout. Resolve substantive findings and rerun affected checks.

- [ ] **Step 3: Evaluate durable documentation**

Use the `update-docs` workflow and update project operator notes only if the idle controls need durable documentation beyond the design.

- [ ] **Step 4: Commit locally**

Inspect status, diff, and recent log. Stage only intended source, tests, and Superpowers docs. Exclude caches and secrets. Create one concise conventional commit and do not push or deploy.
