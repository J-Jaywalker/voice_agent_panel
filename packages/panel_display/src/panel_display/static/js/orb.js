/*
 * One agent's orb.
 *
 * A Speechmatics mark seated on a disc, ringed by a corona of radial bars
 * whose lengths are that agent's own audio envelope — so the thing moving on
 * the wall is the thing coming out of the PA rather than a decoration timed to
 * look like it.
 *
 *
 * ── Why a spectrum, and why it is a curve again ──────────────────────────
 *
 * The corona has been three things. Worth stating all three, because two of
 * the transitions were corrections and the third — the current one — is a
 * deliberate reversal of one of those corrections, and the next person here
 * should know which is which.
 *
 *   1. **An envelope history.** One number per frame pushed into a ring
 *      buffer, the buffer mapped to angle. A syllable appeared at twelve
 *      o'clock and travelled down and away over the next second and a half.
 *      Honest about what it had, and it looked like a wave crawling around a
 *      circle — slower than the voice it was drawn from, because most of what
 *      was on screen was the *past*. Replaced, and not coming back.
 *
 *   2. **Discrete mirrored bars.** `Mixer.take_bands` sends `BANDS`
 *      log-spaced amplitudes per voice per frame, 80Hz to 7kHz, and each got
 *      its own pair of ticks — bass at twelve, sibilance at six, mirrored
 *      down both sides. This fixed the mapping, which was the real fault:
 *      every bar became the present, nothing travelled, and a vowel was a
 *      pair of lobes that sat still and breathed.
 *
 *   3. **A continuous curve, unmirrored, full range around the perimeter.**
 *      Where it is now. The spectrum runs once around the circle — bass at
 *      twelve o'clock, through the mids down the right-hand side, sibilance
 *      approaching twelve again from the left — drawn as one closed outline
 *      rather than as sixty-four ticks.
 *
 * **Step 3 reverses two things step 2 was chosen for, on purpose, and the
 * reasons step 2 gave were not wrong — the goal changed.** The old note
 * argued discrete ticks on two grounds: that the brand is sharp and a soft
 * outline is the "cinematic dark UI" the guidelines rule out, and that a
 * smooth outline has no internal landmarks so its amplitude is guesswork at
 * 20m. Both still true as stated. What changed is that the wall is now
 * explicitly going for a liquid-glass surface, where a continuous meniscus
 * around a lit body is the *point* rather than a lapse, and the amplitude
 * question is now answered by the core glow (which is driven by the same
 * audio) rather than by counting ticks. If the room reports back that the
 * corona has stopped being readable at distance, the thing to restore is
 * countability — `PEAK_TICK` marks, or a coarser `RING_POINTS` — not the
 * mirroring.
 *
 * **Mirroring is gone and that is the substantive change**, not the
 * smoothing. The ring used to show sixteen bands twice; it now shows
 * thirty-two once, so the corona carries double the spectral detail and the
 * same band never appears in two places. The cost is a seam: band 0 and band
 * 31 are now neighbours at twelve o'clock and they are bass and sibilance, so
 * they do not agree. That is handled by `RING_SMOOTH` — a circular blur
 * applied to the whole ring, which treats the seam exactly as it treats every
 * other pair of neighbours. No band is special-cased and nothing is faded
 * toward anything, because a crossfade at the top would mean the highest
 * bands were drawn as bass, which is a lie about the data rather than a
 * smoothing of it.
 *
 * Everything that reads as "smooth" is one of four things, and none of them
 * is an easing curve:
 *
 *   1. Separate attack and release, per band. A voice rises fast and falls
 *      slowly; equal coefficients read as a level meter. Per band rather than
 *      overall, so a sibilant can leave while the vowel under it stays.
 *   2. Frame-rate-independent filtering (`1 - exp(-dt/tau)`), so a dropped
 *      frame slows the motion rather than jumping it.
 *   3. Colour interpolated as a continuous quantity. The orb does not switch
 *      to its speaking colour, it *arrives* at it over ~300ms.
 *   4. Peak caps falling under gravity. See `PEAK_FALL` — they are the one
 *      element that is allowed to lag, and that is exactly their job.
 *
 *
 * ── The three zones ──────────────────────────────────────────────────────
 *
 * Reading outward, and the separation is the whole composition:
 *
 *   centre     the Speechmatics mark. Fixed geometry, never distorted — a
 *              logo that stretches with audio is a logo being misused. It
 *              deepens and takes a few percent of scale, nothing more.
 *   interior   the glass body: two slow caustic pools drifting inside it
 *              (`_caustics`, the idle indicator), a core that lights from
 *              within, concentric pulse rings on syllable onsets, and over
 *              all of it the refraction band and two speculars that make it
 *              read as a lens rather than a disc (`_lens`). The rings are
 *              contained inside the disc on purpose, so they never collide
 *              with the corona.
 *   exterior   the waveform, outside the seat ring. This is the spectrum, as
 *              one continuous closed curve.
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
 * So the light model is inverted rather than the palette, and the orb on
 * paper is ink rather than light. The corona and the mark still work exactly
 * that way: energy makes them **deepen and saturate**, the bars get heavier
 * and blacker, the mark fills in. Do not "fix" those back to a luminous
 * glow — a glow is what they were, and on white they disappeared.
 *
 * **The body is the one exception, and it is a recent and deliberate one.**
 * The lens now does glow when its agent speaks, because the brief asked for
 * it and because the ground changed underneath the old answer: the orb used
 * to sit on a lane painted in its own colour at 38%, and now it sits on a
 * near-white frosted card. What makes that work is that the glow is built
 * out of *chroma* rather than luminance — the centre walks from a pale tint
 * to a saturated one (see `this.glow` in the constructor, and `core` /
 * `coreAudio` in `ORB`). Saturation is the one axis near-white has as much
 * headroom in as near-black does, which is why this is not the old mistake
 * repeated. A *white* bloom on paper is still nothing at all, and adding one
 * is still the thing not to do.
 *
 * So: the body lights, the signal inks. That split is load-bearing — the
 * corona is the element that has to be read at 20m and a dark ring on a
 * white card carries much further than a bright one.
 *
 * The mechanism is shared and the theme decides the direction, because
 * painting a colour at alpha over near-black is additive and painting the same
 * colour at alpha over near-white is subtractive, for free. What cannot be
 * shared is (a) how much alpha each theme's ground can absorb — white needs
 * roughly double — (b) which side the disc's modelling comes from, and (c)
 * which step of the agent's scale the body lights *with*. All three live in
 * `ORB` below.
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
 *   depth   — how far the corona reaches. Idle is a fine ring of stubs; a
 *             voice is a corona two thirds as deep as the disc is wide.
 *   detail  — how *uneven* the ring is. Idle is a synthetic shimmer with no
 *             structure in it; a voice has lobes, and lobes that move. This
 *             axis did not exist while the corona was an envelope, and it is
 *             the one that most makes a live turn look live.
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

// Frequency bands the corona is drawn from. Mirrors `SPECTRUM_BANDS` in
// `panel_runtime/mixer.py`, but is not a contract with it: `setBands`
// resamples whatever actually arrives onto this, so the two drifting costs
// resolution and nothing else.
const BANDS = 32;

// Points the closed outline is sampled at, around the full circle. Six per
// band: enough that the curve through them is smooth at 7cm-per-band pitch,
// few enough that the whole ring is one path of ~192 segments per orb per
// frame. Not a resolution claim — the data is still `BANDS` wide, this is
// only how finely the curve between those values is drawn.
const RING_POINTS = BANDS * 6;

/*
 * Circular blur applied to the band array before the curve is drawn, in
 * bands. This is what makes an unmirrored ring closable.
 *
 * Mirroring used to guarantee continuity for free — a band met itself at the
 * top and at the bottom, so there was no seam to place. Running the spectrum
 * once around the perimeter puts band 0 (bass) next to band 31 (sibilance) at
 * twelve o'clock, and those two do not agree, so without this the outline has
 * a step in it at the most visible point on the ring.
 *
 * A circular blur is the honest fix and a crossfade is not. Fading the top
 * bands toward band 0 would close the seam perfectly and would mean the
 * highest frequencies were being drawn as bass — a lie about the data at the
 * exact place the eye goes first. This kernel treats the seam as it treats
 * every other pair of neighbours: nothing is special-cased, the step becomes
 * a steep transition rather than a jump, and the cost is a little spectral
 * resolution spread evenly around the whole ring instead of concentrated in
 * a fib at the top.
 *
 * 1.1 bands is roughly one band either side at half weight. Raising it past
 * about 2 starts dissolving the lobes that make a vowel legible.
 */
const RING_SMOOTH = 1.1;

/*
 * Corona auto-gain.
 *
 * The ring is drawn against its own recent peak rather than against an
 * absolute amplitude, and this is the one piece of the orb that exists
 * because of a property of the *deployment* rather than of the design.
 *
 * Measured on the probe, a loud syllable put the live curve between r=273 and
 * r=301 of a possible 263-347: a 28px excursion on a 1.5m orb, which reads as
 * a circle with a texture on it rather than as a spectrum. The band values
 * only ever spanned 0.11 to 0.45, so more than half the corona's depth was
 * never used. The obvious fix is more gain — and it is the wrong one, because
 * the right amount of gain depends on the venue's own gain structure, which
 * nobody can measure until load-in (CLAUDE.md § Deployment). Hand-tuning
 * `BAND_FULL_SCALE` against a model would have produced a number that is
 * correct for the model and unknown for the room.
 *
 * So the ring normalises itself: every frame it is divided by the loudest
 * band seen recently, which makes the *shape* fill the available depth
 * whatever the absolute level turns out to be. A spectrum display is read for
 * its shape, and this is the standard way to keep that shape legible across
 * an unknown input range.
 *
 * Loudness is not lost, it moves: the core glow and the halo are both driven
 * by `level`, which is untouched, and `amp` still sets how much depth each
 * *state* is allowed — so an idle orb stays a fine ring no matter how hard
 * this normalises. What this controls is only how much of that allowance the
 * shape uses.
 *
 *   NORM_FALL   how fast the reference peak decays, in units per second. 0.4
 *               is a fall from full to the floor in about two seconds — slow
 *               enough that a syllable does not pump the whole ring, fast
 *               enough to follow a speaker getting quieter.
 *   NORM_FLOOR  the smallest reference allowed. Without it, silence divides
 *               by something near zero and the synthetic shimmer blows up
 *               into a full-depth corona on an orb nobody is speaking
 *               through.
 */
const NORM_FALL = 0.4;
const NORM_FLOOR = 0.22;

/*
 * The other end of the same auto-gain: the quietest band seen recently.
 *
 * Dividing by the peak alone was not enough. It set where the top of the ring
 * lands and left the bottom wherever the spectrum's own noise floor happened
 * to be — measured at 0.155 of the corona's depth, so a sixth of the
 * available excursion was spent before the shape started. Tracking the floor
 * as well and stretching between the two is what makes the ring use all of
 * it, and it is what a spectrum analyser has always done.
 *
 * It rises slowly and drops instantly, the mirror of the peak's behaviour: a
 * band going quiet should pull the floor down at once (or the ring clips flat
 * at the bottom for a second) while a rising floor should be eased into, or
 * every consonant lifts the whole ring.
 *
 * NORM_SPAN is the smallest range allowed between floor and peak. It is the
 * guard against the degenerate case — a flat spectrum, where peak equals
 * floor and the stretch would divide by zero and turn rounding error into a
 * full-depth corona.
 */
const NORM_RISE = 0.25;
const NORM_SPAN = 0.14;

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

// How far toward the ink the mark starts, before `active` carries it the rest
// of the way.
//
// The mark is drawn in the agent's own colour and the lane behind it is now a
// wash of that same colour, so at `quiet` the mark is a tint on a tint. That
// was survivable while the lane was near-neutral and is not now: measured on
// the demo, the amber-accented agent's mark came out at 1.28:1 against its
// own disc and 1.45:1 against its lane, because amber's quiet step is a
// bright yellow and the amber lane is sand. That orb read as empty from
// across the room. (Amber is no longer one of the three accents in use — see
// tokens.css — but the fix it forced applies to all three.)
//
// Starting the walk part way toward the ink keeps the state language — it is
// still one hue getting denser, never a different colour arriving — while
// putting a floor under the contrast. With the raised `mark` alpha in `ORB`,
// the three marks now measure 2.24 / 2.24 / 2.10 against their lanes: all
// clear of 2:1 and within 0.14 of each other, where they used to spread from
// 1.45 to 1.68.
const MARK_INK_FLOOR = 0.5;

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

// The same pair again, for the individual bands. Faster than the envelope's
// in both directions, and that is the point of having a second pair: the
// envelope is being watched for "is someone talking", where overshoot reads
// as a twitch, and a band is being watched for detail, where the same
// smoothing reads as mush. 90ms of release is long enough that a frame the
// server skipped is invisible and short enough that a consonant ends.
const TAU_BAND_ATTACK = 0.025;
const TAU_BAND_RELEASE = 0.09;

/*
 * Peak caps: a short tick held at each band's recent maximum, falling under
 * gravity until the band catches it again.
 *
 * The one thing on the orb allowed to lag, and the reason to have it is that
 * a spectrum at 20m is read by its silhouette. The bars themselves are back
 * at the floor within a tenth of a second of a syllable ending, so without
 * these the outline of a phrase is never visible for long enough to register
 * from the back of a room. The caps hold that outline for about a second and
 * sink through it.
 *
 * PEAK_FALL is in units of corona depth per second: a cap dropped from full
 * excursion reaches the floor in 1/0.85 ≈ 1.2s. PEAK_LAG is the dead time
 * before it starts moving at all, which is what stops a cap chasing a band
 * down inside one syllable.
 */
const PEAK_FALL = 0.85;
const PEAK_LAG = 0.12;
const PEAK_TICK = 5;

/*
 * Per-band normalisation — the two numbers most likely to want moving at
 * load-in, and `?gain=` scales both.
 *
 * BAND_FULL_SCALE is lower than the envelope's FULL_SCALE and has to be: that
 * one is an RMS over everything at once, this one is the amplitude of a
 * single band, and a voice never puts all of itself in one.
 *
 * BAND_TILT is the correction for speech's own spectral slope. Voices roll
 * off at roughly 6dB an octave above the first formant, which over the six
 * and a half octaves these bands span is a factor of thirty or so — a flat
 * mapping gives a corona that is three tall bars at twelve o'clock and a
 * hairline everywhere else, with all the detail the spectrum was added for
 * sitting in the part that never moves. Applied as a smooth ramp from 1x at
 * the lowest band to this at the highest.
 *
 * Deliberately short of a full correction. Flattening the slope completely
 * makes a sibilant the same height as a vowel, and that is not what a voice
 * sounds like or what anyone expects a spectrum to look like; 7x of the ~30x
 * leaves the ring bass-weighted and gives the top two thirds of it something
 * to do.
 *
 * Both set against a synthesised voice — a 130Hz fundamental with a 6dB/oct
 * harmonic rolloff at the RMS the mixer meters real speech at — rather than
 * guessed, and both still want checking through the venue's own gain
 * structure like every other figure here. The test that matters is how many
 * bands sit pegged at full excursion: at the first value tried, fourteen of
 * the thirty-two did, and a corona with a flat top is an envelope again.
 * `?gain=` scales both, alongside FULL_SCALE.
 */
// Back at 0.3, the value this was calibrated to against a synthesised voice.
// It briefly went to 0.18 to buy the corona more excursion, which was
// treating the symptom: how deep the ring runs is now set by the normaliser
// below (`NORM_FALL`), not by how hot this constant is, so this one is free
// to go back to being an honest estimate of a single band's amplitude. The
// standing warning still applies — re-measure through `uv run panel
// --display` on the venue rig before the show.
const BAND_FULL_SCALE = 0.3;
const BAND_TILT = 7.0;

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
 *   barBloom  the wide soft pass under the ticks, at full voice. The orb's
 *             only material effect — see `_waveform`.
 *   sheen     the diffuse wash across the top of the disc, in `--orb-sheen`.
 *             Light mode's is the stronger of the two and that is not a
 *             mistake: there, the disc is a *tinted* body and Sage 1 is a
 *             genuinely lighter thing to lay over it, so the highlight has
 *             somewhere to go. On black the disc is already barely above the
 *             ground, and the same wash at the same strength turns it into a
 *             lit panel.
 *   refract   the band of light hugging the inside of the rim — `_lens`.
 *             The single most load-bearing number for "is this glass": it is
 *             what a thick edge does to whatever is behind it.
 *   spec      the two specular arcs, also `_lens`. A real highlight, as
 *             opposed to `sheen`'s diffuse wash.
 *   caustic   the two slow light pools drifting inside the body — `_caustics`.
 *             This is the idle indicator, so it is at its strongest when the
 *             agent has nothing to say.
 *   rim       the hairline just inside the seat ring, same colour. What
 *             separates "a disc" from "a disc with a thickness".
 *   mark      the Speechmatics mark, at rest / added at full voice
 *   ripple    peak alpha of a pulse ring at birth
 */
const ORB = {
  light: {
    // See `this.glow` in the constructor. On paper the lens lights with the
    // vivid identity step, not the dark text step.
    glowQuiet: true,
    // See `this.shade`. On paper the lens is shaded with its own ink, not
    // with near-black.
    shadeInk: true,
    halo: 0.26,
    // 0.10, down from 0.18. That 0.18 was the body of a disc read against a
    // lane painted in this same colour at 38%, where the disc had to hold its
    // own against a saturated ground. It now sits on a near-white frosted
    // card, where the same fill is the "grey blob" the orb was reported as:
    // opaque enough that nothing behind it shows through, which is the one
    // property glass cannot do without. At 0.10 the card's lit face and the
    // agent's pool are visibly *through* the body, and the modelling, edge
    // and caustics below are what give it a shape instead.
    body: 0.3,
    model: 0.05,
    modelY: 0.46,
    seat: 0.55,
    bar: 0.4,
    barGain: 0.6,
    barBloom: 0.09,
    // 0.10, down from 0.26. A near-white wash is the most effective
    // desaturator on this orb: measured on the wall, a 0.10 tint under a
    // 0.26 sheen plus the caustics and the refraction band came out at a
    // chroma of 23 against the card's own 13 — a tinted white, which is a
    // grey blob with extra steps. The diffuse term only has to say "there
    // is a face here"; the specular arcs say the rest.
    sheen: 0.1,
    refract: 0.3,
    spec: 0.46,
    caustic: 0.1,
    rim: 0.2,
    // The meniscus fill between the seat ring and the curve, at full voice.
    // Light mode's ground takes roughly double the pigment dark's does, which
    // is the same ratio every other pair here sits at.
    menisc: 0.16,
    // The core: what the glass lights *with*. Light mode cannot glow — see
    // the note at the top — so there it is a deepening of the disc's centre
    // and the numbers are a pigment budget, not a luminance one.
    // 0.30 / 0.55, up from 0.20 / 0.26, and this pair is the "glow when
    // talking" half of the brief. On white a glow cannot be made of
    // luminance — the long note at the top still stands — so it is made of
    // *chroma*: the lens walks from a pale mint to a saturated green as the
    // agent speaks, under a specular that does not move. Adding saturation
    // to near-white is as visible as adding light to near-black, and unlike
    // a white bloom it survives being painted on paper.
    //
    // Both roughly doubled from the figures they had as a *subtractive*
    // core. That is not a free choice either: alpha and perceived strength
    // are not the same curve on the two grounds. 0.26 of jade 11 on black is
    // most of the way to the vivid step, because the ground contributes
    // nothing; 0.26 of jade 9 on near-white is a pastel, because the ground
    // contributes three quarters of every pixel. Matching the *look* costs
    // about twice the alpha, which is the same ratio every other pair in
    // this table sits at.
    core: 0.3,
    coreAudio: 0.55,
    inner: 0.16,
    // 0.78, not the 0.5 this was while the lanes were near-neutral. Half the
    // mark's pixels were the disc showing through, which is why deepening its
    // *colour* alone moved the measured contrast by 0.1 and no further. The
    // cost is that the mark's own idle-to-speaking range narrows to 0.78-1.0;
    // the orb spends four redundant axes on that question (count, weight, ink,
    // motion) and the brand mark being legible at rest outranks one of them.
    mark: 0.78,
    markGain: 0.22,
    ripple: 0.3,
  },
  dark: {
    // On black the ink step *is* the vivid one, so lighting and drawing are
    // the same colour and there is nothing to split. And `--orb-model` is
    // near-white there, so the modelling gradient is a top-light rather than
    // an underside shadow — light, which has no hue to get wrong.
    glowQuiet: false,
    shadeInk: false,
    halo: 0.26,
    // Also a rim density rather than a flat fill now. 0.14 against light's
    // 0.30 is the usual ratio inverted for the usual reason: on near-black
    // the same pigment buys far more contrast, and a lens whose edge is too
    // dense stops being glass and becomes a lit ring.
    body: 0.14,
    model: 0.055,
    modelY: -0.42,
    seat: 0.45,
    bar: 0.26,
    barGain: 0.68,
    barBloom: 0.07,
    sheen: 0.09,
    // Dark's lens is built almost entirely out of its edge, which is the same
    // trade every other dark token here makes: on near-black there is no
    // body to shade, so the refraction band and the specular are most of what
    // says "thickness". The caustics stay quieter than light's — a drifting
    // pool of light on black is a lava lamp two steps before it is glass, and
    // the brand rules out exactly that register.
    refract: 0.22,
    spec: 0.3,
    caustic: 0.09,
    rim: 0.22,
    menisc: 0.09,
    core: 0.3,
    coreAudio: 0.48,
    inner: 0.2,
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

// BAND_TILT as a per-band multiplier. Built once and shared — it is a pure
// function of the band count and the same for all three orbs.
const TILT = Float32Array.from({ length: BANDS }, (_, i) =>
  Math.pow(BAND_TILT, i / (BANDS - 1))
);

// Fixed per-band phase offsets for the synthetic corona, in radians.
//
// The golden angle, which is doing real work: the states with no audio behind
// them (idle, thinking, invited) have to put *something* on the ring, and
// stepping the phase by any simple fraction of a turn produces a pattern that
// drifts around the circle — which is the travelling wave this whole rewrite
// exists to be rid of. An irrational step never lines up, so neighbouring
// bands are never in step and the ring shimmers in place instead.
const PHASE = Float32Array.from({ length: BANDS }, (_, i) => i * 2.39996);

/*
 * The circular smoothing kernel, normalised, built once.
 *
 * Gaussian truncated at two sigma — past that the weights are under 2% and
 * buying nothing but arithmetic. `KERNEL_R` is the half-width in bands, and
 * the kernel is applied with wrap-around indexing, which is the whole point:
 * band 0's left neighbour is band 31, so the seam at twelve o'clock is
 * smoothed by exactly the same operation as every other point on the ring.
 */
const KERNEL_R = Math.max(1, Math.round(RING_SMOOTH * 2));
const KERNEL = (() => {
  const w = [];
  let sum = 0;
  for (let d = -KERNEL_R; d <= KERNEL_R; d++) {
    const v = Math.exp(-(d * d) / (2 * RING_SMOOTH * RING_SMOOTH));
    w.push(v);
    sum += v;
  }
  return Float32Array.from(w, (v) => v / sum);
})();

/** Circular convolution of `src` onto `dst`, both length BANDS. */
function smoothRing(src, dst) {
  for (let i = 0; i < BANDS; i++) {
    let acc = 0;
    for (let d = -KERNEL_R; d <= KERNEL_R; d++) {
      // `+ BANDS` before the modulo: JS `%` keeps the sign of the dividend,
      // so a bare `(i + d) % BANDS` indexes negatively at the seam and reads
      // undefined out of the typed array.
      acc += KERNEL[d + KERNEL_R] * src[(i + d + BANDS) % BANDS];
    }
    dst[i] = acc;
  }
}

/*
 * Catmull-Rom through four band values, wrapped.
 *
 * The curve is sampled at `RING_POINTS` and the data is `BANDS` wide, so five
 * of every six points sit between two bands and need interpolating. Linear
 * interpolation gives a polygon with a visible corner at every band — at this
 * size that is thirty-two creases around the ring, which is the discrete-bar
 * look arriving back through the door. Catmull-Rom is C1 and passes through
 * every band value exactly, so the ring still *is* the spectrum rather than
 * an approximation of it.
 */
function catmull(p0, p1, p2, p3, u) {
  const u2 = u * u;
  const u3 = u2 * u;
  return (
    0.5 *
    (2 * p1 +
      (-p0 + p2) * u +
      (2 * p0 - 5 * p1 + 4 * p2 - p3) * u2 +
      (-p0 + 3 * p1 - 3 * p2 + p3) * u3)
  );
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
    this.sheen = readColour(lane, "--orb-sheen");

    /*
     * What the lens lights *with*, as opposed to what the orb draws *in*.
     *
     * The two are the same colour on black and deliberately different on
     * paper, and getting this wrong is what made the first pass at "glow when
     * talking" come out as a dirty grey dish.
     *
     * On black, `ink` is the vivid step (jade 9) and lighting with it is
     * simply turning the lamp up. On paper, `ink` is the *text* step (jade
     * 11) — a dark teal, chosen so 50px of it is readable on a white card.
     * Flooding the middle of a pale lens with a dark teal at rising alpha is
     * a perfectly good way to say "louder" and a terrible way to say "lit":
     * the orb got darker and greyer exactly when it was supposed to come
     * alive. `quiet` is the vivid step there (jade 9, the identity fill), so
     * on paper the core lights with that instead and the centre walks from a
     * pale mint to a saturated green while the specular above it holds still.
     *
     * The corona is left on `ink` in both themes and that split is the
     * design, not an oversight: the body is the thing that lights, the
     * spectrum is the thing that has to be *read*, and on a white card a
     * dark ring is legible from much further away than a bright one.
     */
    this.glow = P.glowQuiet ? this.quiet : this.ink;

    /*
     * What the lens is *shaded* with — the modelling gradient in `_disc` and
     * the inner edge in `_core`.
     *
     * `--orb-model` is Sage 12, which is near-black on paper, and shading a
     * coloured lens with near-black is the other half of why it came out
     * grey. A shadow inside tinted glass is not grey; it is a *denser* pour
     * of the same tint, because what is making it dark is more glass. So on
     * paper this is the agent's own ink — a dark teal for jade, a brown for
     * amber — and the lens gets deeper in its own hue rather than dirtier.
     *
     * Dark keeps Sage 12, where it is near-*white* and `modelY` is negative:
     * there the same gradient is a top-light rather than an underside
     * shadow, and the thing being added is light, which has no hue to get
     * wrong.
     */
    this.shade = P.shadeInk ? this.ink : this.model;

    // Three arrays, one entry per band, and the split is worth stating:
    //   wire  — the last spectrum that arrived, already normalised. Replaced
    //           wholesale by `setBands`, never filtered — the filtering is a
    //           property of the display and belongs where the frames are.
    //   band  — what is drawn. Chases `wire` with attack and release.
    //   peak  — the cap above each bar, falling under gravity.
    this.wire = new Float32Array(BANDS);
    this.band = new Float32Array(BANDS);
    this.peak = new Float32Array(BANDS);
    this.held = new Float32Array(BANDS); // seconds each cap has been stationary
    // Whether a spectrum has arrived recently enough to draw. Cleared when a
    // turn ends, so the corona falls back to the synthetic shimmer rather
    // than freezing on the last vowel of the last thing anyone said.
    this.lit = false;
    // The band array after `RING_SMOOTH`, and the ring radii sampled from it.
    // Both are scratch — rebuilt every frame, kept as fields only so the draw
    // path allocates nothing.
    this.ring = new Float32Array(BANDS);
    // Reference peak for the corona's auto-gain. See NORM_FALL.
    this.norm = NORM_FLOOR;
    this.lo = 0;
    this.ringPeak = new Float32Array(BANDS);
    this.radius = new Float32Array(RING_POINTS);

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
    /*
     * The lens surface's four gradients, built by `_buildLens` from
     * `resize`. Null until then.
     *
     * Both callers resize before the first frame — `buildLanes` is
     * synchronous and ends in `fitStage`, and the probe resizes on the line
     * after construction — so this should never be read while null. It is
     * guarded in `_lens` anyway rather than left to throw: an exception
     * inside the frame loop takes all three orbs down for the rest of the
     * show, and a lens-less orb is a legible orb. Same reasoning as the
     * reconnect-forever socket.
     */
    this.lens = null;
  }

  setState(name) {
    this.target = TARGETS[name] || TARGETS.idle;
  }

  setLevel(v) {
    this.raw = clamp01(Math.pow(clamp01((v * this.gain) / FULL_SCALE), CURVE));
  }

  /**
   * One frame of frequency spectrum, straight off the socket.
   *
   * Resampled onto `BANDS` by nearest neighbour rather than requiring the
   * server to send exactly that many. The wire format is "a spectrum", not "a
   * spectrum of this length": the runtime, the demo and this file each state
   * the count separately, and a mismatch should cost resolution rather than
   * blank the corona in front of an audience.
   *
   * Normalisation is the same shape the envelope gets — gain, full scale,
   * then the same `CURVE` — with the spectral tilt in front of it, so a
   * sibilant and a vowel are comparable heights on the ring instead of the
   * ring being bass and nothing else.
   *
   * @param {number[] | null} values band amplitudes, or null to go quiet
   */
  setBands(values) {
    if (!values || !values.length) {
      this.lit = false;
      return;
    }
    this.lit = true;
    const n = values.length;
    for (let i = 0; i < BANDS; i++) {
      const raw = values[n === BANDS ? i : Math.min(n - 1, Math.round((i * (n - 1)) / (BANDS - 1)))];
      const scaled = (raw * this.gain * TILT[i]) / BAND_FULL_SCALE;
      this.wire[i] = clamp01(Math.pow(clamp01(scaled), CURVE));
    }
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
    this._buildLens();
  }

  /**
   * Build the lens surface's gradients once.
   *
   * Everything `_lens` draws is a function of the centre and `R` alone — it
   * is deliberately independent of audio and of state (see the note there),
   * which means all four of its gradients are constant for the life of a
   * canvas size. Rebuilding them every frame was three orbs × four gradient
   * objects × 60fps of garbage for an image that never changes.
   *
   * Called from `resize`, which is the only thing that can invalidate them,
   * and which `fitStage` already calls on load and on every window resize.
   * `CanvasGradient` is not bound to a transform, so the scale change
   * `resize` makes above does not affect these — they are in stage pixels
   * like everything else in this file.
   */
  _buildLens() {
    const c = this.size / 2;

    const ref = this.ctx.createRadialGradient(c, c, R * 0.68, c, c, R);
    ref.addColorStop(0, rgba(this.sheen, 0));
    ref.addColorStop(0.62, rgba(this.sheen, P.refract * 0.18));
    ref.addColorStop(0.9, rgba(this.sheen, P.refract));
    ref.addColorStop(1, rgba(this.sheen, P.refract * 0.42));

    this.lens = {
      refract: ref,
      // Upper left, the key. Short of the top so it reads as light arriving
      // from one side rather than as a ring that failed to close.
      key: this._specularGradient(c, R * 0.9, -2.72, -1.5, P.spec),
      // Lower right, the back surface. Thinner, dimmer, and deliberately
      // not the mirror image — a symmetrical pair reads as a drawing of
      // glass rather than as glass.
      back: this._specularGradient(c, R * 0.88, 0.42, 1.12, P.spec * 0.3),
    };
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
        : // Synthetic drive for the states with no audio behind them: a slow
          // breath for idle, a quicker one for thinking. Only the overall
          // level — the ring's *shape* in those states comes from `PHASE` in
          // `_bands` below, not from here.
          0.5 + 0.5 * Math.sin(t * TAU * target.idleHz);

    const before = this.level;
    const tau = source > before ? TAU_ATTACK : TAU_RELEASE;
    this.level += (source - this.level) * (1 - Math.exp(-dt / tau));

    const k = 1 - Math.exp(-dt / TAU_STATE);
    this.active += (target.active - this.active) * k;
    this.amp += (target.amp - this.amp) * k;
    this.mark += (target.mark - this.mark) * k;

    this._bands(dt, t, target);
    this._pulses(dt, before);
  }

  /**
   * Move every band toward what it should be, and drag its cap after it.
   *
   * The target is the real spectrum while one is arriving and a synthetic
   * shimmer otherwise. The fallback is not a degraded mode — idle, thinking
   * and invited have no audio *by definition*, and a ring of dead stubs under
   * a name for most of an hour is the thing the lane washes were added to
   * stop happening. What it must not be is anything that travels; see
   * `PHASE`.
   */
  _bands(dt, t, target) {
    const attack = 1 - Math.exp(-dt / TAU_BAND_ATTACK);
    const release = 1 - Math.exp(-dt / TAU_BAND_RELEASE);
    const hz = target.idleHz;
    for (let i = 0; i < BANDS; i++) {
      let want;
      if (this.lit && hz === null) {
        want = this.wire[i];
      } else {
        // The synthetic ring falls away toward the treble end — 1 at the bass
        // and 0.45 at the top — because speech does, and a perfectly even
        // corona reads as a dial rather than as a voice at rest.
        const wobble = 0.5 + 0.5 * Math.sin(t * TAU * (hz || 0.32) * 0.7 + PHASE[i]);
        want = this.level * (1 - 0.55 * (i / (BANDS - 1))) * (0.35 + 0.65 * wobble);
      }
      const current = this.band[i];
      this.band[i] = current + (want - current) * (want > current ? attack : release);

      // The cap. Snapped up instantly on a new maximum — a cap that eased
      // upward would be below the bar it is meant to be marking — then held,
      // then dropped at a constant rate.
      if (this.band[i] >= this.peak[i]) {
        this.peak[i] = this.band[i];
        this.held[i] = 0;
      } else {
        this.held[i] += dt;
        if (this.held[i] > PEAK_LAG) {
          this.peak[i] = Math.max(this.band[i], this.peak[i] - PEAK_FALL * dt);
        }
      }
    }

    // The corona's reference peak. Snaps up to any new maximum so a loud
    // syllable never clips, and bleeds away slowly so the ring does not pump
    // between syllables. Tracked here rather than in the draw path because
    // this is where `dt` lives and because it is a property of the signal,
    // not of the rendering.
    let loudest = 0;
    let quietest = 1;
    for (let i = 0; i < BANDS; i++) {
      if (this.band[i] > loudest) loudest = this.band[i];
      if (this.band[i] < quietest) quietest = this.band[i];
    }
    this.norm = Math.max(NORM_FLOOR, loudest, this.norm - NORM_FALL * dt);
    // Instant down, eased up — see NORM_RISE.
    this.lo =
      quietest < this.lo ? quietest : this.lo + (quietest - this.lo) * (1 - Math.exp(-dt / NORM_RISE));
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
    // The mark walks the same two densities, but from a floor rather than from
    // zero. It is the one element that has to stay legible against a lane now
    // washed in this agent's *own* colour, and at `quiet` it was not: amber's
    // quiet is a bright yellow, so the amber-accented agent's mark at idle was
    // a half-alpha yellow on a sand lane and that orb read as empty. Starting
    // the walk part way toward the ink is the fix that keeps the state
    // language — it is still one hue getting stronger, just never at its
    // lightest.
    const markColour = mix(this.quiet, this.ink, MARK_INK_FLOOR + (1 - MARK_INK_FLOOR) * active);

    ctx.clearRect(0, 0, this.size, this.size);

    this._halo(ctx, c, level, active);
    this._disc(ctx, c);
    this._caustics(ctx, c, t, active);
    this._core(ctx, c, level, active);
    this._pulseRings(ctx, c, colour, active);
    // The surface of the glass, so it goes over everything inside the glass
    // — body, caustics, core, rings — and under the seat ring and the mark.
    // Physically a specular belongs on top of the logo too, since the logo is
    // *in* the lens and the highlight is *on* it; it is kept under because a
    // bright arc crossing the Speechmatics mark is the mark being decorated,
    // and that is not a trade this wall gets to make.
    this._lens(ctx, c);
    this._seat(ctx, c);
    this._waveform(ctx, c, t, colour, active);
    this._mark(ctx, c, level, markColour, active);
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
    g.addColorStop(0, rgba(this.glow, P.halo * strength));
    g.addColorStop(0.42, rgba(this.glow, P.halo * 0.38 * strength));
    g.addColorStop(1, rgba(this.glow, 0));
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
    /*
     * The body, and the thing that makes it glass rather than a swatch is
     * that it is **denser at the rim than at the centre**.
     *
     * That is not a stylistic gradient, it is what coloured glass does: you
     * are looking through a few millimetres of it in the middle of a lens
     * and through several centimetres at the edge, so the edge carries far
     * more of the tint. Getting this wrong in the obvious direction — one
     * flat alpha across the whole disc — is most of why the orb read as a
     * blob. A flat tint has no interior, so the only thing distinguishing it
     * from a circle of paint is whatever is laid on top, and what was laid
     * on top here was three separate near-white washes.
     *
     * **`P.body` is the rim density now, not the fill.** It went 0.10 to
     * 0.30 with that change of meaning, which is not a tripling of the
     * pigment — the centre sits at 0.30 of it, so the middle of the lens is
     * *lighter* than the flat 0.10 it replaced and the orb is more
     * see-through, not less.
     */
    const body = ctx.createRadialGradient(c, c, 0, c, c, R);
    body.addColorStop(0, rgba(this.quiet, P.body * 0.3));
    body.addColorStop(0.5, rgba(this.quiet, P.body * 0.44));
    body.addColorStop(0.84, rgba(this.quiet, P.body * 0.82));
    body.addColorStop(1, rgba(this.quiet, P.body));
    ctx.fillStyle = body;
    ctx.fill();

    const g = ctx.createRadialGradient(c, c + R * P.modelY, R * 0.1, c, c, R * 1.08);
    g.addColorStop(0, rgba(this.shade, P.model));
    g.addColorStop(0.55, rgba(this.shade, P.model * 0.33));
    g.addColorStop(1, rgba(this.shade, 0));
    ctx.fillStyle = g;
    ctx.fill();

    /*
     * The diffuse sheen: a vertical wash from the top of the disc, dying out
     * by its middle, reusing the circle already on the context as a clip.
     *
     * Linear and not radial on purpose — a radial highlight is a hotspot,
     * which is a gloss, and the brand rules gloss out. This is what a flat
     * translucent surface does under a diffuse room light, and it stops well
     * short of the mark so it never sits behind the logo.
     *
     * Halved when `_lens` arrived. It was carrying two jobs at 0.5: the
     * diffuse face *and* a stand-in for a specular the orb did not have. A
     * wash strong enough to read as a highlight is also strong enough to make
     * the body opaque, which is most of how a translucent disc turns into a
     * grey blob. The highlight is now a real one with an edge to it (`spec`),
     * so this can go back to being only the diffuse term.
     */
    const s = ctx.createLinearGradient(c, c - R, c, c + R * 0.1);
    s.addColorStop(0, rgba(this.sheen, P.sheen));
    s.addColorStop(0.45, rgba(this.sheen, P.sheen * 0.28));
    s.addColorStop(1, rgba(this.sheen, 0));
    ctx.fillStyle = s;
    ctx.fill();

    // The thickness. A hairline a few pixels inside the seat ring, brightest
    // at the top where the sheen lands and gone by the bottom — the inside of
    // an edge catching the same light the face does.
    const rim = ctx.createLinearGradient(c, c - R, c, c + R);
    rim.addColorStop(0, rgba(this.sheen, P.rim));
    rim.addColorStop(0.6, rgba(this.sheen, 0));
    ctx.beginPath();
    ctx.arc(c, c, R - 4, 0, TAU);
    ctx.strokeStyle = rim;
    ctx.lineWidth = 3;
    ctx.stroke();
  }

  /**
   * Two slow pools of light drifting inside the body. The idle indicator.
   *
   * This is the answer to "what is this orb doing when its agent has nothing
   * to say", and the constraint on it is that the answer must be *almost
   * nothing*. An idle orb that pulses, spins or breathes visibly is three
   * agents competing for attention with the person actually talking; an idle
   * orb that is completely static is a frozen frame on a 12m wall, which is
   * the problem `breath` in `_waveform` was already solving for the corona.
   *
   * So: light that has clearly moved if you look back in ten seconds, and
   * that you cannot catch moving if you watch it. The two pools orbit at
   * 23 and 31 seconds — incommensurate, so the pair never returns to the
   * same arrangement within any plausible length of show — and each is
   * squashed and rotated on its own slower cycle, which is the "distorted"
   * half of it. The distortion is what keeps them from reading as two
   * headlights: a circular blob of light inside a circle is a lamp, and an
   * ellipse that is slowly changing its aspect is a reflection on something
   * curved.
   *
   * **Strongest at idle and damped when the agent speaks**, which is the
   * opposite of everything else on this orb. The core is a far brighter
   * light source than these are; leaving them at full strength under it
   * would read as mottling on the glass rather than as caustics in it.
   *
   * Cost is two radial gradients and a clip per orb per frame, which is
   * within what `_core` already spends. Nothing here allocates.
   */
  _caustics(ctx, c, t, active) {
    const alpha = P.caustic * (1 - 0.55 * active);
    if (alpha < 0.004) return;

    ctx.save();
    ctx.beginPath();
    ctx.arc(c, c, R, 0, TAU);
    ctx.clip();

    /*
     * Two pools: the leading one larger and further out, the second smaller
     * and travelling the other way, so they cross rather than orbit in
     * convoy.
     *
     * Both are kept well off centre, and the radii are smaller than the
     * first attempt at this by about a third. At `dist 0.46` / `rx 0.78`
     * each pool reached across the middle of the lens and the pair spent
     * most of their cycle overlapping behind the mark, which did not read as
     * two lights moving — it read as a single pale smudge that happened to
     * change shape. Light pools have to be visibly *apart* from each other
     * and from the logo, or the eye files them as dirt on the glass.
     */
    this._pool(ctx, c, t / 23, 0.54, 0.5, 0.34, t / 37, alpha);
    this._pool(ctx, c, -t / 31 + 2.1, 0.44, 0.36, 0.26, -t / 29, alpha * 0.8);

    ctx.restore();
  }

  /**
   * One caustic pool. `turn` and `spin` are in revolutions, not radians —
   * they are divided out of `t` by a period in seconds at the call site, so
   * the numbers there read as "once every 23 seconds" rather than as a rate.
   *
   * The ellipse is produced by scaling the context around the pool's centre,
   * so a unit-radius radial gradient becomes an ellipse with a soft edge for
   * free; `wobble` modulates its aspect on a third cycle, which is the
   * distortion.
   */
  _pool(ctx, c, turn, dist, rx, ry, spin, alpha) {
    const a = turn * TAU;
    const wobble = 1 + 0.22 * Math.sin(turn * TAU * 1.7);
    ctx.save();
    ctx.translate(c + Math.cos(a) * R * dist, c + Math.sin(a) * R * dist);
    ctx.rotate(spin * TAU);
    ctx.scale(R * rx * wobble, R * ry / wobble);
    const g = ctx.createRadialGradient(0, 0, 0, 0, 0, 1);
    g.addColorStop(0, rgba(this.sheen, alpha));
    g.addColorStop(0.55, rgba(this.sheen, alpha * 0.34));
    g.addColorStop(1, rgba(this.sheen, 0));
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(0, 0, 1, 0, TAU);
    ctx.fill();
    ctx.restore();
  }

  /**
   * The surface of the lens: the refraction band and the two speculars.
   *
   * This is the pass that answers "why is this glass and not a tinted
   * circle", and it is almost all in the first of the two.
   *
   * **The refraction band.** A thick piece of glass does not pass what is
   * behind it straight through — near the rim the viewing angle steepens and
   * the background is compressed into a bright band hugging the edge. That
   * band is the single most recognisable thing about every lens anyone has
   * ever looked at, and it is the reason a flat fill with a highlight on it
   * reads as a sticker no matter how good the highlight is. Drawn as a
   * radial gradient that is nothing until 70% of the way out and then ramps
   * hard, with a slight step back at the very edge, because the compression
   * peaks just *inside* the rim rather than at it.
   *
   * **The speculars.** Two, and the second one matters more than it looks.
   * A single highlight reads as a shiny disc; a bright primary with a weaker
   * secondary roughly opposite it is what a transparent body does, because
   * the second one is light that has gone through the glass and come off the
   * *back* surface. It is the cue that says "you can see into this" without
   * anything actually being visible through it.
   *
   * Both are arcs rather than blobs — a highlight on a sphere is a blob, a
   * highlight on a lens is a sliver following the rim — and both are tapered
   * to nothing at their ends by a gradient along the arc's own chord, so
   * neither has a visible start or stop. A specular with ends is a painted
   * line.
   *
   * The whole pass is independent of audio and of state. Light does not get
   * brighter because somebody is speaking, and the one thing holding this
   * composition together while the core swings through its range is that the
   * surface above it never moves.
   */
  _lens(ctx, c) {
    if (!this.lens) return;
    ctx.save();

    ctx.beginPath();
    ctx.arc(c, c, R, 0, TAU);
    ctx.clip();
    ctx.fillStyle = this.lens.refract;
    ctx.fillRect(c - R, c - R, R * 2, R * 2);

    // Out of the clip, back into a clean state. The speculars sit *on* the
    // rim rather than inside it, so clipping them to the disc would shave
    // their outer half off.
    ctx.restore();

    // One `save` for both arcs, for `lineCap` alone: it is the one context
    // property this file sets anywhere, and leaving it round would silently
    // change the ends of every open stroke drawn after it.
    ctx.save();
    ctx.lineCap = "round";
    ctx.strokeStyle = this.lens.key;
    ctx.lineWidth = 11;
    ctx.beginPath();
    ctx.arc(c, c, R * 0.9, -2.72, -1.5);
    ctx.stroke();
    ctx.strokeStyle = this.lens.back;
    ctx.lineWidth = 7;
    ctx.beginPath();
    ctx.arc(c, c, R * 0.88, 0.42, 1.12);
    ctx.stroke();
    ctx.restore();
  }

  /**
   * The taper for one specular arc: a linear gradient laid along the chord
   * between the arc's two ends, transparent at both and solid across the
   * middle.
   *
   * Stroking an arc with a gradient aligned to its own chord is the cheapest
   * honest taper there is — one gradient, no per-segment loop — and because
   * the chord is always shorter than the arc the ends fade slightly early,
   * which is the right direction to err. A specular with visible ends is a
   * painted line.
   */
  _specularGradient(c, radius, from, to, alpha) {
    const g = this.ctx.createLinearGradient(
      c + radius * Math.cos(from),
      c + radius * Math.sin(from),
      c + radius * Math.cos(to),
      c + radius * Math.sin(to)
    );
    g.addColorStop(0, rgba(this.sheen, 0));
    g.addColorStop(0.35, rgba(this.sheen, alpha));
    g.addColorStop(0.62, rgba(this.sheen, alpha));
    g.addColorStop(1, rgba(this.sheen, 0));
    return g;
  }

  /**
   * The core: the glass lighting from inside.
   *
   * This is the layer that makes the orb read as a lit body rather than as a
   * printed disc, and it is the one thing on the orb driven by the audio
   * *continuously* rather than by state alone. Two terms, and the split is
   * the whole behaviour the brief asked for:
   *
   *   `active`  — the agent is in play at all. Lights the core and holds it
   *               lit for the whole turn, so an orb that is on never drops to
   *               nothing between syllables.
   *   `level`   — the envelope, right now. Rides on top, so the core visibly
   *               breathes with the voice and pushes hardest on a stressed
   *               syllable.
   *
   * `active` is the floor and `level` is the swing, which is why the two
   * alphas are separate numbers rather than one multiplied pair: a silent
   * invited agent still has to look switched on.
   *
   * Three passes. A wide soft bloom that reaches most of the way to the seat
   * ring; a tight hot centre under the mark; and an inner shadow around the
   * inside of the rim, which is the one that actually sells glass — a lens
   * is darker at its edge than at its middle, and without it the other two
   * read as a lamp behind paper.
   *
   * On a light ground none of this is light. The same three gradients in the
   * same places *deepen* the disc instead, because painting a colour at alpha
   * over near-white is subtractive for free — see the long note at the top.
   * Do not add a `lighter` composite to "fix" light mode; that is what makes
   * it disappear.
   */
  _core(ctx, c, level, active) {
    const lit = active * (0.45 + 0.55 * level);
    if (lit < 0.004) return;

    ctx.save();
    // Everything here is clipped to the disc. The core is what is *inside*
    // the glass, and a bloom spilling past the seat ring would collide with
    // the meniscus and read as the orb leaking.
    ctx.beginPath();
    ctx.arc(c, c, R, 0, TAU);
    ctx.clip();

    /*
     * The bloom's falloff is deliberately shallow across the first two
     * thirds, and that is a fix rather than a preference.
     *
     * It used to be a steep gradient — full at the centre, 30% by 0.55R,
     * nothing by the rim. On paper that put almost all of the glow *under
     * the Speechmatics mark*, which is 228px wide and painted last: by the
     * radius where the logo stops covering it the alpha had already fallen
     * to around 0.06, so a core nominally at 0.40 arrived on screen as a
     * barely-tinted ring. Speaking read as "slightly greyer" rather than as
     * lit, and raising the peak only lit the part nobody can see.
     *
     * Holding three quarters of the value out to 0.45R and most of a third
     * to 0.75R puts the colour in the annulus that is actually visible,
     * between the mark and the refraction band.
     */
    const peak = (P.core + P.coreAudio * level) * active;
    const bloom = ctx.createRadialGradient(c, c, 0, c, c, R * 0.95);
    bloom.addColorStop(0, rgba(this.glow, peak));
    bloom.addColorStop(0.45, rgba(this.glow, peak * 0.78));
    bloom.addColorStop(0.75, rgba(this.glow, peak * 0.34));
    bloom.addColorStop(1, rgba(this.glow, 0));
    ctx.fillStyle = bloom;
    ctx.fillRect(c - R, c - R, R * 2, R * 2);

    // The hot centre, sized to sit just inside the mark so the logo is lit
    // from behind rather than washed out.
    const hot = ctx.createRadialGradient(c, c, 0, c, c, MARK_SIZE * 0.42);
    hot.addColorStop(0, rgba(this.glow, P.coreAudio * lit * 0.8));
    hot.addColorStop(1, rgba(this.glow, 0));
    ctx.fillStyle = hot;
    ctx.fillRect(c - R, c - R, R * 2, R * 2);

    // The inner shadow. Zero until most of the way out, then rising sharply —
    // a thickness at the edge of a lens, not a vignette over the whole face.
    const edge = ctx.createRadialGradient(c, c, R * 0.6, c, c, R);
    edge.addColorStop(0, rgba(this.shade, 0));
    edge.addColorStop(1, rgba(this.shade, P.inner * (0.5 + 0.5 * active)));
    ctx.fillStyle = edge;
    ctx.fillRect(c - R, c - R, R * 2, R * 2);

    ctx.restore();
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

  /**
   * Build one closed path at a given radial offset from the ring.
   *
   * Shared by the meniscus, its bloom and the peak trace, because all three
   * are the same curve at different distances — generating each separately
   * would let them drift apart by a rounding error and read as three rings
   * that nearly agree, which is worse than one ring.
   *
   * `src` is a smoothed band array; `offset` shifts the whole curve outward.
   */
  _ringPath(c, src, base, reach, offset) {
    const path = new Path2D();
    for (let i = 0; i < RING_POINTS; i++) {
      // Position along the band array, wrapped. `i / RING_POINTS` is the
      // fraction of a full turn, so band 0 lands exactly at twelve o'clock
      // and band 31 arrives back at it from the left.
      const f = (i / RING_POINTS) * BANDS;
      const k = Math.floor(f);
      const v = catmull(
        src[(k - 1 + BANDS) % BANDS],
        src[k % BANDS],
        src[(k + 1) % BANDS],
        src[(k + 2) % BANDS],
        f - k
      );
      const rad = base + offset + reach * Math.max(0, v);
      const a = -Math.PI / 2 + (i * TAU) / RING_POINTS;
      const x = c + rad * Math.cos(a);
      const y = c + rad * Math.sin(a);
      if (i === 0) path.moveTo(x, y);
      else path.lineTo(x, y);
    }
    path.closePath();
    return path;
  }

  /**
   * The spectrum, as one continuous closed curve around the whole orb.
   *
   * Angle is frequency, once around: bass at twelve o'clock, mids down the
   * right, sibilance arriving back at twelve from the left. Radius is that
   * band's amplitude. The ring's *shape* is the voice; its depth is how loud
   * that voice is. Nothing travels and nothing is mirrored — see the long
   * note at the top for why the mirror went and what `RING_SMOOTH` is paying
   * for.
   *
   * Four passes, outward in paint order:
   *
   *   bloom     a wide, very low alpha stroke of the same curve. The orb's
   *             main material effect and the thing that reads as light caught
   *             in a thick edge rather than as a line drawing of one.
   *   fill      the band between the seat ring and the curve, at a low alpha.
   *             This is new with the curve and is most of why it reads as
   *             liquid: a stroke alone is a wire, a filled meniscus is a
   *             volume of something sitting on the glass.
   *   stroke    the crisp reading, and the only pass at full alpha.
   *
   * **There is no peak trace, and that is a deliberate removal.** The caps
   * survive in `_bands` and in `this.peak` — they are cheap and a future pass
   * may want them — but nothing draws them. Carried over from the bar design
   * they were sixty-four 5px ticks, each marking one band, and they held the
   * silhouette of a phrase for about a second after the bands dropped. Drawn
   * as *one continuous ring* they stop being marks and become a second
   * closed curve, and that curve is very nearly a circle: every band's
   * running maximum converges during a syllable, and `PEAK_FALL` only sheds
   * about 0.11 across a 4Hz syllable cycle, so the trace parks near the top
   * of the corona's reach and stays there. Measured on the probe it sat at
   * r=341 against a live curve at 270-290 — brighter, further out, and
   * circular. The thing the audience read as "the waveform" was the one
   * element guaranteed not to have a shape.
   */
  _waveform(ctx, c, t, colour, active) {
    // A slow, shallow drift applied to the whole ring, independent of any
    // audio. Two percent, and the only reason it is here is that a perfectly
    // static ring on a 12m wall looks like a frozen frame.
    const breath = 1 + 0.02 * Math.sin(t * 0.52 * TAU * 0.5);
    const reach = BAR_MAX * this.amp * breath;
    const base = BAR_BASE + BAR_FLOOR;

    smoothRing(this.band, this.ring);
    // Stretch the ring between its recent floor and its recent peak, so the
    // shape fills the depth the state allows whatever the absolute input
    // level is. See NORM_FALL and NORM_RISE.
    const lo = this.lo;
    const span = Math.max(NORM_SPAN, this.norm - lo);
    for (let i = 0; i < BANDS; i++) {
      this.ring[i] = clamp01((this.ring[i] - lo) / span);
    }

    const curve = this._ringPath(c, this.ring, base, reach, 0);
    const weight = WEIGHT_MIN + (WEIGHT_MAX - WEIGHT_MIN) * Math.pow(active, WEIGHT_GAMMA);
    const ink = P.bar + P.barGain * active;

    // Round joins now that this is a curve rather than ticks. The brand rules
    // out rounded bar *ends* — a cap that lengthens a reading — and there are
    // no ends on a closed path. Mitre here would spike wherever a band turns
    // sharply, which is exactly where the signal is most interesting.
    ctx.lineJoin = "round";
    ctx.lineCap = "butt";

    if (active > 0.02) {
      ctx.strokeStyle = rgba(colour, P.barBloom * active);
      ctx.lineWidth = weight * 3.2;
      ctx.stroke(curve);
    }

    // The meniscus fill, between the seat ring and the curve. Drawn with
    // `evenodd` against a disc at the seat radius so it is genuinely an
    // annulus — filling the curve alone would paint over the mark.
    if (active > 0.02) {
      const inner = new Path2D();
      inner.arc(c, c, BAR_BASE, 0, TAU);
      const annulus = new Path2D();
      annulus.addPath(curve);
      annulus.addPath(inner);
      const g = ctx.createRadialGradient(c, c, BAR_BASE, c, c, base + reach);
      g.addColorStop(0, rgba(colour, P.menisc * active));
      g.addColorStop(1, rgba(colour, 0));
      ctx.fillStyle = g;
      ctx.fill(annulus, "evenodd");
    }

    ctx.strokeStyle = rgba(colour, ink);
    ctx.lineWidth = weight;
    ctx.stroke(curve);
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
