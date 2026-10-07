"""Persona schema.

Personas are structured data, not prose, because the floor controller reads
them at runtime to make moment-to-moment decisions. The system prompt is
generated from this — the data is the source of truth.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

# Audio tags the TTS model *performs* rather than reads aloud, written inline
# as `[laughs]`. The vendor's set is free text, not an enumeration — the model
# interprets whatever is in the brackets — so this is our closed list, and
# being closed is the whole safety property. `sanitise()` keeps exactly these
# and destroys every other bracket expression, which is what stops a model
# that has learned the mechanism from reaching the documented tags that would
# end the show: `[gunshot]`, `[applause]`, `[sings]`, `[strong French accent]`.
#
# Measured on `eleven_v3_conversational` (5 Oct 2026), each adds roughly
# 0.5-1.0s of performed audio. Deliberately excluded:
#
# * `[laughs harder]` — +2.0s of stage time from one token.
# * `[whispers]` — a whisper into a PA for 400 people is a dead spot, and the
#   vendor warns a serious voice will not do it convincingly.
# * `[hesitates]` — collides with the hesitation rule in `prompts.GUARDRAILS`,
#   which deliberately wants hesitation as a *spoken word* ("let me think")
#   and not as a direction. A tag here would give the model a way to satisfy
#   that instinct without saying anything, which is the opposite of the intent.
# * every other sound effect and accent tag.
#
# Stored without brackets; canonical form is lowercase, single-spaced.
AUDIO_TAGS: frozenset[str] = frozenset(
    {
        "laughs",
        "chuckles",
        "sighs",
        "exhales",
        "dryly",
        "clears throat",
        "Yorkshire accent",
        "London accent",
        "irish accent",
    }
)

# `ACCENT_TAGS` is the subset of the above that is a standing characteristic
# of a voice, not a performance cue — and that difference is why it is not
# handled the way the rest of `AUDIO_TAGS` is.
#
# Flash's `similarity_boost` used to pin a cloned voice close to its
# reference recording's accent; v3 does not implement that setting at all
# (dropped in `panel_runtime.tts`'s `TTSConfig`), and every persona's
# `stability` separately collapsed onto the same 0.5 preset (`_preset_stability`,
# same module) — between the two, Dexter's Northern accent read as flattened
# towards the model's generic defaults. (Melia's `strong irish accent` was
# tried the same way at stability 0.0 Creative and dropped 5 Oct 2026 — the
# tag did not move this particular voice. Re-added 6 Oct 2026 as plain
# "irish accent", same stability; untested on the real voice since the
# phrasing change — if it still doesn't move, recasting `voice_id` to a voice
# already labelled Irish is the more likely fix than more tag or stability
# tuning, see `personas/melia.yaml`. A `pace` field and `[rapid-fire]` tag
# were tried the same way for Wayne the same day, to compensate for v3
# dropping `speed` entirely — measured as working, ~15% shorter audio for the
# same text, but it read as shouting rather than brisk and was reverted; see
# `personas/wayne.yaml`. That revert was of the tag, not of the mechanism:
# `PACE_TAGS` below is the same field tried again on 2026-10-07 with
# `[briskly]`, and it is live for Wayne.)
#
# The vendor's fix for an accent is an inline tag, but rendering it into the
# "sounds your voice can actually make" prompt block (below) is the wrong
# mechanism: that block is deliberately written for an occasional performance
# choice — "at most one in a turn, most turns none" — and an accent that shows
# up in a minority of turns is not standing, it is a tic.
#
# So `Persona.accent` is not rendered into any prompt, and the model never
# writes it. `panel_runtime.tts` reads it directly and prepends it to every
# push, deterministically — the same reasoning as the introduction text being
# fixed rather than generated: the failure mode of leaving it to the model is
# a turn where it quietly does not show up, and there is no way to notice
# that without listening to the whole show.
ACCENT_TAGS: frozenset[str] = frozenset({"Yorkshire accent", "London accent", "irish accent"})

# `PACE_TAGS` is the accent mechanism above applied to a different standing
# property: how fast a voice talks. Not an accent, and deliberately its own
# field rather than a widening of `accent` — a persona could plausibly want a
# regional voice *and* a brisk one, and one field cannot carry both.
#
# It is a tag at all because v3 has no numeric pace control to use instead,
# and that is the vendor's position rather than our inference (checked
# 2026-10-07): the dialogue WebSocket's own schema says "only `stability` is
# supported for `eleven_v3` dialogue models", the settings page says outright
# that "Speed is not available for the Eleven v3 model", and the prompting
# guide names audio tags and punctuation as *the* way to control pacing on
# v3. Flash's `speed` is still accepted on this endpoint and then ignored
# (see `panel_runtime.tts.TTSConfig`'s docstring). So an inline tag is not the
# convenient lever here, it is the only one.
#
# `[rapid-fire]` was the first attempt at it, for Wayne on 5 Oct 2026 at
# stability 0.0: measurably faster (15.1% shorter audio for the same text) but
# it read as shouting rather than brisk, and was reverted. `[briskly]` is a
# different tag tried fresh on 2026-10-07 — compared by ear against `[rushed]`,
# `[rapid-fire]` and several stability variations on Wayne's real introduction
# text, and preferred. See `personas/wayne.yaml` for what that comparison does
# and does not establish.
#
# Not a member of `AUDIO_TAGS`, and the model is never told it exists: the
# whole point is that the pace does not depend on the model remembering to ask
# for it. `sanitise()` stripping a `[briskly]` the model wrote anyway is the
# correct outcome, not a gap.
#
# Stored without brackets; canonical form is lowercase, single-spaced.
PACE_TAGS: frozenset[str] = frozenset({"briskly"})


class Beat(BaseModel):
    """One discussion topic this persona has prepared material for.

    Ricky cues a beat by topic, not by exact wording — he is a live human and
    he paraphrases — so the model has to recognise that the conversation has
    arrived rather than pattern-match his sentence. The material is a target
    to answer against in this persona's own voice, never a line to recite, and
    the mechanism is never named out loud (`prompts.build_system_prompt`).
    """

    cue: str = Field(description="The topic that raises this beat, described not quoted.")
    material: list[str] = Field(
        default_factory=list,
        description="Substance to answer from, in your own voice — a target, not a script.",
    )
    reacting_to: dict[str, str] = Field(
        default_factory=dict,
        description="Persona id -> how to handle that colleague's take on this same beat.",
    )


class Persona(BaseModel):
    id: str
    name: str
    job_title: str
    employer: str = Field(
        description="Always fictional. Never a real organisation — see FEASIBILITY.md 4.1."
    )
    background: str
    stance: str = Field(description="This agent's position on AI adoption.")

    # --- recurring material ---
    # The "approved knowledge" half of docs/beat-sheet.md #10 #5 (signed off
    # 16 Sept 2026, subject to change on revision): specific first-person
    # experiences this agent is allowed to claim, checked against the beat
    # sheet's content rules (no Speechmatics claims, no invented stats, no
    # real orgs). Reuse across turns — not a fresh example each time — is
    # what makes recurrence read as a person rather than an opinion
    # generator. See docs/beat-sheet.md "Anecdote spines".
    anecdotes: list[str] = Field(default_factory=list)

    # Public, checked figures this agent may quote out loud. The beat sheet's
    # "no invented statistics" rule bans *invented* numbers, not real ones —
    # and what made Dexter and Melia read as wishy-washy (director's note,
    # 21 Sept 2026) was having no numbers at all in two domains that are
    # inherently quantitative: fleet-scale deployment and adoption
    # governance. Every entry here was checked against a published source
    # before it was written down and carries its attribution in the text, in
    # the loose form a practitioner would actually say it out loud. Still
    # subject to GUARDRAILS: about the field, never a named vendor, model,
    # product or customer. Wayne's is deliberately empty — his confidence is
    # temperamental rather than statistical, and that contrast is what Melia
    # gets to point out (docs/beat-sheet.md, Wayne "Never").
    citable_figures: list[str] = Field(default_factory=list)

    # Per-persona speaking discipline. `communication_style` says how they
    # sound; this says what they must not do with a turn. Added 21 Sept 2026:
    # Dexter was closing on the moral of an anecdote instead of opening on
    # the anecdote, and Melia was narrating her own position in the
    # conversation ("I'll wait to hear where Ricky's pointing this") rather
    # than holding one. Both are delivery faults, not stance or style faults,
    # so neither was fixable by editing `stance` or `communication_style`.
    delivery: list[str] = Field(default_factory=list)

    # Prepared material scoped to a discussion beat, keyed by beat id ("q1",
    # "q2", ...). Unlike `anecdotes`, which is standing material the persona
    # reuses anywhere, a beat only applies once the conversation has reached
    # its topic — and the prompt has to say so, because nothing else stops the
    # model reaching for it in an unrelated turn (there is no sampling knob to
    # lean on; see `panel_runtime.brains.BrainConfig`).
    beats: dict[str, Beat] = Field(default_factory=dict)

    # --- fixed opening ---
    # Word-for-word, spoken every time, never generated. The introduction
    # round used to ask the model for this live and it once came back
    # completely empty on stage-adjacent testing — an agent granted the
    # floor with nothing under it. Fixed text removes the failure class
    # outright rather than tuning around it, and it is what FEASIBILITY.md
    # 4.5 wants the opening to become anyway (pre-rendered to audio) — see
    # `FloorController._grant_introduction`, which is the only thing that
    # ever reads this field. Two or three sentences, written to be read
    # aloud. Deliberately leans on its position in the running order —
    # "I'll go first" / "I suppose I can go next" / "saved the best for
    # last" — rather than standing alone, because that running order is
    # itself fixed (`FloorController._start_introductions`: cast directory
    # order, Dexter/Melia/Wayne, identical every run). Renaming a persona
    # file or reordering the cast changes who speaks when without touching
    # this text, so any change to speaking order must be carried into every
    # `introduction` string by hand.
    introduction: str = Field(
        min_length=1,
        description="Fixed, verbatim text for the introduction round. Never sent to a model.",
    )

    # This agent's position in the introduction round — deliberately its own
    # number, not `PanelCast`'s own (alphabetical-by-filename) order. That
    # directory order is shared with the video wall's lane order
    # (`panel_display.wall.WallState.for_cast`), and reordering the intro
    # script must not drag the wall's lanes along with it. Lower speaks
    # first. See `FloorController._start_introductions`.
    intro_position: int = Field(description="0-based speaking position in the introduction round.")

    # --- fixed closing exchange ---
    # A second one-shot fixed round, scripted the same way as `introduction`
    # above and run immediately after it (`FloorController._start_closing`):
    # "We discussed this." / "Repeatedly." / "[sighs] here we go again." —
    # its own beat, not a continuation of who-spoke-when, so it gets its own
    # order rather than reusing `intro_position`. `closing_position=None`
    # means this persona has no line in the exchange and is skipped.
    closing_position: int | None = Field(
        default=None, description="0-based position in the fixed post-introduction exchange."
    )
    closing_line: str = Field(
        default="",
        description="Fixed, verbatim text for the post-introduction exchange. Never sent to a model.",
    )

    # --- voice ---
    voice_id: str
    # Per-persona overrides merged over `panel_runtime.tts.TTSConfig`'s
    # defaults for this voice only (see `ElevenLabsTTS`'s `voice_overrides`).
    # Keys are ElevenLabs `voice_settings` field names — `stability`,
    # `similarity_boost`, `speed`. Deliberately not `style`: ElevenLabs docs
    # note it costs an extra generation pass, which is the one thing this
    # panel's TTS layer is built to avoid (see tts.py's module docstring on
    # TTFB). An empty dict means "use the engine defaults, no override."
    voice_settings: dict[str, float] = Field(default_factory=dict)
    # A fixed output trim applied in the mixer (`panel_runtime.mixer.Mixer`),
    # not sent to ElevenLabs — `voice_settings` above covers everything the
    # API takes, and loudness is not one of those knobs. This is this
    # persona's resting gain: duck and resume ramp relative to it, so a
    # trimmed voice still ducks for a backchannel and comes back to its own
    # level rather than everyone else's. 0.0 is unity, i.e. untouched.
    output_gain_db: float = 0.0
    # Which of `AUDIO_TAGS` this persona is told it may use, bracketless. A
    # subset per persona rather than the whole list to everyone: the tags are
    # characterisation, and a bombastic voice laughing is as specific as a dry
    # one sighing. An empty list means this persona is never told the
    # mechanism exists — `sanitise()` still polices the full list regardless,
    # because the prompt is guidance and the sanitiser is the guarantee.
    audio_tags: list[str] = Field(default_factory=list)
    # A standing vocal characteristic, read by `panel_runtime.tts` and never
    # by a prompt — see `ACCENT_TAGS`' comment for why this is not folded
    # into `audio_tags`. `None` means this voice's engine-default rendering,
    # i.e. unchanged.
    accent: str | None = Field(default=None)
    # A standing delivery speed, on exactly the same terms as `accent` above:
    # read by `panel_runtime.tts`, prepended to every push, never written by a
    # prompt. Independent of `accent` so a voice can carry both — see
    # `PACE_TAGS`. `None` means this voice's engine-default pace, i.e.
    # unchanged.
    pace: str | None = Field(default=None)
    communication_style: str

    @field_validator("anecdotes", "citable_figures", mode="before")
    @classmethod
    def _drop_blank_entries(cls, value: object) -> object:
        """`- >` with nothing under it is YAML for one empty string, not an
        empty list — and a one-entry list is truthy, so it renders the whole
        "Public figures you are expected to use" block with a blank bullet
        under it. Measured on 2026-10-07: Melia and Dexter both carried one,
        and Melia invented a Brussels legislative deadline to fill it.
        """
        if isinstance(value, list):
            return [entry for entry in value if not (isinstance(entry, str) and not entry.strip())]
        return value

    @field_validator("audio_tags")
    @classmethod
    def _tags_are_allowlisted(cls, value: list[str]) -> list[str]:
        """A typo here would be stripped at runtime and silently do nothing.

        Cast data is the one place this can be caught loudly, so it is — the
        failure otherwise is a persona that simply never laughs and no error
        anywhere saying why.
        """
        unknown = [tag for tag in value if tag not in AUDIO_TAGS]
        if unknown:
            raise ValueError(
                f"audio_tags not in the allowlist: {unknown}. Allowed: {sorted(AUDIO_TAGS)}"
            )
        return value

    @field_validator("accent")
    @classmethod
    def _accent_is_allowlisted(cls, value: str | None) -> str | None:
        """Same failure shape as `audio_tags` above: a typo here is a persona
        that quietly never gets its accent applied, with nothing to say why."""
        if value is not None and value not in ACCENT_TAGS:
            raise ValueError(
                f"accent not in ACCENT_TAGS: {value!r}. Allowed: {sorted(ACCENT_TAGS)}"
            )
        return value

    @field_validator("pace")
    @classmethod
    def _pace_is_allowlisted(cls, value: str | None) -> str | None:
        """Same failure shape as `accent` above: a typo here is a persona that
        quietly never gets its pace applied, with nothing to say why."""
        if value is not None and value not in PACE_TAGS:
            raise ValueError(f"pace not in PACE_TAGS: {value!r}. Allowed: {sorted(PACE_TAGS)}")
        return value

    speech_tics: list[str] = Field(
        default_factory=list,
        description="Short verbal habits. Distinctiveness survives a PA better than timbre.",
    )

    # --- floor behaviour ---
    # Two knobs lived here until 21 Sept 2026 and both were removed as dead:
    # `interrupt_tendency`, which only ever fed `interrupt_score()` and went
    # with agent-to-agent interrupts, and `yield_tendency`, which was set in
    # every persona file and read by nothing at all. Neither was rendered into
    # a prompt, so neither could affect a live run. Deleted rather than kept,
    # so nobody tunes a number expecting an effect it cannot have.
    #
    # A persona field earns its place by being read — by `prompts.py`, the
    # floor, or the sim's stub brain. `topics_of_authority` is the model for
    # this: rendered into every prompt, so editing it changes the show.
    topics_of_authority: list[str] = Field(default_factory=list)

    # --- length discipline ---
    # The most common failure mode of an LLM panel is a 45-second monologue.
    # Brevity is a prompt instruction (GUARDRAILS in prompts.py) — turn length
    # has no orchestrator-enforced ceiling; the moderator handles the rest live.
    verbosity: str = "low"

    # --- relationships ---
    # Generic disagreement sounds generic. This is what makes agent-to-agent
    # exchanges read as colleagues rather than as two models (FEASIBILITY.md 4.3).
    relationships: dict[str, str] = Field(default_factory=dict)

    # --- guardrails ---
    forbidden_topics: list[str] = Field(default_factory=list)

    # --- pronunciation ---
    # Belt-and-braces for the STT misrecognition class of failure (e.g.
    # "Melia" transcribed as "Amelia", which the address regex's leading
    # `\b` then can't match): `extra_aliases` widens what the *floor*
    # accepts in text; `sounds_like` biases what *STT* emits in the first
    # place. Both are facts about the persona's name, not about a
    # transcription session or a regex — CLAUDE.md "personas are data".
    #
    # The YAML key is `aliases`; the Python attribute is `extra_aliases`
    # so it doesn't collide with the `aliases()` method below, whose
    # signature/return type other code (`floor.py`) depends on.
    extra_aliases: list[str] = Field(
        default_factory=list,
        alias="aliases",
        description=(
            "Extra written spellings a human (or a misrecognising STT "
            "engine) might use for this persona, e.g. 'amelia' for "
            "Melia. Keep conservative — a false match hands the floor "
            "to the wrong agent."
        ),
    )
    sounds_like: list[str] = Field(
        default_factory=list,
        description=(
            "Phonetic hints for this persona's canonical name, fed to "
            "STT as `additional_vocab` (see panel_runtime.stt). Empty "
            "means no vocabulary bias is requested for this persona."
        ),
    )

    @property
    def canonical_name(self) -> str:
        """The spoken form STT vocabulary bias should target.

        Every persona's `name` is now the single word a human on stage
        actually says, so this is `name` verbatim. It stays a named concept
        rather than callers reaching for `name` directly because what STT is
        biased towards (`panel_runtime.stt.vocab_from_cast`) and what the
        floor will accept in the resulting transcript (`aliases()`) have to
        agree, and that agreement is the thing worth naming. Anything a
        human might say *other* than this belongs in `extra_aliases`, where
        it is declared rather than derived.
        """
        return self.name

    def aliases(self) -> list[str]:
        """Names a human might use to address this agent directly."""
        aliases = {self.name.lower(), self.id.lower()}
        aliases.update(alias.lower() for alias in self.extra_aliases)
        return list(aliases)


def _validate_fixed_round_ordering(personas: dict[str, Persona]) -> None:
    """Catch a mis-authored cast loudly rather than at the one moment on
    stage the two fixed rounds run.

    `intro_position` and `closing_position` are hand-maintained per persona
    file rather than derived, so nothing stops two files claiming the same
    slot or a `closing_line` going unscripted — both would otherwise surface
    only as `_start_introductions`/`_start_closing` silently collapsing two
    agents onto one position, or an agent granted an empty line.
    """
    intro_positions = [p.intro_position for p in personas.values()]
    if len(set(intro_positions)) != len(intro_positions):
        raise ValueError(f"duplicate intro_position across cast: {sorted(intro_positions)}")
    closing_positions = [p.closing_position for p in personas.values() if p.closing_position is not None]
    if len(set(closing_positions)) != len(closing_positions):
        raise ValueError(f"duplicate closing_position across cast: {sorted(closing_positions)}")
    for persona in personas.values():
        if (persona.closing_position is None) != (not persona.closing_line):
            raise ValueError(
                f"{persona.id}: closing_position and closing_line must be set together"
            )


class PanelCast(BaseModel):
    personas: dict[str, Persona]

    @classmethod
    def from_dir(cls, directory: Path | str) -> PanelCast:
        # Coerced rather than required. Every call site in the repo passes a
        # Path already (argparse `type=Path`, or a module constant), so this is
        # purely for the hand-typed one-liner in a shell or a docstring —
        # `from_dir("personas")` used to fail with a bare AttributeError about
        # `str` having no `glob`, which says nothing about what went wrong.
        directory = Path(directory)
        personas: dict[str, Persona] = {}
        for path in sorted(directory.glob("*.yaml")):
            data = yaml.safe_load(path.read_text())
            persona = Persona.model_validate(data)
            personas[persona.id] = persona
        if not personas:
            raise ValueError(f"no persona YAML files found in {directory}")
        _validate_fixed_round_ordering(personas)
        return cls(personas=personas)

    def ids(self) -> tuple[str, ...]:
        return tuple(self.personas)

    def __getitem__(self, agent_id: str) -> Persona:
        return self.personas[agent_id]
