# Golden sets

Instructor-marked submissions, the only ground truth the accuracy gate accepts.

## Where they live — not here

Real golden sets contain student submissions and instructor marks. That is
personal data, and committing it to git cannot be undone. The runner therefore
reads from **outside the repository** by default:

```
~/.aijudge/golden/          # default
$AIJUDGE_GOLDEN_DIR         # override
```

The `cs_intro_c/example-task/` directory here is a **synthetic example of the
format** — invented code, invented marks. It exists so the loader has something
to test against and so the layout is documented by a working instance. It is
deliberately too small to satisfy the gate, and must never be treated as data.

## Layout

```
<golden_dir>/
  cs_intro_c/                      subject profile name
    prog2-2025-ex06-p3/            task, named after its Sharif Judge directory
      task/                        desc.md, in/, out/, reference solution
      marks/
        s001.c                     the submission
        s001.yaml                  the instructor's marks for it
```

## A mark file

```yaml
submission: s001.c
marks:
  correctness: 3      # rubric level, not a percentage
  readability: 2
marker: instructor-a
marked_at: 2026-04-15
blind: true           # marked without seeing the AI's output
notes: 変数名は良いが初期化が読みにくい
```

`blind` matters. A mark made after reading the AI's verdict is anchored by it
and is not ground truth — the runner excludes non-blind marks unless you pass
`--include-non-blind`, and you should only do that to inspect the difference,
never to fill out a sample.

## Running

```fish
uv run aijudge-eval --subject cs_intro_c --out report.md
```

Exit codes: `0` pass, `1` fail, `2` not measurable. A thin sample reports `2`,
never `0` — see `evals/gates.yaml` for `min_sample_size`.
