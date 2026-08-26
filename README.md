# aiJudge

AI-assisted assessment platform for STEM coursework — programming, mathematics,
and written reports on one grading substrate.

Three capabilities, each operable as an independent subsystem:

- **AI grading** — deterministic evaluators first (test execution, CAS equivalence,
  unit checking), then LLM rubric judgement on what remains, always with evidence
  and always with a human holding the final call.
- **AI authoring** — generate tasks aimed at specific knowledge components,
  machine-verify solvability and answer uniqueness, then route to instructor review.
- **Skills and portfolio** — evidence-based mastery per knowledge component,
  aggregated across courses and terms into a credential the learner controls.

Design and rollout plan: [`docs/design/`](docs/design/).
Architecture decisions: [`docs/adr/`](docs/adr/).

> Status: pre-PoC. The domain model, the grading pipeline, and one deterministic
> evaluator exist. Real course assignments (Sharif Judge format) import and grade
> end to end. No AI evaluator yet — that is PoC-1.

## Layout

| Path | Contents |
|------|----------|
| `packages/core` | Subject-agnostic domain model and event contracts. The only package everything else may depend on. |
| `packages/grading` | The grading pipeline, evaluator registry, and subject profile loader (S5). Knows nothing about any subject. |
| `packages/authoring` | Task authoring and importers for existing course assets (S2). |
| `packages/*` | One package per remaining subsystem (S1, S3–S11). |
| `evaluators/*` | Grading plugins. Depend on `packages/core` and nothing else. |
| `subjects/` | Subject profiles: which evaluators run, in what order, under what review policy. |
| `evals/` | Golden datasets and regression tests for grading accuracy. |
| `docs/adr/` | Architecture decision records. |

## Development

Requires [uv](https://docs.astral.sh/uv/).

```fish
uv sync --extra dev

uv run pytest          # tests
uv run ruff check .    # lint
uv run mypy packages/core/src
uv run lint-imports    # module boundary contracts
```

### Module boundaries are enforced, not suggested

`packages/core` depends on nothing and performs no I/O. Subsystems never import
each other — they communicate through the events in `aijudge_core.events`.
These rules live in `.importlinter` and fail the build when broken. Adding a
package means registering it there; `packages/core/tests/test_boundaries.py`
catches the omission if you forget.

See [ADR 0001](docs/adr/0001-modular-monolith.md) for why.

### Adding a subject should not touch the engine

`packages/grading` never imports an evaluator. Evaluators register through the
`aijudge.evaluators` entry point group, and a subject profile in `subjects/`
names the ones it wants:

```yaml
# subjects/cs_intro_c.yaml
deterministic:  [code_test_runner]
ai_evaluators:  []            # empty still grades — see design principle P2
review_policy:  {boundary_score: 0.6, boundary_margin: 0.05}
```

Naming an evaluator that is not installed fails at load, not at grading time.
An import-linter contract fails the build if the engine ever imports an
evaluator directly. `evals/test_prog2_ex06_p3.py` grades a real course
assignment through this path to prove the claim holds on real data.

See [ADR 0002](docs/adr/0002-evaluator-plugin-boundary.md).
