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
    HumanSpeechStarted,
    InjectDirective,
    OperatorAction,
    OperatorCommand,
    PanelCast,
    PanelState,
    RequestProposals,
    StartSpeech,
    StopSpeech,
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

            case InjectDirective():
                console.print(f"  [yellow]↯ {self.cast[command.agent].name}: wrap up[/]")

            case CueModerator():
                console.print(f"  [magenta]▸ hand back to Ricky ({command.reason})[/]")

    def _gather(self, agents: tuple[str, ...]) -> None:
        """Speculative proposals, in parallel — as production will do."""
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
                AgentProposal(t=self.clock, agent=agent, utterance=utterance, signals=signals)
            )

    # ---------------------------------------------------------------------- turns

    def human_turn(self, text: str) -> None:
        self.emit(HumanSpeechStarted(t=self.clock))
        self.emit(TranscriptUpdated(t=self.clock, speaker=HUMAN, text=text, is_final=True))
        self.clock += len(text.split()) / WORDS_PER_SECOND
        self.emit(TurnYielded(t=self.clock))

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


def _jsonable(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


HELP = """
[bold]Commands[/]
  <text>          speak as Ricky
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
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high"])
    parser.add_argument("--log", type=Path, default=None, help="append an event log for replay")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    cast = PanelCast.from_dir(args.personas)
    brain: Brain = ClaudeBrain(args.model, args.effort) if args.live else StubBrain(args.seed)

    console.print(
        f"[bold]Panel:[/] {', '.join(p.name for p in cast.personas.values())}  "
        f"[dim]({'live · ' + args.model if args.live else 'stub brains'})[/]"
    )
    console.print(HELP)

    sim = Simulation(cast, brain, FloorConfig(), args.log)

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
                        f"addressed={sim.state.addressed_agent} "
                        f"agent_turns={sim.state.consecutive_agent_turns} "
                        f"killed={sim.state.killed} t={sim.clock:.1f}s"
                    )
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
