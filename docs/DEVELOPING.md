# Developing aiJudge

The design arguments behind this codebase, and the commands to work on it.
For running a deployment see [`RUNNING.md`](RUNNING.md); for the decisions
themselves see [`adr/`](adr/) and [`design/`](design/).

## Commands

Requires [uv](https://docs.astral.sh/uv/).

```fish
uv sync --extra dev

uv run pytest          # tests
uv run ruff check .    # lint
uv run mypy packages/core/src
uv run lint-imports    # module boundary contracts
```

## Module boundaries are enforced, not suggested

`packages/core` depends on nothing and performs no I/O. Subsystems never import
each other — they communicate through the events in `aijudge_core.events`.
These rules live in `.importlinter` and fail the build when broken. Adding a
package means registering it there; `packages/core/tests/test_boundaries.py`
catches the omission if you forget.

See [ADR 0001](adr/0001-modular-monolith.md) for why.

## Adding a subject should not touch the engine

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

See [ADR 0002](adr/0002-evaluator-plugin-boundary.md).

## LLM calls go through the gateway

Nothing calls a model provider directly. `packages/llm_gateway` enforces that
learner data only reaches a local provider, validates structured output against
a schema and retries with the actual error when it does not match, records which
prompt version and model produced a result, and derives confidence from
self-consistency across samples.

Defaults point at a local ollama (`http://localhost:11434`, `gemma4:e4b`) —
a default must not send learner data off the machine, so it points where P7
allows and you override it deliberately with `AIJUDGE_LLM_BASE_URL` and
`AIJUDGE_LLM_MODEL`.

```fish
AIJUDGE_LIVE_LLM=1 uv run pytest evals/test_llm_live.py -v -s
```

Everything else runs offline against a scripted provider. See
[ADR 0004](adr/0004-llm-gateway.md) for what the real hardware taught us —
several assumptions about constrained decoding did not survive contact.

## Grading and reviewing are separate processes

```fish
uv run aijudge-web                             # learners submit          :8080
uv run aijudge-worker --phase deterministic    # the fast lane
uv run aijudge-worker --phase ai               # the slow one, arriving later
uv run aijudge-review                          # instructors confirm      :8765
```

The two phases are separate queues, because a test run takes half a second and
an LLM takes seventeen ([ADR 0011](adr/0011-split-the-grading-queue-by-phase.md)).
Sharing one queue meant a learner whose tests had already finished waited behind
somebody else's model call: measured on exam08 — 496 real submissions arriving
uniformly over two hours — one worker gave a p95 of 1689 seconds, and it took
four workers to reach the 30 seconds §9.1 asks for. Split, **one worker answers
in a p95 of 0.8 seconds** and the AI verdict lands when it lands.

The worker grades on submission; the console reads what arrived. **Reviewing is
not a precondition for grading.** It used to be — the console only invoked the
pipeline after the instructor had entered a blind mark, which made capturing
measurement data a prerequisite for grading at all. That inverted means and
ends, so it was undone ([ADR 0007](adr/0007-phase-separation-and-optional-measurement.md)).

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

## A partial total is worse than no total

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

Three different situations leave a criterion without a machine score, and they
do not want the same treatment: an evaluator that fell over (above), a criterion
an instructor scores by hand, and a criterion an aggregation gate cut off. The
run keeps them apart, because mixing them either raises the score of a learner
the gate cut off or closes a submission nobody scored. See
[ADR 0015](adr/0015-three-reasons-a-criterion-has-no-machine-score.md).

## Criteria in order, and a gate that stops asking

A rubric is a list, and until now the order of that list meant nothing: every
criterion was judged independently and the weighted sum was the grade. Some
rubrics do have an order, though — judging how readable a program is, after the
tests showed it does not run, spends an LLM call to describe the readability of
something that is already worth nothing.

So a rubric declares how it is folded. **OR** (the default, and what every
existing course keeps) judges every criterion and sums. **AND** walks the
criteria in order and stops at the first one that scores 0%: the rest are not
evaluated, and count as 0% at their own weight.

Counting them at their own weight is the whole point. Treating them as
"could not be scored" hands their weight to the survivors — with weights
0.3/0.3/0.2/0.2 and the second criterion at 0%, the learner scores 50% instead
of the 30% the gate meant, and the AND disappears. They are also not sent to
review and do not block automatic finalisation: a cut is the specification
working, not an anomaly.

The gate stops where it cannot decide yet. If the criterion above has no verdict
yet — the AI phase has not run — nothing is cut, because cutting on a guess
would settle a learner's criterion at 0% that would have passed.

The order is edited as a number per criterion rather than up/down buttons: ten
criteria would otherwise be ten round trips through the server. A task inherits
the course's setting unless it names its own.

## A criterion an instructor scores by hand

Some criteria have no machine judgement to give — a hand-drawn diagram, a
photograph of an apparatus. The rubric editor can leave a criterion **unassigned**
(`人が採点する`), which is not the same as leaving the evaluator blank: blank
means "any AI evaluator may take it", and pointing an AI at a criterion it cannot
see produces a confident judgement of nothing.

An unassigned criterion goes to no evaluator, records as `awaiting_human`, and
holds the grade open until someone enters a level — including against the bulk
route, which normally closes whatever an instructor signs for. Bulk finalisation
means "I take responsibility without reading each one"; here there is nothing to
take responsibility for, because no one has judged the criterion at all. A whole
task may be scored this way; the machine then scores nothing and says so.

## Finalising a grade is not the same as reviewing it

The instructor's queue holds only the submissions learners contested
([ADR 0009](adr/0009-show-the-verdict-and-let-the-learner-contest-it.md)).
That is the only queue 91 learners times a dozen tasks can produce that anyone
can act on — but it leaves every uncontested submission unfinalised forever, and
the term never closes. Two things close it:

```fish
uv run aijudge-finalize --once     # grading + n minutes elapsed (for cron)
uv run aijudge-finalize            # or resident, every 15 minutes
```

and a per-task **"finalise the rest"** button in `/manage`, which demands a
written justification and shows it back to the learner.

**Neither writes a `HumanReview`.** A grade closing and an instructor having read
the work are two different facts, and the record keeps them apart
([ADR 0010](adr/0010-finalising-a-grade-is-not-a-human-review.md)):

| record | means | evidence for κ |
|---|---|---|
| `HumanReview` | an instructor read **this** submission | **yes** |
| `Finalization` | the grade closed, and by what route | no |

Collapse them and most of a term's grades carry "the instructor agreed with the
AI" while nobody read them — the agreement you then measure is invented, which is
exactly what [ADR 0005](adr/0005-accuracy-measurement.md) exists to prevent.
Keeping them apart means the measurement code needed no change at all.

The automatic route runs in two stages, because a grade that closes silently is
the one-way notice [ADR 0009](adr/0009-show-the-verdict-and-let-the-learner-contest-it.md)
set out to avoid:

```
graded ──→ provisional at once     "this settles at 09/08 23:59 — say so before then"
       ──→ settled at graded + n   unless a learner contested it
```

**The clock starts when that submission finished grading, not at the task's
deadline.** A deadline-based clock makes a learner who submitted early wait days
for their own grade to close, and until it closes the mark they are deciding
whether to resubmit against is provisional. Grading finishes seconds after
submission, so counting from there settles the grade *before* the deadline and
leaves the deadline free to resubmit against — resubmission is not blocked by
finalization (`Task.accepts_submissions_at` never consults it) and the best
attempt is the one that counts.

Announcing the time is what earns the right to close the appeal window at `n`.
Miss that window and the page points at the instructor instead of the form. The
stage is derived, never stored.

The automatic route is the stricter one. It skips anything the review policy
flagged (`review_required`), anything whose score is not settled yet
(`is_provisional`), and anything a learner has contested — a verdict nobody looked at does not become a grade
because a clock ran out (P5). The bulk route includes what the policy flagged,
because an instructor is signing for it in writing.

The grace period is expressed **in minutes**: the course carries the default and a
problem set may override it (`auto_finalize_after_minutes`, editable in `/manage`
by INSTRUCTOR and above, default off), not in `subjects/*.yaml`. Minutes rather
than hours because "settle ten minutes after the deadline" is a real lab
workflow that hours cannot express. The
subject profile is grading configuration and stays out of the browser
([ADR 0002](adr/0002-evaluator-plugin-boundary.md)); a grace period is an
operational value of the same kind as a deadline, so it sits where deadlines sit.

Learners are told which route closed their grade. "The instructor confirmed this"
and "a deadline passed" are not interchangeable sentences, and a grade closed
without being read keeps its appeal link open.

## Lateness is a deduction, not a criterion

Grading does not know about deadlines. No evaluator receives one, and the
pipeline never sees `due_at`. Handing in late is applied afterwards, from
outside the evaluation:

```
evaluation (blind to lateness)        deduction (blind to the work)
  GradingPipeline                     Course.late_penalty_steps × Task.due_at
        └──→ CriterionScore ─┬────────────────┘
                             ↓
              aijudge_core.final_score(run, task_version, review)
```

It began the other way — a rung on the compliance criterion, copied from a
marking sheet where the instructor wrote `遅延14h` in the format column. Folding
it into a level broke three things. The criterion's κ measured reading agreement
and clerical agreement mixed together. Waiving a deduction meant raising a level,
which lands in `HumanReview.adjusted_levels` and reads as the instructor
disagreeing with the AI — the mirror of the trap ADR 0010 closed. And the
deduction rode into S7, mixing what a learner can do with when they handed it in.

The ladder lives on the **course**, beside `auto_finalize_after_minutes` and for
the same reason. A course with no ladder deducts nothing and shows no deduction
row at all: a rule left unset must not look like a rule that was applied and came
to zero. Instructors can waive — a deduction nobody can lift contradicts P5 —
and waiving leaves `agreed` true, because it is not disagreement. Where the
deduction alone turns a pass into a fail, the run goes to review: the lateness is
certain, but only a person can decide whether to forgive it.

The deduction is **recorded on the run**, not recomputed at display time. The
rule is editable mid-term, and recomputing would silently move grades already
returned. See [ADR 0013](adr/0013-lateness-is-a-deduction-not-a-criterion.md).

## A generated task has to earn its way in

```fish
aijudge-admin task draft  --course <id> --key gen/ex01 --author <id> \
    --kc cs.loops.termination --model <drafter> --solver-model <other>
aijudge-admin task review list
aijudge-admin task review decide --version <id> --reviewer <id> \
    --reject --reason "the statement never gives the input format"
aijudge-admin task review rate --course <id>
```

Drafting is the easy half. What decides whether a generated task is usable is
what throws it away, and that runs before an instructor spends attention on it.

**Gate 1** asks whether the reference solution passes its own test cases.
**Gate 2** mutates that solution and asks whether the tests notice
([ADR 0008](adr/0008-companion-processes-for-network-tasks.md)). Gate 2 is
the one that matters: a suite that accepts anything sails through gate 1 with
full marks. Mutants are textual - four operators, no parser - so C and Python
go through the same code and adding a subject does not add a mutation backend.

Three ways it lied, all found by running it rather than reading it, and all in
the direction of making gate 2 *easier*: flipping the `<` in `#include
<stdio.h>` produced a mutant that never compiled and was scored as killed;
mutants that fail to build were counted in the denominator, which is exactly how
a suite of `exit 0` earns a pass; and equivalent mutants failed an honest task
outright - dropping `return 0;` cannot be killed by any test under C99, and two
of six mutants being equivalent put a correct solution at 67% against a
threshold of 80%. Equivalent mutants cannot be eliminated in general, which is
why the threshold is not 1.0 and why the known shapes are skipped.

**Solvability** then hands the statement alone - never the reference solution -
to a *different* model and runs whatever comes back through gate 1's path. It is
not compared against the reference: many programs are correct, and what is being
asked is whether the statement is enough to reach one. Failing is not a
rejection. A task can go unsolved because it is ambiguous or because it is hard,
and nothing here can tell those apart, so discarding automatically would remove
the difficult good questions first.

**The instructor decides.** Rejection demands a reason, which is both what
improves the prompt and what the approval rate needs in its denominator, and a
decided version cannot be decided again - a change of mind is a new version.
The rate counts generated versions only, since an instructor's own tasks are
approved by construction, and a sample under thirty reports NOT_MEASURED.

Knowledge components are the one thing the model never chooses: `TaskDraft` has
no field for them and the blueprint is read instead. But **nothing verifies that
a task actually asks about what it was tagged with** - every check above watches
behaviour. A wrong tag would give a learner mastery in something the task never
exercised, and BKT folds the sequence, so noticing later does not undo it. Two
things stand in the way, neither automatic: the review packet always prints the
components and says no machine checked them, and the solver reports which ones
its own solution actually needed.

Two more checks run alongside the gates, and both refuse more often than they
answer.

**Duplicates.** Embeddings recognise a paraphrase; character trigrams recognise
a copy. Which measure produced the number is always printed, because a lexical
run that found nothing has not established that nothing is there. A provider
with no embedding model is refused by the gateway rather than returning an
empty result - empty is indistinguishable from no matches - and the checker
catches that and drops to lexical, saying so. Embeddings go through the same
P7 policy as generation, since the original text is partly recoverable from
one, and vectors are only compared within the same model and subject.

pgvector is deliberately absent. It is an index, not a capability: a course has
tens to hundreds of tasks, brute-force cosine over that is faster than an index
and runs on SQLite where the tests are. When a corpus needs one, only the column
type changes - the comparison lives in the authoring package and knows nothing
about storage.

**Difficulty** predicts a pass rate from the same neighbours, weighted by
similarity. A task under fifteen attempts contributes nothing rather than a
noisy number, neighbours without history are dropped rather than filled in, and
fewer than two survivors means no estimate - one task's quirks would become the
prediction. Each of those reports NOT_MEASURED with its reason. What it measures
is a pass rate, which moves with when the task was set, whether it was optional
and who was enrolled; the summary says so every time rather than letting 40% be
read as "hard".

## Accuracy is measured, and "unmeasured" is not a pass

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
[ADR 0005](adr/0005-accuracy-measurement.md) for why the harness is built to
refuse to flatter itself.
