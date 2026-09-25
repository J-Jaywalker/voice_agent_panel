/*
 * One agent's orb.
 *
 * A ring whose radius is modulated by that agent's own audio envelope, so the
 * thing pulsing on the wall is the thing coming out of the PA rather than a
 * decoration timed to look like it.
 *
 * The shape is a *history*, not a waveform snapshot. Every frame pushes the
 * current envelope into a ring buffer, and the buffer is mapped to angle
 * outward from twelve o'clock down both sides — so a syllable appears at the
 * top and travels down and away over the following second and a half,
 * symmetrically. Mirroring is what makes it a closed curve with no seam: the
 * newest sample meets itself at the top and the oldest at the bottom, so
 * there is no discontinuity to hide.
 *
 * Everything that reads as "smooth" here is one of four things, and none of
 * them is an easing curve:
 *
 *   1. Separate attack and release on the envelope. A voice rises fast and
 *      falls slowly; equal coefficients read as a level meter.
 *   2. Frame-rate-independent filtering (`1 - exp(-dt/tau)`), so a dropped
 *      frame slows the motion rather than jumping it.
 *   3. Colour interpolated as a continuous quantity. The orb does not switch
 *      to its speaking colour, it *arrives* at it over ~300ms.
 *   4. A quadratic curve through segment midpoints, which is C1-continuous at
 *      every joint. Straight lines between 192 points still show as facets on
 *      a 1.4m circle.
 *
 *
 * ── Light mode is not the dark orb inverted ──────────────────────────────
 *
 * The first version of this file was built for a black wall and was made
 * almost entirely of *added light*: an accent bloom thrown onto the ground
 * behind the disc, a `rgba(255,255,255,0.055)` top-light giving the disc its
 * body, a bright core, and strokes at 0.28 alpha that read because they were
 * brighter than what was behind them. None of that survives contact with a
 * Sage 2 ground. White has no headroom to add light into: the bloom becomes a
 * pale wash, the top-light is nothing at all, and a low-alpha stroke on paper
 * is a stroke you cannot see.
 *
 * So the light model is inverted rather than the palette. The orb is ink on
 * paper. Energy makes it **deepen and saturate**, not glow: the halo darkens
 * the paper around it, the disc fills with more pigment, the stroke gets
 * heavier and blacker. Do not "fix" this back to a glow — a glow is what it
 * was, and on white it disappeared.
 *
 * The mechanism is shared and the theme decides the direction, because
 * painting a colour at alpha over near-black is additive and painting the
 * same colour at alpha over near-white is subtractive, for free. What cannot
 * be shared is (a) how much alpha each theme's ground can absorb — white needs
 * roughly double — and (b) which side the disc's modelling comes from. Both
 * live in `ORB` below.
 *
 *
 * ── Idle has a colour now, so colour stopped being the state ─────────────
 *
 * Every agent owns their hue permanently, including when they have nothing to
 * say (see the lane bar and wash in wall.css — the orb is only part of it).
 * That removes the axis the old version discriminated on: it went from
 * neutral grey to accent, and "which one is coloured" answered "which one is
 * talking" from anywhere in the room.
 *
 * Four axes replace it, and they are deliberately redundant because the
 * question has to be answerable from 20m, at a glance, by someone who is
 * mostly watching the people on the stage:
 *
 *   ink     — pigment density inside the curve. Idle is a watermark, speaking
 *             is a solid, and it is the largest area on the wall.
 *   weight  — stroke width, 2.5px to 12px. Gamma'd hard (WEIGHT_GAMMA) so
 *             thinking and invited stay hairlines and only a voice gets a
 *             heavy line. Line weight is the cue that survives longest as a
 *             room gets deeper; it outlives hue.
 *   size    — the live curve sits ~12% inside the seat ring at rest and grows
 *             out past it to speak. The seat ring never moves, so there is a
 *             fixed reference to read that growth against.
 *   motion  — 20x the excursion, and driven by real audio rather than by the
 *             slow synthetic breath.
 *
 * Any one of the four would probably do it. All four together mean the answer
 * does not depend on the room's sightlines being good.
 */

const TAU = Math.PI * 2;

// Samples of envelope history kept per orb. At 60fps this is ~1.6s of travel
// from the top of the ring to the bottom — about the length of a clause, which
// is what makes the motion read as speech rather than as a meter.
const HISTORY = 96;

// Base ring radius in stage pixels. 240px = 1.50m at 320px/m; the loudest
// peaks reach about 2.07m. The canvas is 720px, so a peak leaves ~29px before
// the edge — enough for the 12px stroke and for the halo to reach zero alpha
// on its own rather than being cut off square against the next lane. Raising
// R past ~250 starts clipping, and the canvas has to grow with it.
const R = 240;

// Per-state targets, all interpolated rather than switched.
//   active — pigment density, 0..1. *Not* "how coloured" any more: an idle
//            orb is fully its agent's hue, it is simply at its lightest.
//   amp    — peak radial excursion as a fraction of the base radius
//   radius — base radius of the live curve, as a fraction of the seat ring.
//            Under 1 the curve sits inside its berth; at 1 it fills it and
//            the excursion pushes out past it.
//   idleHz — frequency of the synthetic envelope when not driven by audio;
//            null means "use the real audio level"
const TARGETS = {
  idle: { active: 0.0, amp: 0.014, radius: 0.88, idleHz: 0.32 },
  // Invited reads stronger than thinking on purpose. Every agent is thinking
  // after every question; being *named* is the rarer and more informative
  // fact, and it is the one the audience needs to follow the floor.
  invited: { active: 0.36, amp: 0.03, radius: 0.95, idleHz: 0.55 },
  thinking: { active: 0.22, amp: 0.042, radius: 0.93, idleHz: 1.45 },
  ducked: { active: 0.52, amp: 0.1, radius: 0.94, idleHz: null },
  speaking: { active: 1.0, amp: 0.32, radius: 1.0, idleHz: null },
};

// Halo strength rises faster than `active` does.
//
// Linear, the thinking and invited states put nearly as much ink on the wall
// as a live turn did, and from the back of a room "three agents shaded" is
// indistinguishable from "three agents talking". The exponent widens the gap
// between considering and speaking without making either disappear.
const HALO_GAMMA = 1.5;

// Stroke weight is gamma'd far harder than anything else, and that is the
// single most load-bearing number for telling idle from speaking at distance.
// At 2.2, thinking (0.22) lands at 4% of the range and speaking at 100%: the
// line is a hairline for every state that is not a voice, and 3.75cm of ink
// the moment one is.
const WEIGHT_GAMMA = 2.2;
const WEIGHT_MIN = 2.5;
const WEIGHT_MAX = 12;

// Filter time constants, seconds.
const TAU_ATTACK = 0.05;
const TAU_RELEASE = 0.19;
const TAU_STATE = 0.28;

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
 *   body      the berth disc. Constant — see `_disc` for why none of the
 *             state-driven density is allowed to live here.
 *   model     the disc's modelling gradient; `modelY` is where it comes from,
 *             in units of R. Negative is above (dark: a top-light on a convex
 *             body), positive is below (light: the underside shading that
 *             makes the same convex body read on paper — a highlight there
 *             would be white on white).
 *   seat      the fixed base ring. Constant: it is the berth, not the state.
 *   fill      inside the live curve, at rest / added at full voice. This is
 *             where the pigment lives.
 *   line      the curve's stroke, at rest / added at full voice
 *   core      the centre. A saturated fill on light, a glow on dark.
 */
const ORB = {
  light: {
    halo: 0.15,
    body: 0.18,
    model: 0.05,
    modelY: 0.46,
    seat: 0.55,
    fill: 0.03,
    fillGain: 0.52,
    line: 0.5,
    lineGain: 0.5,
    core: 0.34,
  },
  dark: {
    halo: 0.26,
    body: 0.05,
    model: 0.055,
    modelY: -0.42,
    seat: 0.45,
    fill: 0.03,
    fillGain: 0.18,
    line: 0.3,
    lineGain: 0.6,
    core: 0.18,
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
    this.radii = new Float32Array(HISTORY * 2);

    this.target = TARGETS.idle;
    this.level = 0; // smoothed envelope, 0..1
    this.raw = 0; // latest envelope from the wire
    this.active = 0; // smoothed pigment density, 0..1
    this.amp = TARGETS.idle.amp; // smoothed excursion
    this.radius = TARGETS.idle.radius; // smoothed base radius, fraction of R

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
          // history buffer is what maps one to the other — so the idle ring
          // breathes and the thinking ring ripples, from the same three lines.
          0.5 + 0.5 * Math.sin(t * TAU * target.idleHz);

    const tau = source > this.level ? TAU_ATTACK : TAU_RELEASE;
    this.level += (source - this.level) * (1 - Math.exp(-dt / tau));

    const k = 1 - Math.exp(-dt / TAU_STATE);
    this.active += (target.active - this.active) * k;
    this.amp += (target.amp - this.amp) * k;
    this.radius += (target.radius - this.radius) * k;

    this.head = (this.head + 1) % HISTORY;
    this.history[this.head] = this.level;
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
    this._seat(ctx, c);
    this._wave(ctx, c, t, colour, active);
    this._core(ctx, c, level, active, colour);
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
   * The berth: a tinted disc at the fixed radius, in the agent's resting
   * colour, in every state including idle. That is the point — an agent who
   * is not talking still has a seat on the wall, and three empty rings look
   * like the machine crashed.
   *
   * Its density is *constant*, and that is a correction rather than a choice.
   * The first light-mode pass drove this fill from `active` and put most of
   * the pigment here, which looked right until the wave grew past R: the
   * disc's hard circular edge then showed straight through the translucent
   * curve as a tonal step, so a speaking orb had a pale crescent bitten out
   * of its top. Anything that varies with state belongs on the wave, which
   * has no edge of its own to give away. This layer only ever says "this is
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
   * The base ring. Never moves, never deepens — the agent's berth.
   *
   * It matters more than it used to. The live curve now grows from ~0.88 of
   * this radius to past it, and a change of size is only legible against
   * something that is not changing.
   */
  _seat(ctx, c) {
    ctx.beginPath();
    ctx.arc(c, c, R, 0, TAU);
    ctx.strokeStyle = rgba(this.quiet, P.seat);
    ctx.lineWidth = 3;
    ctx.stroke();
  }

  /** The envelope history, as a closed curve. */
  _wave(ctx, c, t, colour, active) {
    const n = HISTORY * 2;
    const radii = this.radii;
    const base = R * this.radius;
    // A slow, shallow scale applied to the whole ring, independent of any
    // audio. Under 1%, and the only reason it is here is that a perfectly
    // static circle on a 12m wall looks like a frozen frame.
    const breath = 1 + 0.009 * Math.sin(t * 0.52 * TAU * 0.5);

    for (let i = 0; i < n; i++) {
      // Mirror index: 0 at twelve o'clock (newest), HISTORY-1 at six
      // (oldest), the same value on both sides. Continuous at both seams.
      const m = Math.min(i <= HISTORY ? i : n - i, HISTORY - 1);
      const v = this.history[(this.head - m + HISTORY) % HISTORY];
      // Older samples sit lower and quieter — the clause fades as it travels.
      const taper = Math.pow(1 - m / HISTORY, 0.55);
      // A little high-frequency detail so a loud syllable has texture instead
      // of inflating into a smooth balloon.
      const grain = 0.1 * v * Math.sin(m * 0.55 + t * 2.2);
      radii[i] = base * (breath + this.amp * (v * taper + grain));
    }

    // Three-tap spatial smooth. The temporal filter already keeps consecutive
    // samples close, but a transient still lands in one slot and would show as
    // a spike at two mirrored angles.
    let prev = radii[n - 1];
    const first = radii[0];
    for (let i = 0; i < n; i++) {
      const next = i === n - 1 ? first : radii[i + 1];
      const current = radii[i];
      radii[i] = (prev + current * 2 + next) * 0.25;
      prev = current;
    }

    const path = new Path2D();
    let px = c + radii[n - 1] * Math.cos(-Math.PI / 2 + ((n - 1) * TAU) / n);
    let py = c + radii[n - 1] * Math.sin(-Math.PI / 2 + ((n - 1) * TAU) / n);
    // Start on the midpoint of the closing segment so the loop has no joint.
    const a0 = -Math.PI / 2;
    path.moveTo((px + c + radii[0] * Math.cos(a0)) / 2, (py + c + radii[0] * Math.sin(a0)) / 2);
    for (let i = 0; i < n; i++) {
      const a = -Math.PI / 2 + (i * TAU) / n;
      const b = -Math.PI / 2 + (((i + 1) % n) * TAU) / n;
      const cx = c + radii[i] * Math.cos(a);
      const cy = c + radii[i] * Math.sin(a);
      const nx = c + radii[(i + 1) % n] * Math.cos(b);
      const ny = c + radii[(i + 1) % n] * Math.sin(b);
      // Control point on the sample, endpoint on the midpoint to the next:
      // C1-continuous everywhere, and cheaper than fitting real splines.
      path.quadraticCurveTo(cx, cy, (cx + nx) / 2, (cy + ny) / 2);
      px = cx;
      py = cy;
    }
    path.closePath();

    ctx.fillStyle = rgba(colour, P.fill + P.fillGain * active);
    ctx.fill(path);
    ctx.strokeStyle = rgba(colour, P.line + P.lineGain * active);
    ctx.lineWidth = WEIGHT_MIN + (WEIGHT_MAX - WEIGHT_MIN) * Math.pow(active, WEIGHT_GAMMA);
    ctx.lineJoin = "round";
    ctx.stroke(path);
  }

  /**
   * The centre, swelling with the voice. Depth, not decoration.
   *
   * A glow on black; on paper the same gradient is a saturated well of pigment
   * at the middle of the disc, which is what a voice looks like when it cannot
   * be light. Gone entirely at idle — one more thing the resting orb does not
   * have.
   */
  _core(ctx, c, level, active, colour) {
    if (active < 0.02) return;
    const r = R * 0.26 * (1 + 0.22 * level);
    const g = ctx.createRadialGradient(c, c, 0, c, c, r);
    g.addColorStop(0, rgba(colour, P.core * active));
    g.addColorStop(1, rgba(colour, 0));
    ctx.beginPath();
    ctx.arc(c, c, r, 0, TAU);
    ctx.fillStyle = g;
    ctx.fill();
  }
}
