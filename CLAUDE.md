# Loopi — instructions for Claude Code

Read `docs/HANDOFF.md` first: it holds the current state, the next steps and every decision taken so far.

## Working with the owner
- Reply in Vietnamese and address the owner as "bạn". Code, comments, commit messages and plans are in English;
  user-facing brain UI text is Vietnamese.
- Execute approved plans autonomously with `superpowers:subagent-driven-development` (one implementer + one
  reviewer per task, a final whole-branch review). Do not ask "continue?" between tasks.
- Stop and ask only for: changing a decision recorded in the spec, irreversible or outward-facing actions
  (push, publish, deleting data, other people's resources), and hardware steps only the owner can do
  (flashing an SD card, plugging/unplugging the camera, mic, power).
- Record every ruling you make in the plan's ledger (`docs/superpowers/ledgers/` is the committed copy).

## Sources of truth (in order)
1. `docs/superpowers/specs/2026-10-14-loopi-design.md` — binding spec (Vietnamese).
2. `docs/protocol/v1.openapi.yaml` — device↔brain contract. The brain enforces it; the agent's tests validate
   against it. Any change must update the YAML, the brain, the agent and both contract test suites together.
3. `docs/superpowers/plans/*.md` — implementation plans; ledgers record where the code deliberately differs.

## Commands
```bash
# brain (Python 3.13, uv; Docker needed for testcontainers)
cd brain && uv sync && uv run pytest --cov=loopi_brain
# deploy smoke test against the real stack: see deploy/README.md
# agent (Rust)
cd agent && cargo test && cargo llvm-cov --ignore-filename-regex 'main.rs' && cargo tree -i aws-lc-rs
```

## Non-negotiables
- Conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`), TDD, coverage ≥ 80 % (brain package; agent library).
- rustls with the **ring** provider only — `aws-lc-rs` must never enter the agent's dependency tree.
- Privacy: the LED is solid whenever any recorder runs; recorders die with the agent; pause persists and fails closed.
- Durability: the agent deletes a queued item only after the brain answers 201/200; anything else is retried or
  quarantined, never dropped. Audio and markers are never deleted automatically.
- Never commit secrets (`deploy/.env`, PKI material, pair tokens). Never prune or delete Docker resources you did not create.
