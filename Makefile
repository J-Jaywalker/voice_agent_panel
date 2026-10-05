# Short names for the invocations in CLAUDE.md's Commands section.
#
# `make`, not `just`: the show runs on a machine at the venue that nobody has
# shelled into yet, and make is already on it.
#
# The raw `uv run ...` commands stay the ground truth — anything that is not
# one of the handful of combinations below should be typed out in full rather
# than added here.

.DEFAULT_GOAL := help

.PHONY: help panel panel-unlocked enrol display sim test test-core lint

help:
	@echo "voice_agent_panel"
	@echo ""
	@echo "  make panel           live pipeline + wall + TypeSafe addressing, logged"
	@echo "  make panel-unlocked  the same, mic ungated (no speaker enrolment)"
	@echo "  make enrol           re-capture Ricky's voice enrolment"
	@echo "  make display         video wall alone, synthetic panel, no mic or keys"
	@echo "  make sim             text mode, offline stub brains"
	@echo "  make test            everything (~2min)"
	@echo "  make test-core       floor logic only (~4s) — the tight loop"
	@echo "  make lint            ruff"
	@echo ""
	@echo "Any other combination of flags: see CLAUDE.md, or uv run panel --help"

panel:
	uv run panel --display --log recordings/$$(date +%F-%H%M).jsonl

panel-unlocked:
	uv run panel --display --no-speaker-lock --log recordings/$$(date +%F-%H%M).jsonl

enrol:
	uv run panel --re-enrol

display:
	uv run panel-display --demo

sim:
	uv run panel-sim

test:
	uv run pytest

test-core:
	uv run pytest packages/panel_core

lint:
	uv run ruff check .
