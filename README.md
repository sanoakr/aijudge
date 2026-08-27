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

> Status: Phase 0 is built end to end — submit, isolate, grade, confirm, return.
> Submission intake and job orchestration (S3), local authentication (S1), the
> learner web app, PostgreSQL persistence, the grading worker, next-step
> feedback, and the instructor review console are all in place. See
> [`docs/RUNNING.md`](docs/RUNNING.md) to run it.
>
> **Not yet cleared: the container escape suite has never run.** macOS seatbelt
> cannot contain a fork bomb (measured — see
> [ADR 0006](docs/adr/0006-execution-isolation.md)), so real student code needs
> Linux and containers, and `packages/sandbox/tests/test_container.py` must pass
> rather than skip before any of it runs. A skip is not a pass.
>
> **Measuring grading accuracy is Phase 1, not Phase 0.** The records it needs
> are captured from the start — a blind mark cannot be reconstructed later — but
> grading does not depend on the measurement, and the accuracy gate currently
> reports NOT MEASURED. See [ADR 0007](docs/adr/0007-phase-separation-and-optional-measurement.md).

## Layout

| Path | Contents |
|------|----------|
| `packages/core` | Subject-agnostic domain model and event contracts. The only package everything else may depend on. |
| `packages/grading` | The grading pipeline, evaluator registry, and subject profile loader (S5). Knows nothing about any subject. |
| `packages/authoring` | Task authoring and importers for existing course assets (S2). |
| `packages/llm_gateway` | Provider abstraction, policy routing, structured output, prompt versioning (S6). |
| `packages/submission` | Submission intake, artifact storage, and grading job orchestration (S3). Does not import the grading engine. |
| `packages/identity` | Local authentication, courses, enrolment (S1). Credentials never leave this package. |
| `packages/persistence` | PostgreSQL implementations of the S2/S3/S1 stores. Infrastructure — no subsystem may import it. |
| `packages/feedback` | Turns grading results into the learner's next step. Draws only on deterministic results. |
| `packages/observation` | The observation record — what grading leaves behind for measurement to read. Depends on pydantic and nothing else. |
| `packages/analytics` | Agreement metrics and gate evaluation (S9). Pure functions. Delete it and grading still runs — verified by deleting it. |
| `apps/studentweb` | The learner-facing app: submit, see results. What is visible lives in one module, not in templates. |
| `apps/grader` | The grading worker. The only layer that knows both the queue and the pipeline. |
| `apps/reviewconsole` | Instructor review console. Reads results, confirms grades. Never grades. |
| `apps/evalrunner` | Reads recorded observations and measures agreement against the gates. Never grades. |
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

### Grading and reviewing are separate processes

```fish
uv run aijudge-web        # learners submit                    :8080
uv run aijudge-worker     # grading consumes the queue
uv run aijudge-review     # instructors confirm                :8765
```

The worker grades on submission; the console reads what arrived. **Reviewing is
not a precondition for grading.** It used to be — the console only invoked the
pipeline after the instructor had entered a blind mark, which made capturing
measurement data a prerequisite for grading at all. That inverted means and
ends, so it was undone ([ADR 0007](docs/adr/0007-phase-separation-and-optional-measurement.md)).

For most submissions the instructor reads the verdict and settles the grade. For
a **sampled** subset they mark first, blind, and only then see the model. Marking
after reading the model is anchored by it, and the agreement you measure is
inflated — but demanding it on every submission taxes every review, so it is
sampled. The rate is declared per subject (`measurement.blind_sample_rate`) and
the selection is a hash of the submission id, not the instructor's choice: let
them pick and the hard submissions end up over-represented.

The blind page carries no trace of the verdict even though grading has already
finished. It is not hidden — it is absent from the response, and a test asserts
the body is clean.

No authentication yet; it binds to localhost only. Put it behind S1 before
exposing it.

### Accuracy is measured, and "unmeasured" is not a pass

```fish
uv run aijudge-eval --subject cs_intro_c --out accuracy.md
```

**It does not grade.** It reads the observation records that grading and review
left behind — one per submission per criterion — computes Cohen's κ, quadratic
weighted κ, miss rate and review rate, and checks them against
`evals/gates.yaml`. Recomputing κ needs no LLM, no sandbox, and no task
definition; `import-linter` enforces that the measurement side cannot import the
grading side at all — and, in the other direction, that grading cannot import
the measurement side. Both directions are needed: with only the first, the
review console imported the record type from `analytics` and deleting it stopped
grading from starting at all. The record type now lives in its own package.

The verdict has three values, not two, and the exit code follows: `0` pass, `1`
fail, `2` **not measurable**. A sample below `min_sample_size` reports `2` no
matter how good the numbers look — three items agreeing perfectly is not evidence
of anything. And `2` is not an operational failure: grading works whether or not
anyone measures it.

Observations hold student work and instructor marks, so they live **outside the
repository** (`~/.aijudge/golden`, or `AIJUDGE_GOLDEN_DIR`). Marks made after
seeing the AI's output are excluded: they are anchored by it and are not ground
truth, and the report lists every observation it excluded and why. See
[ADR 0005](docs/adr/0005-accuracy-measurement.md) for why the harness is built to
refuse to flatter itself.
