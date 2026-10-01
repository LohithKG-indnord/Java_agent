# Changelog

All notable changes to Java Code Agent are documented here.

## 2026-10-01

### Added

- Added `architecture/proposal.md` describing the project goals, scope, workflow, safety strategy, and future improvements.
- Added `architecture/design.md` documenting the runtime components, configuration, observability, testing strategy, and design constraints.
- Added `HANDOFF.md` with setup, operational behavior, known limitations, and next steps.
- Added Claude-backed Java source generation through Anthropic's API.
- Added deterministic input and output guardrails.
- Added optional local semantic-scope and prompt-injection model guardrails.
- Added `javac` validation with one compiler-guided repair attempt.
- Added structured event logging and per-stage latency metrics.
- Added unit tests covering the main guardrail behaviors.
