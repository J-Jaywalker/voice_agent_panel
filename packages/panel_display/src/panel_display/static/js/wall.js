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

// The stage canvas, 8:3. Mirrors --stage-w / --stage-h in tokens.css; the two
// have to agree or fitStage() letterboxes against the wrong shape.
const STAGE_W = 3840;
const STAGE_H = 1440;
// The orb's CSS size, mirroring `.orb canvas` in wall.css. The agent lane is
// 1440 tall and the vertical budget spends 720 of it here.
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
/** speaker -> the bubble row currently showing that voice's in-progress line. */
const partialNodes = new Map();

// ── Stage fit ─────────────────────────────────────────────────────────────

let pixelScale = 1;

/*
 * The wall is 8:3. The browser window, at load-in, will be whatever someone
 * dragged it to, and the LED processor may report a resolution nobody
 * predicted. Laying out at a fixed 3840x1440 and scaling once means every
 * dimension in the stylesheet keeps meaning what it says — 960px is 3m of
 * wall whatever the hardware does — and the stage letterboxes rather than
 * crops.
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
 * invitation and be idle, which is the beat between Ricky naming someone and
 * that person's first word, and it is worth seeing.
 *
 * Precedence, strongest first: speaking, ducked, invited, thinking, idle.
 * Audible always wins — whoever is on the PA is what the wall is about. And
 * invited beats thinking, which is not obvious: after any question every
 * agent is thinking, so "thinking" is nearly free information, while "Ricky
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

/**
 * What the band calls a speaker. `HUMAN` is hardcoded, like
 * `PanelSTT({"ricky": "human"})` in panel_runtime: the moderator is not cast
 * data, there is no persona behind him.
 */
function nameFor(speaker) {
  if (speaker === HUMAN) return "Ricky";
  const view = agents.get(speaker);
  return view ? view.name : speaker;
}

/**
 * Which side of the band a bubble grows from — moderator left, agents right.
 *
 * Read off the speaker and nothing else, so a voice is always on the same
 * side of the wall. It is the person asking the panel things on one side and
 * the panel on the other, which is the one distinction worth spending the
 * only piece of two-sided geometry the band has.
 */
function sideFor(speaker) {
  return speaker === HUMAN ? "left" : "right";
}

/**
 * One utterance: a name tag over an accent-tinted bubble.
 *
 * `cont` is "the voice before this one was the same voice", which drops the
 * tag and tightens the gap (see `[data-cont]` in wall.css). An agent's turn
 * arrives as a run of sentences, so repeating their name under itself is both
 * noise and 28px spent on every sentence of a column that is narrow enough
 * now to hold only a handful of bubbles at once.
 */
function bubbleRow(speaker, text, cont) {
  const row = document.createElement("div");
  row.className = "bubble-row";
  row.dataset.speaker = speaker;
  row.dataset.side = sideFor(speaker);
  row.dataset.cont = String(cont);
  // `data-accent` drives the fill and the tag's colour in wall.css off the
  // same `--accent-*` tokens the agent's own lane reads — one set of tokens
  // naming one agent, wherever on the stage they show up. Ricky holds no
  // accent seat, so his bubble is left on the band's default.
  if (speaker !== HUMAN) {
    const view = agents.get(speaker);
    row.dataset.accent = view ? view.accent : "";
  }

  if (!cont) {
    const tag = document.createElement("span");
    tag.className = "bubble__tag";
    tag.textContent = nameFor(speaker);
    row.appendChild(tag);
  }

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  row.appendChild(bubble);
  return row;
}

/** The bubble itself, for rewriting a partial's text in place. */
function bubbleOf(row) {
  return row.lastElementChild;
}

/*
 * Appends, rather than rebuilding.
 *
 * The lines are a bounded window, so a naive re-render would re-run the
 * entrance animation on every bubble every time anything changed anywhere on
 * the wall. `from` is the absolute index of the first line in the window,
 * which is enough to tell "two new lines" from "the same lines again".
 */
function renderLines(lines, from) {
  const renderedTo = renderedFrom + feed.childElementCount;
  // Continuation is a fact about the line before this one, which is in the
  // same array either way. The window's oldest line has no predecessor here
  // and so keeps its tag — it is under the mask's fade by then.
  const cont = (i) => i > 0 && lines[i - 1].speaker === lines[i].speaker;

  // No overlap with what is on screen: a fresh connection, or a gap big
  // enough that reconciling would be guesswork. Repaint the window.
  if (renderedFrom < 0 || from >= renderedTo) {
    feed.replaceChildren(...lines.map((line, i) => bubbleRow(line.speaker, line.text, cont(i))));
    renderedFrom = from;
    return;
  }

  // `renderedTo > from` on this branch, so the start index is never 0 and
  // `lines[i - 1]` is always the line already on screen above it.
  for (let i = renderedTo - from; i < lines.length; i++) {
    feed.appendChild(bubbleRow(lines[i].speaker, lines[i].text, cont(i)));
  }
  while (feed.childElementCount > lines.length) {
    feed.removeChild(feed.firstElementChild);
    renderedFrom += 1;
  }
}

/**
 * The in-progress bubbles, one per voice still mid-sentence.
 *
 * Reconciled by speaker rather than rebuilt, and that is not an optimisation:
 * a partial is replaced on every word, so recreating the node would re-run
 * `line-in` forty times a sentence and the band would strobe. Only the text
 * inside the bubble is rewritten; the bubble grows with it, which is the whole
 * reason the band is drawn in bubbles.
 *
 * The one case that does rebuild is a change of continuation — the finalised
 * line above landed and the name tag now has to appear or disappear. That is
 * once per sentence at most, not once per word.
 */
function renderPartials(rows) {
  const seen = new Set();
  const above = feed.lastElementChild;
  rows.forEach((row, i) => {
    seen.add(row.speaker);
    // Only the first in-progress bubble sits against the finalised feed; a
    // second one is the barge-in case and follows the first, not the feed.
    const cont = i === 0 && above !== null && above.dataset.speaker === row.speaker;
    let node = partialNodes.get(row.speaker);
    if (!node || node.dataset.cont !== String(cont)) {
      if (node) node.remove();
      node = bubbleRow(row.speaker, "", cont);
      node.dataset.partial = "true";
      partialNodes.set(row.speaker, node);
    }
    bubbleOf(node).textContent = row.text;
    // Re-appended every pass, so the server's ordering (agents in stage
    // order, Ricky last) survives a voice dropping out and coming back.
    partials.appendChild(node);
  });
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
  transcriptLabel.textContent = message.human_speaking ? "Ricky — live" : "Ricky";

  renderLines(message.lines, message.line_seq - message.lines.length);
  renderPartials(message.partials || []);

  cue.dataset.on = String(Boolean(message.cue));
  cue.textContent = message.cue ? "over to Ricky" : "";
}

// Ricky's mic, smoothed in the frame loop below. Not an orb — he is a person
// standing in the room and does not need one — but the dot beside "moderator"
// following his voice is what tells the audience the transcript column is
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
  // Optional on the wire — see `set_levels` in server.py. An agent with no
  // spectrum in this frame is left holding the one before it, which the orb's
  // own release filter then takes down; zeroing here instead would make the
  // corona flicker on any frame the server chose not to spend bytes on.
  if (!message.b) return;
  for (const [id, bands] of Object.entries(message.b)) {
    const view = agents.get(id);
    if (view) view.orb.setBands(bands);
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
      orb.setBands(null);
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
  // brightens with Ricky's voice instead of blinking on a VAD boolean.
  humanLevel += (humanTarget - humanLevel) * (1 - Math.exp(-dt / 0.12));
  humanDot.style.transform = `scale(${(1 + 0.55 * humanLevel).toFixed(3)})`;
  humanDot.style.opacity = (0.45 + 0.55 * humanLevel).toFixed(3);

  requestAnimationFrame(frame);
}

fitStage();
offline.dataset.on = "true";
connect();
requestAnimationFrame(frame);
