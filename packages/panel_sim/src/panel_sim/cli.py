"""Text-mode panel harness.

Type as Ricky; watch the floor controller arbitrate. No audio, no venue, no AV
team, no credentials in stub mode. This is where personas get written and
thresholds get tuned, in parallel with the audio pipeline being built — see
FEASIBILITY.md 5.2.

    uv run panel-sim              # offline stub brains
    uv run panel-sim --live       # real model behind the personas
    uv run panel-sim --replay recordings/2026-10-07.jsonl

Virtual clock: the sim advances time in proportion to words spoken, so runs are
reproducible and a rehearsal replays identically.

**The sim always uses the regex address detector**, never the LLM one
(`FloorConfig.llm_address_detection` is left at its default here, and there is
no `--llm-address` flag). `panel_sim` depends on `panel_core` alone and must
keep running offline with stub brains, so importing `panel_runtime.address`
— and with it livekit and sounddevice — is not an option. Do not read a
`panel-sim` run as a rehearsal of the classifier: it exercises everything
downstream of the invitation and nothing about how the invitation was chosen.
The event log a live run writes does carry its `AddressDetected` events, and
with the flag off `panel_core` ignores them and lets the regex decide again.
That is deliberate — it is what would let one recorded session be scored
through both detectors — but note that the `--replay` flag advertised above is
not built yet (there is no such argument), so nothing exercises it today.
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from pathlib import Path

from panel_core import (
    HUMAN,
    AgentProposal,
    AgentSpeechEnded,
    AgentSpeechStarted,
    CueModerator,
    FloorConfig,
    FloorController,
    HandsRaised,
    HumanSpeechStarted,
    OperatorAction,
    OperatorCommand,
    PanelCast,
    PanelState,
    RequestProposals,
    StartSpeech,
    StopSpeech,
    Tick,
    TranscriptUpdated,
    TurnYielded,
)
from rich.console import Console
from rich.table import Table

from .brains import Brain, ClaudeBrain, StubBrain

WORDS_PER_SECOND = 2.8  # rough speaking rate, for the virtual clock

console = Console()


class Simulation:
    def __init__(self, cast: PanelCast, brain: Brain, config: FloorConfig, log: Path | None):
        self.cast = cast
        self.brain = brain
        self.fc = FloorController(cast, config)
        self.state = PanelState.for_agents(cast.ids())
        self.clock = 0.0
        self.log = log
        self.pool = ThreadPoolExecutor(max_workers=len(cast.ids()))
        if log:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("")

    # ------------------------------------------------------------------ plumbing

    def emit(self, event) -> None:
        """Feed one event through the floor controller and act on its commands."""
        self._record(event)
        self.state, commands = self.fc.reduce(self.state, event)
        for command in commands:
            self._execute(command)

    def _record(self, event) -> None:
        if not self.log:
            return
        payload = {"type": type(event).__name__}
        if is_dataclass(event):
            payload |= {k: _jsonable(v) for k, v in asdict(event).items()}
        with self.log.open("a") as fh:
            fh.write(json.dumps(payload) + "\n")

    def _execute(self, command) -> None:
        match command:
            case RequestProposals():
                self._gather(command.agents)

            case StartSpeech():
                persona = self.cast[command.agent]
                if command.lead_in_s:
                    # Introduction round only — a silent beat before this
                    # agent's first word (`StartSpeech.lead_in_s`). No real
                    # audio here, so there is nothing to sleep through; just
                    # carry it into the virtual clock so replays stay
                    # reproducible.
                    console.print("  [dim]…[/]")
                    self.clock += command.lead_in_s
                console.print(
                    f"\n[bold cyan]{persona.name}[/] [dim](turn {command.turn_id})[/]\n"
                    f"  {command.utterance}"
                )
                self.emit(AgentSpeechStarted(t=self.clock, agent=command.agent))
                self.clock += len(command.utterance.split()) / WORDS_PER_SECOND
                self.emit(AgentSpeechEnded(t=self.clock, agent=command.agent, completed=True))

            case StopSpeech():
                name = self.cast[command.agent].name
                console.print(
                    f"  [red]⏹ {name} cut off[/] [dim]({command.reason.value}, "
                    f"overlap {command.overlap_ms}ms)[/]"
                )

            case HandsRaised():
                hands = "  ".join(
                    f"{self.cast[a].name} [dim]{score:.2f}[/]" for a, score in command.agents
                )
                console.print(f"  [yellow]✋ wants in:[/] {hands} [dim](not invited)[/]")

            case CueModerator():
                console.print(f"  [magenta]▸ hand back to Ricky ({command.reason})[/]")

    def _gather(self, agents: tuple[str, ...]) -> None:
        """Speculative proposals, in parallel — as production will do.

        `epoch` and `input_t` are snapshotted together, up front, because they
        are one round's identity: `FloorController._ask_for_proposals` set both
        in the same `reduce()` call that produced the command being executed
        here, and `emit()` has already installed that state. They must not be
        re-read per agent — emitting one proposal can run a whole agent turn
        synchronously (see `_execute`), which opens another round and moves
        both, and half this round would then be labelled as the next one.

        `input_t` is what `FloorController._stale` measures: the transcript
        timestamp these lines are answers to, not the moment they arrived.
        """
        epoch = self.state.speculation_epoch
        input_t = self.state.last_proposal_request_t
        futures = {
            agent: self.pool.submit(self.brain.propose, self.cast[agent], self.state)
            for agent in agents
        }
        for agent, future in futures.items():
            try:
                result = future.result(timeout=30)
            except Exception as exc:  # noqa: BLE001 — a dead brain must never take the panel down
                console.print(f"  [red]✗ {agent} brain failed: {exc}[/]")
                continue
            if result is None:
                continue
            utterance, signals = result
            self.emit(
                AgentProposal(
                    t=self.clock,
                    agent=agent,
                    utterance=utterance,
                    signals=signals,
                    epoch=epoch,
                    input_t=input_t,
                )
            )

        # An invitation worth more than one turn keeps the floor with the panel.
        # Re-opening arbitration is runtime behaviour, not floor logic: the core
        # stays a reducer and never schedules anything for itself.
        invitation = self.state.invitation
        if (
            self.state.speaking is None
            and self.state.floor_holder is None
            and invitation is not None
            and invitation.is_live()
        ):
            self.emit(TurnYielded(t=self.clock))
            # That re-arbitration can arm the beat too, so it needs resolving
            # here as well as after a human turn. `_cue_overdue` emits only a
            # cue and a repaint, so this cannot re-enter `_gather`.
            self._settle()

    # ---------------------------------------------------------------------- turns

    def human_turn(self, text: str) -> None:
        self.emit(HumanSpeechStarted(t=self.clock))
        self.emit(TranscriptUpdated(t=self.clock, speaker=HUMAN, text=text, is_final=True))
        self.clock += len(text.split()) / WORDS_PER_SECOND
        self.emit(TurnYielded(t=self.clock))
        self._settle()

    def _settle(self) -> None:
        """Run the virtual clock past any beat the floor is holding.

        A named agent with no answer no longer cues the moderator immediately —
        the floor holds `FloorConfig.invited_agent_grace_s` for the answer that
        is probably still being written, and `Tick` is what resolves the wait
        (see `FloorController._cue_overdue`). The live runtime ticks every
        100ms; this harness has no clock of its own, so without this a silent
        named agent would leave the sim sitting on an armed beat that nothing
        ever fires, and `▸ hand back to Ricky` would simply never print.

        Gathering here is synchronous, so by this point every brain has already
        returned: the beat can only be resolved one way and there is nothing to
        be gained by paying it out in real time.
        """
        if self.state.awaiting_agent is None:
            return
        self.clock += self.fc.config.invited_agent_grace_s
        self.emit(Tick(t=self.clock))

    def show_scores(self) -> None:
        from panel_core import floor_priority

        table = Table(title="current proposals", show_lines=False)
        for col in ("agent", "priority", "rel", "urg", "dis", "exp", "line"):
            table.add_column(col)
        for agent, proposal in self.state.proposals.items():
            s = proposal.signals
            score = floor_priority(
                s,
                self.cast[agent],
                now=self.clock,
                last_spoke_at=self.state.agents[agent].last_spoke_at,
                config=self.fc.config,
            )
            table.add_row(
                self.cast[agent].name,
                f"{score:.2f}",
                f"{s.relevance:.2f}",
                f"{s.urgency:.2f}",
                f"{s.disagreement:.2f}",
                f"{s.expertise:.2f}",
                proposal.utterance[:48],
            )
        console.print(table if self.state.proposals else "[dim]no live proposals[/]")


def _describe(invitation) -> str:
    if invitation is None:
        return "closed"
    who = invitation.agent or "panel"
    return f"{who}×{invitation.turns_remaining}({invitation.source.value})"


def _jsonable(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


HELP = r"""
[bold]Commands[/]
  <text>          speak as Ricky — a QUESTION invites the panel, a
                  statement invites nobody
  /open \[agent] \[n]  open the floor by hand (backstop for a missed cue)
  /close          revoke a standing invitation
  /scores         show live proposal scores
  /force <agent>  give an agent the floor now
  /mute <agent>   mute / unmute an agent
  /kill           emergency silence  (/unkill to release)
  /state          dump floor state
  /quit
"""


def main() -> None:
    parser = argparse.ArgumentParser(prog="panel-sim")
    parser.add_argument("--personas", type=Path, default=Path("personas"))
    parser.add_argument("--live", action="store_true", help="use a real model, not stub brains")
    # Matches `BrainConfig`'s default so the sim rehearses the model that will
    # be on stage — see the reasoning there. Both are overridable, which is
    # what keeps the S0.7 bake-off re-runnable.
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    parser.add_argument("--log", type=Path, default=None, help="append an event log for replay")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--agent-interrupts",
        action="store_true",
        help="let an agent cut off a speaking agent (off by default)",
    )
    args = parser.parse_args()

    cast = PanelCast.from_dir(args.personas)
    brain: Brain = ClaudeBrain(args.model, args.effort) if args.live else StubBrain(args.seed)

    console.print(
        f"[bold]Panel:[/] {', '.join(p.name for p in cast.personas.values())}  "
        f"[dim]({'live · ' + args.model if args.live else 'stub brains'})[/]"
    )
    console.print(HELP)

    sim = Simulation(
        cast,
        brain,
        FloorConfig(allow_agent_interrupts=args.agent_interrupts),
        args.log,
    )

    while True:
        try:
            line = console.input("\n[bold green]Ricky ›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue

        if line.startswith("/"):
            cmd, _, arg = line[1:].partition(" ")
            arg = arg.strip()
            match cmd:
                case "quit" | "q":
                    break
                case "scores":
                    sim.show_scores()
                case "state":
                    console.print(
                        f"floor={sim.state.floor_holder} speaking={sim.state.speaking} "
                        f"invitation={_describe(sim.state.invitation)} "
                        f"agent_turns={sim.state.consecutive_agent_turns} "
                        f"killed={sim.state.killed} t={sim.clock:.1f}s"
                    )
                case "open":
                    agent, _, turns = arg.partition(" ")
                    sim.emit(
                        OperatorCommand(
                            t=sim.clock,
                            action=OperatorAction.OPEN_FLOOR,
                            agent=agent or None,
                            turns=int(turns) if turns.strip().isdigit() else 1,
                        )
                    )
                    console.print(f"  [dim]floor open to {agent or 'the panel'}[/]")
                case "close":
                    sim.emit(OperatorCommand(t=sim.clock, action=OperatorAction.CLOSE_FLOOR))
                    console.print("  [dim]floor closed[/]")
                case "force" if arg:
                    sim.emit(
                        OperatorCommand(t=sim.clock, action=OperatorAction.FORCE_AGENT, agent=arg)
                    )
                case "mute" if arg:
                    muted = sim.state.agents[arg].muted
                    action = OperatorAction.UNMUTE_AGENT if muted else OperatorAction.MUTE_AGENT
                    sim.emit(OperatorCommand(t=sim.clock, action=action, agent=arg))
                    console.print(f"  [dim]{arg} {'unmuted' if muted else 'muted'}[/]")
                case "kill":
                    sim.emit(OperatorCommand(t=sim.clock, action=OperatorAction.KILL_ALL))
                case "unkill":
                    sim.emit(OperatorCommand(t=sim.clock, action=OperatorAction.RELEASE_KILL))
                case _:
                    console.print(HELP)
            continue

        started = time.perf_counter()
        sim.human_turn(line)
        console.print(f"[dim]  ({time.perf_counter() - started:.1f}s wall)[/]")


if __name__ == "__main__":
    main()
