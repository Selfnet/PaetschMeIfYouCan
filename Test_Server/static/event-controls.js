let eventControlState = null;
let eventCatalog = [];
let eventCatalogRevision = null;
let eventEditRevision = null;
let editingEvent = null;
let pendingEventCommand = null;
let eventListGeneration = 0;

function eventInputTime(value) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Berlin",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(value));
  const fields = Object.fromEntries(
    parts.map((part) => [part.type, part.value]),
  );
  return `${fields.year}-${fields.month}-${fields.day}T${fields.hour}:${fields.minute}`;
}

function eventStatus(board) {
  if (eventControlState?.leaderboard_policy?.active_event?.id === board.id)
    return "Active";
  return Date.parse(board.ends_at) <= Date.parse(eventControlState?.server_now)
    ? "Past"
    : "Upcoming";
}

function showEventError(message) {
  const error = document.getElementById("eventError");
  error.textContent = message || "";
  error.hidden = !message;
}

function renderEventForm() {
  const busy = pendingEventCommand !== null;
  const unavailable = !eventControlState?.leaderboard_policy?.available;
  const status = editingEvent ? eventStatus(editingEvent) : null;
  document.getElementById("eventFormTitle").textContent = editingEvent
    ? `Edit event: ${editingEvent.name}`
    : "New event";
  document.getElementById("saveEvent").textContent = editingEvent
    ? "Save changes"
    : "Create event";
  for (const id of [
    "saveEvent",
    "newEvent",
    "reloadEvents",
    "endEvent",
    "cancelEvent",
    "eventName",
    "eventEnd",
  ]) {
    document.getElementById(id).disabled = busy || unavailable;
  }
  document.getElementById("saveEvent").disabled ||= eventEditRevision === null;
  document.getElementById("retryEventStorage").disabled =
    busy || eventControlState === null;
  document.getElementById("eventStartNow").disabled =
    busy || unavailable || Boolean(editingEvent);
  document.getElementById("eventStart").disabled =
    busy ||
    unavailable ||
    document.getElementById("eventStartNow").checked ||
    Boolean(status && status !== "Upcoming");
  document.getElementById("eventStart").required =
    !editingEvent && !document.getElementById("eventStartNow").checked;
  document.getElementById("eventEnd").disabled ||= status === "Past";
  document.getElementById("endEvent").hidden = status !== "Active";
  document.getElementById("cancelEvent").hidden = status !== "Upcoming";
  for (const button of document.querySelectorAll("#eventList button"))
    button.disabled = busy || unavailable;
  const start = document.getElementById("eventStartNow").checked
    ? "Now (server time)"
    : document.getElementById("eventStart").value.replace("T", " ");
  const end = document.getElementById("eventEnd").value.replace("T", " ");
  document.getElementById("eventRange").textContent = end
    ? `${start || "Choose start"} - ${end} (Europe/Berlin)`
    : "";
}

function editEvent(board = null) {
  if (pendingEventCommand !== null) return;
  editingEvent = board;
  eventEditRevision = eventCatalogRevision;
  document.getElementById("eventName").value = board?.name || "";
  document.getElementById("eventStartNow").checked = !board;
  document.getElementById("eventStart").value = board
    ? eventInputTime(board.starts_at)
    : "";
  document.getElementById("eventEnd").value = board
    ? eventInputTime(board.ends_at)
    : "";
  showEventError(null);
  renderEventForm();
}

function renderEventList() {
  const list = document.getElementById("eventList");
  list.replaceChildren();
  for (const board of eventCatalog) {
    const button = document.createElement("button");
    button.type = "button";
    const name = document.createElement("strong");
    name.textContent = `${board.name} - ${eventStatus(board)}`;
    const range = document.createElement("span");
    range.textContent = `${eventInputTime(board.starts_at).replace("T", " ")} - ${eventInputTime(board.ends_at).replace("T", " ")} (Europe/Berlin)`;
    button.append(name, range);
    button.addEventListener("click", () => {
      editEvent(board);
      document.getElementById("eventName").focus();
    });
    list.appendChild(button);
  }
  renderEventForm();
}

function updateEventStatuses() {
  eventCatalog.forEach((board, index) => {
    document.getElementById("eventList").children[
      index
    ].children[0].textContent = `${board.name} - ${eventStatus(board)}`;
  });
}

function loadEvents(reloadEditor = false) {
  if (!eventControlState?.leaderboard_policy?.available) return;
  const generation = ++eventListGeneration;
  const status = document.getElementById("eventListStatus");
  status.textContent = "Loading events...";
  socket
    .timeout(10_000)
    .emit("list_events", { past_only: false }, (failure, ack) => {
      if (generation !== eventListGeneration || !eventControlState) return;
      if (failure || !ack?.accepted) {
        status.textContent = failure
          ? "Events did not arrive. Reload to try again."
          : ack?.error || "Could not load events";
        return;
      }
      eventCatalog = ack.events;
      eventCatalogRevision = ack.revision;
      if (eventEditRevision === null) eventEditRevision = ack.revision;
      status.textContent = eventCatalog.length ? "" : "No events yet";
      renderEventList();
      if (reloadEditor)
        editEvent(
          eventCatalog.find((board) => board.id === editingEvent?.id) || null,
        );
    });
}

function sendEventCommand(command, payload) {
  if (!eventControlState || pendingEventCommand !== null) return;
  const attempt = {};
  pendingEventCommand = attempt;
  showEventError(null);
  renderEventForm();
  socket.timeout(10_000).emit(command, payload, (failure, ack) => {
    if (pendingEventCommand !== attempt) return;
    pendingEventCommand = null;
    if (failure || !ack?.accepted) {
      showEventError(
        failure
          ? "Could not confirm the change. Reload events before trying again."
          : ack?.error || "Event change could not be saved",
      );
    } else {
      if (command !== "retry_leaderboard_storage") {
        eventCatalogRevision = ack.revision;
        editEvent();
      }
      loadEvents();
    }
    renderEventForm();
  });
}

function saveEventForm(event) {
  event.preventDefault();
  if (
    pendingEventCommand !== null ||
    !eventControlState?.leaderboard_policy?.available ||
    eventEditRevision === null
  )
    return;
  const name = document.getElementById("eventName").value.trim();
  if (!name || Array.from(name).length > 60) {
    showEventError("Name must contain 1-60 characters.");
    return;
  }
  const startNow =
    !editingEvent && document.getElementById("eventStartNow").checked;
  const data = { name, start_now: startNow };
  if (editingEvent) data.id = editingEvent.id;
  // Preserve UTC seconds and DST offsets when a field was not edited.
  for (const [field, id] of [
    ["starts_at", "eventStart"],
    ["ends_at", "eventEnd"],
  ]) {
    if (field === "starts_at" && startNow) continue;
    const value = document.getElementById(id).value;
    data[field] =
      editingEvent && value === eventInputTime(editingEvent[field])
        ? editingEvent[field]
        : value;
  }
  sendEventCommand("save_event", { event: data, revision: eventEditRevision });
}

function mutateEvent(command) {
  if (
    !editingEvent ||
    pendingEventCommand !== null ||
    !eventControlState?.leaderboard_policy?.available
  )
    return;
  const message =
    command === "end_event"
      ? `End "${editingEvent.name}" now? Races already started keep this leaderboard.`
      : `Cancel "${editingEvent.name}"? Only a future empty event can be cancelled.`;
  if (window.confirm(message))
    sendEventCommand(command, {
      id: editingEvent.id,
      revision: eventEditRevision,
    });
}

function resetEventConnection() {
  eventControlState = null;
  pendingEventCommand = null;
  eventListGeneration += 1;
  document.getElementById("eventDestination").textContent = "Connecting";
  renderEventForm();
}

socket.io.on("open", resetEventConnection);
socket.on("disconnect", resetEventConnection);
socket.on("game_state", (state) => {
  if (eventControlState && state.revision < eventControlState.revision) return;
  const previous = eventControlState?.leaderboard_policy;
  const previousStatuses = eventCatalog.map(eventStatus);
  eventControlState = state;
  const policy = state.leaderboard_policy;
  const board = policy?.next;
  document.getElementById("eventDestination").textContent = policy?.available
    ? `Next race: ${board.slot ? `Slot ${board.slot}` : board.name}${policy.active_event ? " (event locked)" : ""}`
    : policy?.error || "Leaderboard routing unavailable";
  if (
    policy?.available &&
    (!previous?.available || previous.revision !== policy.revision)
  )
    loadEvents();
  if (
    eventCatalog.some(
      (board, index) => eventStatus(board) !== previousStatuses[index],
    )
  )
    updateEventStatuses();
  renderEventForm();
});
document.getElementById("eventForm").addEventListener("submit", saveEventForm);
document
  .getElementById("newEvent")
  .addEventListener("click", () => editEvent());
document
  .getElementById("reloadEvents")
  .addEventListener("click", () => loadEvents(true));
document
  .getElementById("retryEventStorage")
  .addEventListener("click", () =>
    sendEventCommand("retry_leaderboard_storage", {}),
  );
document
  .getElementById("endEvent")
  .addEventListener("click", () => mutateEvent("end_event"));
document
  .getElementById("cancelEvent")
  .addEventListener("click", () => mutateEvent("cancel_event"));
for (const id of ["eventStartNow", "eventStart", "eventEnd"]) {
  document.getElementById(id).addEventListener("change", renderEventForm);
}
