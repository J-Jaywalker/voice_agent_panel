/*
 * One agent's orb.
 *
 * A Speechmatics mark seated on a disc, ringed by a corona of radial bars
 * whose lengths are that agent's own audio envelope — so the thing moving on
 * the wall is the thing coming out of the PA rather than a decoration timed to
 * look like it.
 *
 *
 * ── Why bars and not a blob ──────────────────────────────────────────────
 *
 * The first version drew the envelope as a single closed curve: one organic
 * outline that swelled and rippled. It was correct and it read as a lava lamp.
 * Two reasons it had to go, and neither is taste:
 *
 *   1. The brand is sharp. "No 3D, no drop shadows, no rounded bar ends, no
 *      gradients beyond fill-under" — a soft amoeba on a 12m wall is the
 *      "cinematic dark UI" the guidelines rule out, in light mode.
 *   2. A smooth outline has no internal landmarks, so at 20m its amplitude is
 *      guesswork. Discrete ticks give the eye something to count: the corona
 *      is visibly two ticks deep or visibly eight, from anywhere in the room.
 *
 * What survived intact is the mapping, which was the good idea in the old
 * version and is unchanged here. The shape is a *history*, not a spectrum.
 * Every frame pushes the current envelope into a ring buffer, and the buffer
 * is mapped to angle outward from twelve o'clock down both sides — so a
 * syllable appears at the top and travels down and away over the following
 * second and a half, symmetrically. Mirroring is what makes it seamless: the
 * newest sample meets itself at the top and the oldest at the bottom, so there
 * is no discontinuity to hide. Nothing here is an FFT and nothing here
 * pretends to be; it is one number over time, drawn as a clock.
 *
 * Everything that reads as "smooth" is one of three things, and none of them
 * is an easing curve:
 *
 *   1. Separate attack and release on the envelope. A voice rises fast and
 *      falls slowly; equal coefficients read as a level meter.
 *   2. Frame-rate-independent filtering (`1 - exp(-dt/tau)`), so a dropped
 *      frame slows the motion rather than jumping it.
 *   3. Colour interpolated as a continuous quantity. The orb does not switch
 *      to its speaking colour, it *arrives* at it over ~300ms.
 *
 *
 * ── The three zones ──────────────────────────────────────────────────────
 *
 * Reading outward, and the separation is the whole composition:
 *
 *   centre     the Speechmatics mark. Fixed geometry, never distorted — a
 *              logo that stretches with audio is a logo being misused. It
 *              deepens and takes a few percent of scale, nothing more.
 *   interior   concentric pulse rings, emitted on syllable onsets, travelling
 *              from the mark out to the seat ring and dying there. Contained
 *              inside the disc on purpose, so they never collide with the
 *              bars and turn the corona into noise.
 *   exterior   the bar corona, outside the seat ring. This is the envelope.
 *
 * The seat ring is the border between "brand" and "signal" and it never
 * moves, which is what makes the corona's depth legible: a change of size is
 * only readable against something that is not changing.
 *
 *
 * ── Light mode is not the dark orb inverted ──────────────────────────────
 *
 * The original build was for a black wall and was made almost entirely of
 * *added light*: a bloom on the ground, a top-light giving the disc its body,
 * strokes at 0.28 alpha that read because they were brighter than what was
 * behind them. None of that survives contact with a Sage 2 ground. White has
 * no headroom to add light into: the bloom becomes a pale wash, the top-light
 * is nothing at all, and a low-alpha stroke on paper is a stroke you cannot
 * see.
 *
 * So the light model is inverted rather than the palette. The orb is ink on
 * paper. Energy makes it **deepen and saturate**, not glow: the halo darkens
 * the paper around it, the bars get heavier and blacker, the mark fills in.
 * Do not "fix" this back to a glow — a glow is what it was, and on white it
 * disappeared.
 *
 * The mechanism is shared and the theme decides the direction, because
 * painting a colour at alpha over near-black is additive and painting the same
 * colour at alpha over near-white is subtractive, for free. What cannot be
 * shared is (a) how much alpha each theme's ground can absorb — white needs
 * roughly double — and (b) which side the disc's modelling comes from. Both
 * live in `ORB` below.
 *
 *
 * ── Idle has a colour, so colour is not the state ────────────────────────
 *
 * Every agent owns their hue permanently, including when they have nothing to
 * say (see the lane bar and wash in wall.css — the orb is only part of it).
 * That removes the axis the earliest version discriminated on: it went from
 * neutral grey to accent, and "which one is coloured" answered "which one is
 * talking" from anywhere in the room.
 *
 * Four axes replace it, and they are deliberately redundant because the
 * question has to be answerable from 20m, at a glance, by someone who is
 * mostly watching the people on the stage:
 *
 *   count   — how deep the corona is. Idle is a fine ring of stubs; a voice is
 *             a corona two thirds as deep as the disc is wide.
 *   weight  — bar width, 4.5px to 10px. Gamma'd hard (WEIGHT_GAMMA) so
 *             thinking and invited stay hairlines and only a voice gets a
 *             heavy line. Line weight is the cue that survives longest as a
 *             room gets deeper; it outlives hue.
 *   ink     — pigment density in the mark and the bars, idle to speaking.
 *   motion  — the pulse rings, which exist only when there is a voice, and
 *             20x the corona excursion.
 *
 * Any one of the four would probably do it. All four together mean the answer
 * does not depend on the room's sightlines being good.
 */

const TAU = Math.PI * 2;

/*
 * The Speechmatics mark, as path data in a 24x24 box.
 *
 * Inlined rather than fetched, and that is a deployment requirement rather
 * than a preference (CLAUDE.md § Deployment): this runs on a machine wheeled
 * into a venue and the wall must paint correctly with the network unplugged
 * and nothing warmed up. `Path2D` also wants the geometry, not a document —
 * an `<img>` would mean a decode, a load event to sequence against, and no way
 * to recolour it per agent without three copies of the file.
 *
 * The mark only, never the wordmark: at 0.52m across it is a shape, and the
 * lockup's type would be 4cm tall and unreadable. Its bounding box is centred
 * on (12, 12) to within a rounding error, so no offset is needed.
 */
const MARK_PATH =
  "M17.4301 6.65978C16.6043 5.91456 15.6818 5.35866 14.7029 4.98L15.6254 " +
  "2.58319C12.3222 1.31429 8.43899 2.12799 5.92938 4.90749C4.88606 6.0636 " +
  "4.20932 7.42112 3.88706 8.83504C3.19822 11.8442 4.12069 15.1272 6.5739 " +
  "17.3387C7.3997 18.0839 8.33022 18.6439 9.31312 19.0225L8.39065 " +
  "21.4234C11.6898 22.6842 15.565 21.8745 18.0706 19.091C19.1139 17.9349 " +
  "19.7906 16.5814 20.1129 15.1634C20.8017 12.1543 19.8792 8.8713 17.426 " +
  "6.65978H17.4301ZM15.573 16.8271C14.0262 18.5431 11.6374 19.0427 9.59912 " +
  "18.2733C8.98683 18.0396 8.40676 17.6932 7.89114 17.2259C5.65949 15.2158 " +
  "5.48225 11.7757 7.49235 9.54402C9.04725 7.81992 11.4562 7.32042 13.4985 " +
  "8.11399C14.0987 8.34762 14.6667 8.69003 15.1742 9.14522C17.4059 11.1553 " +
  "17.5831 14.5955 15.573 16.8271Z";

// Built once and shared by all three orbs. A Path2D carries no paint state, so
// there is nothing per-agent in it — the colour is set on the context.
const MARK = new Path2D(MARK_PATH);

// Samples of envelope history kept per orb. At 60fps this is ~1.6s of travel
// from the top of the ring to the bottom — about the length of a clause, which
// is what makes the motion read as speech rather than as a meter.
const HISTORY = 96;

// Bars drawn around the full circle, mirrored, so half this many per side.
// The pitch at the seat ring is 2*pi*R/BARS ≈ 15.7 stage px = 4.9cm of LED.
// 128 was tried first and the corona read as fur: past about a hundred, the
// ticks stop being countable and the whole ring collapses into one soft edge,
// which is the organic look this was built to get away from. Lower than
// HISTORY on purpose — the buffer is sampled, and it is already smoothed in
// both time and angle, so there is nothing to alias.
const BARS = 96;

// Seat ring radius in stage pixels. 240px = 0.75m at 320px/m, so the disc is
// 1.50m across and the corona reaches 2.15m at full voice. The canvas is
// 720px, which leaves ~16px past the longest bar — enough for the halo to
// reach zero alpha on its own rather than being cut off square against the
// next lane.
const R = 240;

// The mark's box at full size, stage px. 228px = 0.71m across, which is 47%
// of the disc. It has to be this big: the mark is the only fixed, recognisable
// shape on the wall and at half this size it read as a bullet in the middle of
// a meter rather than as the thing the orb is built around.
const MARK_SIZE = 228;

// Where the corona starts and how far it can reach, stage px from centre.
// The gap is deliberate: bars growing straight off the ring weld to it and the
// ring stops reading as a fixed reference. BAR_BASE + BAR_MAX + the grain term
// has to stay clear of the canvas half-width (360) or the corona clips square
// against the next lane.
const BAR_BASE = R + 14;
const BAR_MAX = 84;

// Every bar is drawn, always, even at silence — a berth with no ticks in it
// looks like the machine crashed rather than like an agent with nothing to
// say. This is the stub length.
const BAR_FLOOR = 9;

// Per-state targets, all interpolated rather than switched.
//   active — pigment density, 0..1. *Not* "how coloured" any more: an idle
//            orb is fully its agent's hue, it is simply at its lightest.
//   amp    — peak bar excursion as a fraction of BAR_MAX
//   mark   — the mark's scale, as a fraction of MARK_SIZE
//   idleHz — frequency of the synthetic envelope when not driven by audio;
//            null means "use the real audio level"
const TARGETS = {
  idle: { active: 0.0, amp: 0.06, mark: 0.9, idleHz: 0.32 },
  // Invited reads stronger than thinking on purpose. Every agent is thinking
  // after every question; being *named* is the rarer and more informative
  // fact, and it is the one the audience needs to follow the floor.
  invited: { active: 0.36, amp: 0.13, mark: 0.96, idleHz: 0.55 },
  thinking: { active: 0.22, amp: 0.17, mark: 0.93, idleHz: 1.45 },
  ducked: { active: 0.52, amp: 0.34, mark: 0.97, idleHz: null },
  speaking: { active: 1.0, amp: 1.0, mark: 1.0, idleHz: null },
};

// Halo strength rises faster than `active` does.
//
// Linear, the thinking and invited states put nearly as much ink on the wall
// as a live turn did, and from the back of a room "three agents shaded" is
// indistinguishable from "three agents talking". The exponent widens the gap
// between considering and speaking without making either disappear.
const HALO_GAMMA = 1.5;

// Bar weight is gamma'd far harder than anything else, and that is the single
// most load-bearing number for telling idle from speaking at distance. At 2.2,
// thinking (0.22) lands at 4% of the range and speaking at 100%: the corona is
// a fine comb for every state that is not a voice, and 4.4cm ticks the moment
// one is.
const WEIGHT_GAMMA = 2.2;
const WEIGHT_MIN = 7;
const WEIGHT_MAX = 14;

// Filter time constants, seconds.
const TAU_ATTACK = 0.05;
const TAU_RELEASE = 0.19;
const TAU_STATE = 0.28;

/*
 * Pulse rings.
 *
 * One ring is emitted when the envelope crosses RIPPLE_ON upward, no more
 * often than RIPPLE_GAP. That is deliberately not an onset detector: a real
 * one fires on plosives and stays silent through a long vowel, so the wall
 * would go still exactly when someone is making their point. A threshold with
 * a refractory period fires on the shape of a phrase — a few rings per clause,
 * roughly in step with the stresses — which is what the eye is reading it for.
 *
 * RIPPLE_GAP also caps the draw cost: at 220ms and a 900ms life, at most five
 * rings can ever be alive per orb.
 */
const RIPPLE_ON = 0.42;
const RIPPLE_GAP = 0.22;
const RIPPLE_LIFE = 0.9;

/*
 * Alpha budgets, per theme.
 *
 * These are not a palette — the colours are all role tokens in tokens.css and
 * are read off the lane. These are how much of each ground will take pigment
 * before the shape stops reading, and they differ by roughly 2x because a
 * Sage 2 ground and a near-black one are not the same amount of headroom.
 * Kept here rather than as custom properties because they are rendering
 * parameters for this file and nothing else ever reads them.
 *
 *   halo      peak alpha of the ground tint around the orb
 *   body      the berth disc. Constant — see `_disc`.
 *   model     the disc's modelling gradient; `modelY` is where it comes from,
 *             in units of R. Negative is above (dark: a top-light on a convex
 *             body), positive is below (light: the underside shading that
 *             makes the same convex body read on paper — a highlight there
 *             would be white on white).
 *   seat      the fixed ring between the mark and the corona. Constant: it is
 *             the berth, not the state.
 *   bar       a corona tick, at rest / added at full voice
 *   mark      the Speechmatics mark, at rest / added at full voice
 *   ripple    peak alpha of a pulse ring at birth
 */
const ORB = {
  light: {
    halo: 0.15,
    body: 0.18,
    model: 0.05,
    modelY: 0.46,
    seat: 0.55,
    bar: 0.4,
    barGain: 0.6,
    mark: 0.5,
    markGain: 0.5,
    ripple: 0.3,
  },
  dark: {
    halo: 0.26,
    body: 0.05,
    model: 0.055,
    modelY: -0.42,
    seat: 0.45,
    bar: 0.26,
    barGain: 0.68,
    mark: 0.34,
    markGain: 0.62,
    ripple: 0.24,
  },
};

// Set once, from the class `?theme=dark` puts on <html> before the stylesheets
// load. Nothing toggles the theme at runtime, so nothing has to re-read this.
const P = ORB[document.documentElement.classList.contains("dark") ? "dark" : "light"];

/*
 * Envelope normalisation.
 *
 * The runtime sends the loudest 16ms block RMS since the last frame, post-gain
 * — so a ducked agent genuinely shrinks. Speech RMS from the TTS at unity sits
 * somewhere around 0.15-0.35 full scale, which is a guess until it has been
 * heard through the venue's own gain structure. `?gain=` overrides it from the
 * URL for exactly that reason: at load-in, with the rig in front of you and no
 * way to rebuild anything, "the orbs are barely moving" has to be a one-line
 * fix typed into the address bar.
 */
const FULL_SCALE = 0.35;
const CURVE = 0.7;

function clamp01(v) {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

/** Pull `r,g,b` out of whatever the Radix step resolved to. */
function readColour(el, prop) {
  const raw = getComputedStyle(el).getPropertyValue(prop).trim();
  const probe = document.createElement("span");
  probe.style.color = raw;
  document.body.appendChild(probe);
  const resolved = getComputedStyle(probe).color;
  probe.remove();
  const parts = resolved.match(/[\d.]+/g) || ["255", "255", "255"];
  return [Number(parts[0]), Number(parts[1]), Number(parts[2])];
}

function mix(a, b, t) {
  return [
    Math.round(a[0] + (b[0] - a[0]) * t),
    Math.round(a[1] + (b[1] - a[1]) * t),
    Math.round(a[2] + (b[2] - a[2]) * t),
  ];
}

function rgba([r, g, b], alpha) {
  return `rgba(${r},${g},${b},${alpha})`;
}

export class Orb {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {HTMLElement} lane element carrying `--accent` and `--accent-quiet`
   * @param {number} gain envelope multiplier, from `?gain=`
   */
  constructor(canvas, lane, gain = 1) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.gain = gain;

    // The two ends of one hue. Both are this agent's colour — `quiet` is where
    // the orb rests and `ink` is where a voice takes it — which is why an idle
    // orb is never grey and never anyone else's.
    this.ink = readColour(lane, "--accent");
    this.quiet = readColour(lane, "--accent-quiet");
    this.model = readColour(lane, "--orb-model");

    this.history = new Float32Array(HISTORY);
    this.head = 0;
    this.lengths = new Float32Array(BARS);

    this.target = TARGETS.idle;
    this.level = 0; // smoothed envelope, 0..1
    this.raw = 0; // latest envelope from the wire
    this.active = 0; // smoothed pigment density, 0..1
    this.amp = TARGETS.idle.amp; // smoothed corona excursion
    this.mark = TARGETS.idle.mark; // smoothed mark scale

    // Live pulse rings, oldest first. `since` is seconds since the last one
    // was emitted, which is the refractory clock.
    this.ripples = [];
    this.since = RIPPLE_GAP;

    this.size = 0;
    this.scale = 0;
  }

  setState(name) {
    this.target = TARGETS[name] || TARGETS.idle;
  }

  setLevel(v) {
    this.raw = clamp01(Math.pow(clamp01((v * this.gain) / FULL_SCALE), CURVE));
  }

  /**
   * Size the backing store.
   *
   * The wall's real pixel count is not known until an LED processor is plugged
   * in, and the stage is scaled to meet it. Multiplying the backing store by
   * that scale is what stops a 5120-wide wall getting a 720px orb stretched
   * across 1.3m. Capped at 2 so an unexpectedly huge canvas cannot cost the
   * frame budget the audio path depends on being nowhere near.
   *
   * @param {number} css on-screen size in stage pixels
   * @param {number} pixelScale devicePixelRatio x stage scale
   */
  resize(css, pixelScale) {
    const scale = Math.min(2, Math.max(1, pixelScale));
    if (this.size === css && this.scale === scale) return;
    this.size = css;
    this.scale = scale;
    this.canvas.width = Math.round(css * scale);
    this.canvas.height = Math.round(css * scale);
    // Everything below draws in stage pixels and lets the transform handle
    // the backing resolution.
    this.ctx.setTransform(scale, 0, 0, scale, 0, 0);
  }

  /**
   * @param {number} dt seconds since the last frame
   * @param {number} t seconds since start, for the free-running oscillators
   */
  frame(dt, t) {
    this._advance(dt, t);
    this._draw(t);
  }

  // ------------------------------------------------------------- simulation

  _advance(dt, t) {
    const target = this.target;

    // A voice rises quickly and decays slowly. Two time constants, chosen by
    // direction, is the whole difference between "someone is talking" and "a
    // needle is twitching".
    const source =
      target.idleHz === null
        ? this.raw
        : // Synthetic drive for the states with no audio behind them. A slow
          // sine *in time* becomes a travelling wave *in angle*, because the
          // history buffer is what maps one to the other — so the idle corona
          // breathes and the thinking corona ripples, from the same three
          // lines.
          0.5 + 0.5 * Math.sin(t * TAU * target.idleHz);

    const before = this.level;
    const tau = source > before ? TAU_ATTACK : TAU_RELEASE;
    this.level += (source - this.level) * (1 - Math.exp(-dt / tau));

    const k = 1 - Math.exp(-dt / TAU_STATE);
    this.active += (target.active - this.active) * k;
    this.amp += (target.amp - this.amp) * k;
    this.mark += (target.mark - this.mark) * k;

    this.head = (this.head + 1) % HISTORY;
    this.history[this.head] = this.level;

    this._pulses(dt, before);
  }

  /**
   * Age the pulse rings and decide whether to emit another.
   *
   * Gated on `active` rather than on state so the rings inherit the same 280ms
   * arrival the rest of the orb has: an agent who has just been cut off stops
   * emitting as they fade, instead of firing one last ring into an orb that is
   * already going quiet.
   */
  _pulses(dt, before) {
    for (const ring of this.ripples) ring.age += dt;
    while (this.ripples.length && this.ripples[0].age > RIPPLE_LIFE) {
      this.ripples.shift();
    }

    this.since += dt;
    const crossed = before < RIPPLE_ON && this.level >= RIPPLE_ON;
    if (crossed && this.since >= RIPPLE_GAP && this.active > 0.4) {
      this.ripples.push({ age: 0 });
      this.since = 0;
    }
  }

  // ---------------------------------------------------------------- drawing

  _draw(t) {
    const ctx = this.ctx;
    const c = this.size / 2;
    const level = this.level;
    const active = this.active;
    // One hue, two densities. `active` walks between them, so the transition
    // an audience sees is the agent's own colour getting stronger — never a
    // different colour arriving.
    const colour = mix(this.quiet, this.ink, active);

    ctx.clearRect(0, 0, this.size, this.size);

    this._halo(ctx, c, level, active);
    this._disc(ctx, c);
    this._pulseRings(ctx, c, colour, active);
    this._seat(ctx, c);
    this._corona(ctx, c, t, colour, active);
    this._mark(ctx, c, level, colour, active);
  }

  /**
   * What the orb does to the wall around it.
   *
   * On black this is a bloom and the orb throws light. On Sage 2 the identical
   * gradient deepens the paper instead, and reads as weight rather than as
   * light — which is the only version of "this one is loud" that white can
   * express. Not a drop shadow: it is concentric and unoffset, so it says
   * energy rather than elevation.
   */
  _halo(ctx, c, level, active) {
    const strength = Math.pow(active, HALO_GAMMA) * (0.3 + 0.7 * level);
    if (strength < 0.005) return;
    // The outer stop lands exactly on the canvas edge at zero alpha. Anything
    // still visible there would clip against the next lane as a hard square.
    const g = ctx.createRadialGradient(c, c, R * 0.15, c, c, c - 5);
    g.addColorStop(0, rgba(this.ink, P.halo * strength));
    g.addColorStop(0.42, rgba(this.ink, P.halo * 0.38 * strength));
    g.addColorStop(1, rgba(this.ink, 0));
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, this.size, this.size);
  }

  /**
   * The berth: a tinted disc at the seat radius, in the agent's resting
   * colour, in every state including idle. That is the point — an agent who is
   * not talking still has a seat on the wall.
   *
   * Its density is *constant*, and that is a correction rather than a choice.
   * An earlier pass drove this fill from `active`, which put most of the
   * pigment on a hard-edged circle and meant every other layer had to be read
   * against a ground that was itself moving. Anything that varies with state
   * belongs on the corona and the mark. This layer only ever says "this is
   * whose three metres these are".
   *
   * The second pass is the modelling that stops it being a flat sticker: one
   * soft gradient, no specular, no gloss. Dark mode lights it from above in
   * Sage 12 (near-white there); light mode shades it from below in Sage 12
   * (near-black there). Same token, same geometry, opposite direction — a
   * highlight on paper would be white on white.
   */
  _disc(ctx, c) {
    ctx.beginPath();
    ctx.arc(c, c, R, 0, TAU);
    ctx.fillStyle = rgba(this.quiet, P.body);
    ctx.fill();

    const g = ctx.createRadialGradient(c, c + R * P.modelY, R * 0.1, c, c, R * 1.08);
    g.addColorStop(0, rgba(this.model, P.model));
    g.addColorStop(0.55, rgba(this.model, P.model * 0.33));
    g.addColorStop(1, rgba(this.model, 0));
    ctx.fillStyle = g;
    ctx.fill();
  }

  /**
   * Pulse rings, travelling from the edge of the mark out to the seat ring.
   *
   * Kept strictly inside the disc. They were outside first, and expanding
   * rings crossing a corona of ticks made both illegible — the rings looked
   * like the bars were smearing. Inside, the composition separates cleanly:
   * the interior carries rhythm, the exterior carries amplitude.
   *
   * Quadratic ease-out on the radius and a steeper fade on the alpha, so a
   * ring leaves quickly and arrives at the seat ring already gone rather than
   * piling up against it.
   */
  _pulseRings(ctx, c, colour, active) {
    if (!this.ripples.length) return;
    const from = MARK_SIZE * 0.5;
    const to = R * 0.97;
    for (const ring of this.ripples) {
      const p = ring.age / RIPPLE_LIFE;
      const eased = 1 - (1 - p) * (1 - p);
      const alpha = P.ripple * Math.pow(1 - p, 1.6) * active;
      if (alpha < 0.004) continue;
      ctx.beginPath();
      ctx.arc(c, c, from + (to - from) * eased, 0, TAU);
      ctx.strokeStyle = rgba(colour, alpha);
      ctx.lineWidth = 1.5 + 5 * (1 - p);
      ctx.stroke();
    }
  }

  /**
   * The ring between the mark and the corona. Never moves, never deepens.
   *
   * It matters more than it looks. It is the fixed reference the corona's
   * depth is read against, and it is the edge that keeps the Speechmatics mark
   * in its own space rather than sitting in the middle of a level meter.
   */
  _seat(ctx, c) {
    ctx.beginPath();
    ctx.arc(c, c, R, 0, TAU);
    ctx.strokeStyle = rgba(this.quiet, P.seat);
    ctx.lineWidth = 3;
    ctx.stroke();
  }

  /** The envelope history, as a corona of radial bars. */
  _corona(ctx, c, t, colour, active) {
    const lengths = this.lengths;
    const half = BARS / 2;
    // A slow, shallow drift applied to the whole corona, independent of any
    // audio. Two percent, and the only reason it is here is that a perfectly
    // static ring on a 12m wall looks like a frozen frame.
    const breath = 1 + 0.02 * Math.sin(t * 0.52 * TAU * 0.5);

    for (let i = 0; i < BARS; i++) {
      // Mirror index: 0 at twelve o'clock (newest), half at six (oldest), the
      // same value on both sides. Continuous at both seams. Scaled back onto
      // the history buffer, which is longer than the bar count.
      const m = (i <= half ? i : BARS - i) / half;
      const s = Math.min(HISTORY - 1, Math.round(m * (HISTORY - 1)));
      const v = this.history[(this.head - s + HISTORY) % HISTORY];
      // Older samples sit lower — the clause fades as it travels.
      const taper = Math.pow(1 - m, 0.55);
      // A little high-frequency detail so a loud syllable has texture instead
      // of inflating into a smooth arc.
      const grain = 0.12 * v * Math.sin(s * 0.55 + t * 2.2);
      lengths[i] = BAR_FLOOR + BAR_MAX * this.amp * breath * (v * taper + grain);
    }

    // Three-tap spatial smooth. The temporal filter already keeps consecutive
    // samples close, but a transient still lands in one slot and would show as
    // a spike at two mirrored angles.
    let prev = lengths[BARS - 1];
    const first = lengths[0];
    for (let i = 0; i < BARS; i++) {
      const next = i === BARS - 1 ? first : lengths[i + 1];
      const current = lengths[i];
      lengths[i] = (prev + current * 2 + next) * 0.25;
      prev = current;
    }

    // One path, one stroke. 128 separate strokes per orb per frame is 384
    // paint calls a frame across the wall and shows up in the frame budget;
    // batching them costs nothing because every bar shares its paint.
    const path = new Path2D();
    for (let i = 0; i < BARS; i++) {
      const a = -Math.PI / 2 + (i * TAU) / BARS;
      const cos = Math.cos(a);
      const sin = Math.sin(a);
      const outer = BAR_BASE + Math.max(0, lengths[i]);
      path.moveTo(c + BAR_BASE * cos, c + BAR_BASE * sin);
      path.lineTo(c + outer * cos, c + outer * sin);
    }

    ctx.strokeStyle = rgba(colour, P.bar + P.barGain * active);
    ctx.lineWidth = WEIGHT_MIN + (WEIGHT_MAX - WEIGHT_MIN) * Math.pow(active, WEIGHT_GAMMA);
    // Butt, not round. The brand rules out rounded bar ends, and at 3cm wide a
    // round cap adds a visible 1.5cm to every tick — the corona would read as
    // longer than the signal actually is.
    ctx.lineCap = "butt";
    ctx.stroke(path);
  }

  /**
   * The Speechmatics mark, at the centre of every orb.
   *
   * Never distorted and never driven by the waveform: the scale moves by four
   * percent with the voice and nothing else happens to it. A logo that
   * stretches with audio is a logo being misused, and this one is on a 12m
   * wall in front of the company's own audience.
   *
   * What it *does* do is deepen. At idle it is a watermark in the agent's
   * resting colour; on a live turn it is solid in their ink. That is the same
   * one-hue-two-densities move every other layer makes, so the mark reads as
   * part of the orb rather than as a sticker on top of it.
   */
  _mark(ctx, c, level, colour, active) {
    const size = MARK_SIZE * this.mark * (1 + 0.04 * level);
    const k = size / 24;
    ctx.save();
    ctx.translate(c - size / 2, c - size / 2);
    ctx.scale(k, k);
    ctx.fillStyle = rgba(colour, P.mark + P.markGain * active);
    ctx.fill(MARK);
    ctx.restore();
  }
}
