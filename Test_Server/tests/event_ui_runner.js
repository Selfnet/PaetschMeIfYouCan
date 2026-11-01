const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const [page, scenario] = process.argv.slice(2);
const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(
  path.join(root, "templates", `${page}.html`),
  "utf8",
);
const elements = new Map();
const handlers = {},
  managerHandlers = {},
  requests = [],
  timers = new Map();
let timerId = 0;
class Element {
  constructor(tag = "div", id = "") {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.children = [];
    this.dataset = {};
    this.style = { setProperty() {} };
    this.attributes = {};
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.value = "";
    this.checked = false;
    this.textContent = "";
    this.scrollTop = 0;
    this.clientHeight = 400;
    this.scrollHeight = 800;
    this.previousElementSibling = { textContent: "" };
    this.listeners = {};
    this.classes = new Set();
    this.classList = {
      toggle: (name, on) =>
        on ? this.classes.add(name) : this.classes.delete(name),
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
    };
  }
  appendChild(child) {
    child.parent = this;
    this.children.push(child);
    if (child.id) elements.set(child.id, child);
    return child;
  }
  append(...children) {
    children.forEach((child) => this.appendChild(child));
  }
  replaceChildren(...children) {
    this.children = [];
    this.append(...children);
  }
  get firstChild() {
    return this.children[0];
  }
  setAttribute(name, value) {
    this.attributes[name] = value;
  }
  addEventListener(event, callback) {
    (this.listeners[event] ||= []).push(callback);
  }
  fire(event, data = {}) {
    for (const fn of this.listeners[event] || [])
      fn({ target: this, preventDefault() {}, ...data });
  }
  focus() {
    document.activeElement = this;
  }
  showModal() {
    this.open = true;
  }
  close() {
    this.open = false;
  }
  scrollIntoView() {}
  matches(selector) {
    return (
      this.tagName === "INPUT" &&
      selector.startsWith(".name-input") &&
      !this.disabled
    );
  }
  getBoundingClientRect() {
    return { top: 100, height: 40 };
  }
  closest(selector) {
    return /input|textarea|select/.test(selector) &&
      ["INPUT", "SELECT", "TEXTAREA"].includes(this.tagName)
      ? this
      : null;
  }
  querySelectorAll(selector) {
    const all = this.children.flatMap((child) => [
      child,
      ...child.querySelectorAll("*"),
    ]);
    return all.filter(
      (child) =>
        selector === "*" ||
        (selector === "button" && child.tagName === "BUTTON") ||
        (selector.startsWith(".") && child.className === selector.slice(1)),
    );
  }
  querySelector(selector) {
    return selector === "thead"
      ? new Element("thead")
      : this.querySelectorAll(selector)[0] || null;
  }
  remove() {
    this.parent.children = this.parent.children.filter(
      (child) => child !== this,
    );
  }
}
for (const match of html.matchAll(/<(\w+)\b[^>]*\bid="([^"]+)"[^>]*>/g))
  elements.set(match[2], new Element(match[1], match[2]));
function el(id) {
  return elements.get(id);
}
const document = {
  activeElement: null,
  listeners: {},
  getElementById: el,
  createElement: (tag) => new Element(tag),
  querySelectorAll(selector) {
    if (selector === ".leaderboard-slot")
      return [el("idleLeaderboardSlot"), el("viewLeaderboardSlot")];
    if (selector.startsWith("#")) {
      const [id, child] = selector.slice(1).split(" ");
      return el(id)?.querySelectorAll(child) || [];
    }
    return [];
  },
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  },
  addEventListener(event, callback) {
    (this.listeners[event] ||= []).push(callback);
  },
};
const socketMock = {
  io: {
    on(event, fn) {
      const prior = managerHandlers[event];
      managerHandlers[event] = (...args) => {
        prior?.(...args);
        fn(...args);
      };
    },
  },
  on(event, fn) {
    const prior = handlers[event];
    handlers[event] = (...args) => {
      prior?.(...args);
      fn(...args);
    };
  },
  timeout() {
    return this;
  },
  emit(event, data, callback) {
    requests.push({ event, data, callback });
  },
};
const context = vm.createContext({
  document,
  Element,
  HTMLInputElement: Element,
  console,
  io: () => socketMock,
  performance: { now: () => 1000 },
  setTimeout(fn) {
    timers.set(++timerId, fn);
    return timerId;
  },
  clearTimeout(id) {
    timers.delete(id);
  },
  setInterval() {},
  requestAnimationFrame() {},
  fetch: () => {
    throw new Error("Unexpected HTTP command");
  },
  window: { addEventListener() {}, confirm: () => true },
  el,
  handlers,
  managerHandlers,
  requests,
  timers,
});
for (const script of html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)) {
  if (!script[1].includes("src=")) vm.runInContext(script[2], context);
  else if (script[1].includes("event-controls.js"))
    vm.runInContext(
      fs.readFileSync(path.join(root, "static/event-controls.js"), "utf8"),
      context,
    );
}
vm.runInContext(
  `
const numbered = Array.from({length: 9}, (_, i) => ({id: i + 101, slot: i + 1, name: null, starts_at: null, ends_at: null}));
const eventBoard = {id: 210, slot: null, name: "Patch night", starts_at: "2026-11-01T10:00:00+00:00", ends_at: "2026-11-01T12:00:00+00:00"};
const archive = {...eventBoard, id: 209, name: "Old <night>", ends_at: "2026-10-31T12:00:00+00:00", starts_at: "2026-10-31T10:00:00+00:00"};
function state(patch = {}) { return {revision: 1, phase: "ready", race_id: null, browse_epoch: 1,
    server_now: "2026-11-01T09:00:00+00:00",
    leaderboard_policy: {available: true, error: null, revision: 1, next: numbered[0], active_event: null, numbered},
    race_leaderboard: null, mode: {id: "full-field", label: "Full Field", description: "Patch all ports", required_port_count: 48},
    available_modes: [], players: {}, result_order: [], name_entry: {active_player: null, drafts: {}, resolved: {}},
    persistence_status: "saved", persistence_error: null, remaining_seconds: null, ...patch}; }
function key(key, patch = {}) {
    const event = {key, code: key.length === 1 && /[a-z]/i.test(key) ? "Key" + key.toUpperCase() : key === " " ? "Space" : key,
        target: document.activeElement || el("game-title"), preventDefault() { this.prevented = true; }, ...patch};
    for (const handler of document.listeners.keydown || []) handler(event);
    if (!event.prevented && ['Enter', ' '].includes(key) && event.target.tagName === 'BUTTON') event.target.fire('click');
    return Boolean(event.prevented);
}
function lastRequest(event) { return requests.filter(r => r.event === event).at(-1); }
function reply(event, ack, error = null) { lastRequest(event).callback(error, ack); }
function output(value) { console.log(JSON.stringify(value)); }
`,
  context,
);
vm.runInContext(scenario, context);
