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

> Status: PoC-1 in progress. Real course assignments import and grade end to end
> with a deterministic evaluator plus an LLM rubric judge on a local model, and
> the accuracy harness and the instructor review console are in place.
> **The gate currently reports NOT MEASURED — the golden set is still too small,
> so PoC-1 has not passed.**

## Layout

| Path | Contents |
|------|----------|
| `packages/core` | Subject-agnostic domain model and event contracts. The only package everything else may depend on. |
| `packages/grading` | The grading pipeline, evaluator registry, and subject profile loader (S5). Knows nothing about any subject. |
| `packages/authoring` | Task authoring and importers for existing course assets (S2). |
| `packages/llm_gateway` | Provider abstraction, policy routing, structured output, prompt versioning (S6). |
| `packages/analytics` | Agreement metrics and PoC gate evaluation (S9). Pure functions. |
| `apps/reviewconsole` | Instructor review console. Blind marking first, verdict second. |
| `apps/evalrunner` | Measures agreement against the gates. |
| `apps/*` | Composition roots. The only layer allowed to combine subsystems. |
| `packages/*` | One package per remaining subsystem (S1, S3–S11). |
| `evaluators/*` | Grading plugins. Depend on `packages/core` and nothing else. |
| `subjects/` | Subject profiles: which evaluators run, in what order, under what review policy. |
| `evals/` | Gate thresholds, the golden-set format, and grading regression tests. |
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
ai_evaluators:  [rubric_ai_judge]   # removing this still grades — principle P2
review_policy:  {boundary_score: 0.6, boundary_margin: 0.05}
```

Naming an evaluator that is not installed fails at load, not at grading time.
An import-linter contract fails the build if the engine ever imports an
evaluator directly. `evals/test_prog2_ex06_p3.py` grades a real course
assignment through this path to prove the claim holds on real data.

See [ADR 0002](docs/adr/0002-evaluator-plugin-boundary.md).

### LLM calls go through the gateway

Nothing calls a model provider directly. `packages/llm_gateway` enforces that
learner data only reaches a local provider, validates structured output against
a schema and retries with the actual error when it does not match, records which
prompt version and model produced a result, and derives confidence from
self-consistency across samples.

Defaults point at the lab GPU host (`http://slab-llm:11434`, `gemma4:e4b`);
override with `AIJUDGE_LLM_BASE_URL` and `AIJUDGE_LLM_MODEL`.

```fish
AIJUDGE_LIVE_LLM=1 uv run pytest evals/test_llm_live.py -v -s
```

Everything else runs offline against a scripted provider. See
[ADR 0004](docs/adr/0004-llm-gateway.md) for what the real hardware taught us —
several assumptions about constrained decoding did not survive contact.

### Reviewing is how the golden set gets built

```fish
uv run aijudge-review --golden ~/.aijudge/golden --marker sano
```

The instructor marks the submission **before** the AI verdict exists, then the
verdict is revealed and they settle the final grade. The blind mark is what the
accuracy gate measures; the final grade is what the student receives. Changing
your mind after seeing the AI does not touch the golden entry.

The order is the whole point. Mark after reading the model and your marking is
anchored by it, and the agreement you then measure is inflated. Because the
blind mark is a by-product of ordinary review, keeping the order costs nothing.
The blind page carries no trace of the verdict — grading has not even run yet,
and a test asserts the response body is clean.

No authentication yet; it binds to localhost only. Put it behind S1 before
exposing it.

### Accuracy is measured, and "unmeasured" is not a pass

```fish
uv run aijudge-eval --subject cs_intro_c --out accuracy.md
```

It grades an instructor-marked golden set, computes Cohen's κ, quadratic
weighted κ, miss rate, review rate and scoring consistency, and checks them
against `evals/gates.yaml`. The verdict has three values, not two, and the exit
code follows: `0` pass, `1` fail, `2` **not measurable**. A sample below
`min_sample_size` reports `2` no matter how good the numbers look — three items
agreeing perfectly is not evidence of anything.

Golden sets hold student work and instructor marks, so they live **outside the
repository** (`~/.aijudge/golden`, or `AIJUDGE_GOLDEN_DIR`). Marks made after
seeing the AI's output are excluded by default: they are anchored by it and are
not ground truth. See [`evals/golden/README.md`](evals/golden/README.md) for the
format and [ADR 0005](docs/adr/0005-accuracy-measurement.md) for why the harness
is built to refuse to flatter itself.
