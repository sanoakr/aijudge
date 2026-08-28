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
> The container escape suite passes: fork bomb contained, non-root, read-only
> root, no host home visible, no network, memory and output capped. It runs on
> macOS through colima, so this is not a Linux-only story. `sandbox-exec` alone
> still cannot contain a fork bomb, which is why the container is required — see
> [ADR 0006](docs/adr/0006-execution-isolation.md), including the two defects the
> suite found the first time it actually ran.
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
uv run aijudge-web                             # learners submit          :8080
uv run aijudge-worker --phase deterministic    # the fast lane
uv run aijudge-worker --phase ai               # the slow one, arriving later
uv run aijudge-review                          # instructors confirm      :8765
```

The two phases are separate queues, because a test run takes half a second and
an LLM takes seventeen ([ADR 0011](docs/adr/0011-split-the-grading-queue-by-phase.md)).
Sharing one queue meant a learner whose tests had already finished waited behind
somebody else's model call: measured on exam08 — 496 real submissions arriving
uniformly over two hours — one worker gave a p95 of 1689 seconds, and it took
four workers to reach the 30 seconds §9.1 asks for. Split, **one worker answers
in a p95 of 0.8 seconds** and the AI verdict lands when it lands.

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

### A partial total is worse than no total

When S6 is down the AI criteria are skipped, and the run records them as
unscored. The deterministic results still go back to the learner — a test run
either passed or it did not, and that is what tells them what to fix next.

The **overall score does not**. Renormalising the surviving criteria produces an
arithmetically correct number that reads as "100%, readability unassessed" to
someone who does not know a criterion is missing. The per-criterion table says
`確認中`, but the large number at the top of the page is read first. So the total
is withheld until an instructor fills the gap, and the page says why — a blank
where a score belongs is indistinguishable from a zero.

Instructors still see it. Closing the gap is their job, and what the
deterministic side returned is what they need to do it.

### Finalising a grade is not the same as reviewing it

The instructor's queue holds only the submissions learners contested
([ADR 0009](docs/adr/0009-show-the-verdict-and-let-the-learner-contest-it.md)).
That is the only queue 91 learners times a dozen tasks can produce that anyone
can act on — but it leaves every uncontested submission unfinalised forever, and
the term never closes. Two things close it:

```fish
uv run aijudge-finalize --once     # deadline + n hours elapsed (for cron)
uv run aijudge-finalize            # or resident, every 15 minutes
```

and a per-task **"finalise the rest"** button in `/manage`, which demands a
written justification and shows it back to the learner.

**Neither writes a `HumanReview`.** A grade closing and an instructor having read
the work are two different facts, and the record keeps them apart
([ADR 0010](docs/adr/0010-finalising-a-grade-is-not-a-human-review.md)):

| record | means | evidence for κ |
|---|---|---|
| `HumanReview` | an instructor read **this** submission | **yes** |
| `Finalization` | the grade closed, and by what route | no |

Collapse them and most of a term's grades carry "the instructor agreed with the
AI" while nobody read them — the agreement you then measure is invented, which is
exactly what [ADR 0005](docs/adr/0005-accuracy-measurement.md) exists to prevent.
Keeping them apart means the measurement code needed no change at all.

The automatic route runs in two stages, because a grade that closes silently is
the one-way notice [ADR 0009](docs/adr/0009-show-the-verdict-and-let-the-learner-contest-it.md)
set out to avoid:

```
graded ──→ provisional at the deadline   "this settles at 09/08 23:59 — say so before then"
       ──→ settled at deadline + n       unless a learner contested it
```

Announcing the time is what earns the right to close the appeal window at `n`.
Miss that window and the page points at the instructor instead of the form. The
stage is derived from `due_at` and the grace, never stored — deadlines move
during a term, and a stored stage would keep the old one.

The automatic route is the stricter one. It skips anything the review policy
flagged (`review_required`), anything with an unscored criterion, and anything a
learner has contested — a verdict nobody looked at does not become a grade
because a clock ran out (P5). The bulk route includes what the policy flagged,
because an instructor is signing for it in writing.

The grace period lives on the **course** (`auto_finalize_after_hours`, editable in
`/manage` by INSTRUCTOR and above, default off), not in `subjects/*.yaml`. The
subject profile is grading configuration and stays out of the browser
([ADR 0002](docs/adr/0002-evaluator-plugin-boundary.md)); a grace period is an
operational value of the same kind as a deadline, so it sits where deadlines sit.

Learners are told which route closed their grade. "The instructor confirmed this"
and "a deadline passed" are not interchangeable sentences, and a grade closed
without being read keeps its appeal link open.

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
