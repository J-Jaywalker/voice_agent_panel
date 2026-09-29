/*
 * The client.
 *
 * Three jobs: fit the stage to whatever the wall turns out to be, keep the DOM
 * in step with the snapshots arriving over the socket, and run one animation
 * loop for all three orbs.
 *
 * The socket is treated as unreliable on purpose. This runs on a venue machine
 * for one night; something will be unplugged, something will be refreshed, and
 * a page that needs a clean restart to recover is a page that will be black at
 * the wrong moment. So: reconnect forever, repaint from a full snapshot rather
 * than a diff, and keep the orbs animating while disconnected instead of
 * freezing them mid-motion.
 */

import { Orb } from "./orb.js";

// The stage canvas, 16:9. Mirrors --stage-w / --stage-h in tokens.css; the two
// have to agree or fitStage() letterboxes against the wrong shape.
const STAGE_W = 3840;
const STAGE_H = 2160;
// The orb's CSS size, mirroring `.orb canvas` in wall.css. Unchanged by the
// move to 16:9 — the agent row is 1280 tall and the vertical budget still
// spends 720 of it here.
const ORB_CSS = 720;

const RECONNECT_MIN_MS = 400;
const RECONNECT_MAX_MS = 2500;

// Eyebrow copy. Lower case in the markup, upper-cased by CSS, because a wall
// that shouts in the source is a wall nobody can re-word without shouting.
const LABELS = {
  idle: "",
  invited: "invited",
  thinking: "thinking",
  ducked: "yielding",
  speaking: "speaking",
};

// The moderator channel's key in the levels message. Matches `panel_core.HUMAN`.
const HUMAN = "human";

// A stage mic into a PA runs hotter than the TTS does, and the dot only has
// to register presence rather than measure anything. Same `?gain=` caveat as
// the orbs: this is a rehearsal dial.
const HUMAN_FULL_SCALE = 0.25;

const params = new URLSearchParams(location.search);
const GAIN = Number(params.get("gain")) || 1;

const stage = document.getElementById("stage");
const laneHost = document.querySelector("[data-role=agents]");
const template = document.querySelector("[data-role=agent-template]");
const offline = document.querySelector("[data-role=offline]");
const humanDot = document.querySelector("[data-role=human-dot]");
const transcriptLabel = document.querySelector("[data-role=transcript-label]");
const feed = document.querySelector("[data-role=lines]");
const partials = document.querySelector("[data-role=partials]");
const cue = document.querySelector("[data-role=cue]");

/** agent id -> { lane, orb, status, state } */
const agents = new Map();
/** Absolute index of the oldest transcript line currently in the DOM. */
let renderedFrom = -1;
/** speaker -> the <p> currently showing that voice's in-progress line. */
const partialNodes = new Map();

// ── Stage fit ─────────────────────────────────────────────────────────────

let pixelScale = 1;

/*
 * The wall is 16:9. The browser window, at load-in, will be whatever someone
 * dragged it to, and the display may report a resolution nobody predicted.
 * Laying out at a fixed 3840x2160 and scaling once means every dimension in
 * the stylesheet keeps meaning what it says — 1280px is a third of the width
 * whatever the hardware does — and the stage letterboxes rather than crops.
 */
function fitStage() {
  const scale = Math.min(window.innerWidth / STAGE_W, window.innerHeight / STAGE_H);
  stage.style.setProperty("--fit", String(scale));
  pixelScale = (window.devicePixelRatio || 1) * scale;
  for (const { orb } of agents.values()) orb.resize(ORB_CSS, pixelScale);
}

window.addEventListener("resize", fitStage);

// ── Lanes ─────────────────────────────────────────────────────────────────

/** Build the three agent lanes. Runs once, on the first snapshot. */
function buildLanes(list) {
  laneHost.replaceChildren();
  agents.clear();
  // The cast is what an accent is read off, so any partial built against the
  // previous one is holding a stale colour. Cheaper to rebuild them than to
  // re-attribute in place, and this runs once per connection.
  partials.replaceChildren();
  partialNodes.clear();
  for (const agent of list) {
    const lane = template.content.firstElementChild.cloneNode(true);
    lane.dataset.accent = agent.accent;
    lane.querySelector("[data-role=name]").textContent = agent.name;
    lane.querySelector("[data-role=role]").textContent = agent.role;
    lane.querySelector("[data-role=employer]").textContent = agent.employer;
    laneHost.appendChild(lane);

    const canvas = lane.querySelector("[data-role=canvas]");
    agents.set(agent.id, {
      lane,
      orb: new Orb(canvas, lane, GAIN),
      status: lane.querySelector("[data-role=status]"),
      state: "idle",
      // Carried for the transcript band: a line attributed to this agent
      // needs its name and colour, and re-deriving both from the DOM on
      // every appended line is needless when they arrived on this same
      // snapshot payload.
      name: agent.name,
      accent: agent.accent,
    });
  }
  fitStage();
}

/**
 * What the orb should actually show.
 *
 * `state` and `invited` are independent on the wire — an agent can hold the
 * invitation and be idle, which is the beat between James naming someone and
 * that person's first word, and it is worth seeing.
 *
 * Precedence, strongest first: speaking, ducked, invited, thinking, idle.
 * Audible always wins — whoever is on the PA is what the wall is about. And
 * invited beats thinking, which is not obvious: after any question every
 * agent is thinking, so "thinking" is nearly free information, while "James
 * named this one" is the fact the audience needs to follow the floor.
 */
function visualState(agent) {
  if (agent.state === "speaking" || agent.state === "ducked") return agent.state;
  if (agent.invited) return "invited";
  if (agent.state === "thinking") return "thinking";
  return "idle";
}

function labelFor(agent, visual) {
  if (visual === "speaking" || visual === "ducked") return LABELS[visual];
  if (agent.hand !== null && agent.hand !== undefined) return "wants in";
  return LABELS[visual];
}

// ── Transcript ────────────────────────────────────────────────────────────

/*
 * Appends, rather than rebuilding.
 *
 * The lines are a bounded window, so a naive re-render would re-run the
 * entrance animation on every line every time anything changed anywhere on
 * the wall. `from` is the absolute index of the first line in the window,
 * which is enough to tell "two new lines" from "the same lines again".
 */
function renderLines(lines, from) {
  const renderedTo = renderedFrom + feed.childElementCount;

  // No overlap with what is on screen: a fresh connection, or a gap big
  // enough that reconciling would be guesswork. Repaint the window.
  if (renderedFrom < 0 || from >= renderedTo) {
    feed.replaceChildren(...lines.map(lineNode));
    renderedFrom = from;
    return;
  }

  for (let i = renderedTo - from; i < lines.length; i++) {
    feed.appendChild(lineNode(lines[i]));
  }
  while (feed.childElementCount > lines.length) {
    feed.removeChild(feed.firstElementChild);
    renderedFrom += 1;
  }
}

/**
 * Attribute a <p> to a speaker: name tag ahead of the text, accent on the tag.
 *
 * Four voices sharing one band are only tellable apart by who is named on
 * them once they are off their own lane, and the moderator is one of the four.
 * Only the three agents are accent-coloured — James does not hold one of the
 * three accent seats, so his tag takes the band's quiet default rather than
 * borrowing a colour that already means a specific agent.
 *
 * Shared by finalised lines and in-progress ones, because both now come from
 * the same place: a real transcription session, one per voice.
 */
function attribute(p, speaker) {
  const tag = document.createElement("span");
  tag.className = "line__speaker";
  if (speaker === HUMAN) {
    // Hardcoded, like `PanelSTT({"James": "human"})` in panel_runtime. The
    // moderator is not cast data — there is no persona behind him.
    tag.textContent = "James";
  } else {
    const view = agents.get(speaker);
    // `data-accent` drives colour in wall.css off the same `--accent-*` tokens
    // the agent's own lane reads — one set of tokens naming one agent,
    // wherever on the stage they show up.
    p.dataset.accent = view ? view.accent : "";
    tag.textContent = view ? view.name : speaker;
  }
  p.appendChild(tag);
  return tag;
}

/**
 * One finalised line in the band. `line.speaker` is `HUMAN` or an agent id —
 * all four share one feed, in the order they were said (see `TranscriptLine`
 * in `wall.py`).
 */
function lineNode(line) {
  const p = document.createElement("p");
  p.className = "line";
  attribute(p, line.speaker);
  p.append(" " + line.text);
  return p;
}

/**
 * The in-progress lines, one per voice still mid-sentence.
 *
 * Reconciled by speaker rather than rebuilt, and that is not an
 * optimisation: a partial is replaced on every word, so recreating the node
 * would re-run `line-in` forty times a sentence and the band would strobe.
 * Only the text after the name tag is rewritten.
 */
function renderPartials(rows) {
  const seen = new Set();
  for (const row of rows) {
    seen.add(row.speaker);
    let p = partialNodes.get(row.speaker);
    if (!p) {
      p = document.createElement("p");
      p.className = "line line--partial";
      attribute(p, row.speaker);
      p.appendChild(document.createTextNode(""));
      partialNodes.set(row.speaker, p);
    }
    p.lastChild.nodeValue = " " + row.text;
    // Re-appended every pass, so the server's ordering (agents in stage
    // order, James last) survives a voice dropping out and coming back.
    partials.appendChild(p);
  }
  for (const [speaker, node] of partialNodes) {
    if (seen.has(speaker)) continue;
    node.remove();
    partialNodes.delete(speaker);
  }
}

// ── Snapshots ─────────────────────────────────────────────────────────────

function applyState(message) {
  if (agents.size !== message.agents.length) buildLanes(message.agents);

  for (const agent of message.agents) {
    const view = agents.get(agent.id);
    if (!view) continue;
    const visual = visualState(agent);
    view.lane.dataset.state = visual;
    view.status.textContent = labelFor(agent, visual);
    view.orb.setState(visual);
  }

  stage.dataset.killed = String(Boolean(message.killed));

  humanDot.dataset.live = String(Boolean(message.human_speaking));
  transcriptLabel.textContent = message.human_speaking ? "Moderator — live" : "Moderator";

  renderLines(message.lines, message.line_seq - message.lines.length);
  renderPartials(message.partials || []);

  cue.dataset.on = String(Boolean(message.cue));
  cue.textContent = message.cue ? "over to James" : "";
}

// James's mic, smoothed in the frame loop below. Not an orb — he is a person
// standing in the room and does not need one — but the dot beside "moderator"
// following his voice is what tells the audience the band along the bottom is
// live rather than a caption track running on a delay.
let humanTarget = 0;
let humanLevel = 0;

function applyLevels(message) {
  for (const [id, value] of Object.entries(message.v)) {
    if (id === HUMAN) {
      humanTarget = Math.min(1, value / HUMAN_FULL_SCALE);
      continue;
    }
    const view = agents.get(id);
    if (view) view.orb.setLevel(value);
  }
}

// ── Socket ────────────────────────────────────────────────────────────────

let backoff = RECONNECT_MIN_MS;

function connect() {
  const socket = new WebSocket(`ws://${location.host}/ws`);

  socket.addEventListener("open", () => {
    backoff = RECONNECT_MIN_MS;
    offline.dataset.on = "false";
  });

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "levels") applyLevels(message);
    else if (message.type === "state") applyState(message);
  });

  socket.addEventListener("close", () => {
    offline.dataset.on = "true";
    // Every orb settles to idle rather than holding whatever radius it had
    // when the socket went. A frozen waveform is indistinguishable from an
    // agent who is still talking.
    for (const { orb } of agents.values()) {
      orb.setState("idle");
      orb.setLevel(0);
    }
    setTimeout(connect, backoff);
    backoff = Math.min(RECONNECT_MAX_MS, backoff * 1.7);
  });

  socket.addEventListener("error", () => socket.close());
}

// ── Frame loop ────────────────────────────────────────────────────────────

let last = performance.now();
const started = last;

function frame(now) {
  // Clamped: a backgrounded tab or a GC pause otherwise delivers a dt of
  // several seconds and every filter in the orb snaps to its target at once.
  const dt = Math.min(0.05, (now - last) / 1000);
  last = now;
  const t = (now - started) / 1000;
  for (const { orb } of agents.values()) orb.frame(dt, t);

  // Same filter shape as the orbs, one tenth of the code: the dot swells and
  // brightens with James's voice instead of blinking on a VAD boolean.
  humanLevel += (humanTarget - humanLevel) * (1 - Math.exp(-dt / 0.12));
  humanDot.style.transform = `scale(${(1 + 0.55 * humanLevel).toFixed(3)})`;
  humanDot.style.opacity = (0.45 + 0.55 * humanLevel).toFixed(3);

  requestAnimationFrame(frame);
}

fitStage();
offline.dataset.on = "true";
connect();
requestAnimationFrame(frame);
