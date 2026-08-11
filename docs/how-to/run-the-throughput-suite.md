# Run the throughput suite

The suite in `evals/data/` asks whether the agent can work on more data than it
can read. It tests the claim the [action space](../explanation/action-space.md)
page makes — that one `run_python` call beats a menu of fine-grained tools —
and produces [`evals/data/BOARD.md`](../../evals/data/BOARD.md).

It is the only suite here measuring **competence**. The
[adversarial suite](run-the-adversarial-suite.md) measures what the harness
refuses; the demo suite measures containment. This one measures what it can do.

## The task

A month of service logs: 30 gzipped NDJSON files, ~300k requests, ~50MB
uncompressed — roughly ten times a large context window. Three questions:

1. how many requests were served,
2. which day `/checkout` p95 first breached its 500ms SLO,
3. which hour of the day carried the most traffic.

The answers are three integers, so scoring is exact-match and needs no judge
model.

## Why the data is dirty

Summing a column proves nothing — a shell one-liner does it. So the corpus has
five defects planted in it, none named in the prompt, each load-bearing for
exactly one question:

| Defect | What it is |
|---|---|
| `timezone-shift` | timestamps become local time partway through the month, still suffixed `Z` |
| `field-rename` | the `route` key silently becomes `path` |
| `duplicate-export` | one day was exported twice, with identical request ids |
| `unit-change` | `latency_ms` starts holding microseconds, name unchanged |
| `malformed-lines` | truncated writes scattered through the month |

Each one makes a *plausible wrong answer* reachable. The unit change is the
sharpest: it puts an apparent latency breach six days before the real
regression, so an arm that runs the obvious groupby gets a confident, specific,
wrong day. That gap between the naive answer and the correct one is what makes a
right answer evidence rather than arithmetic.

Which defects an arm handled is derived from **the integers it returned**, never
from a keyword scan of its prose. A model can describe a timezone problem
eloquently and still report the wrong hour.

## The arms

All three get the same prompt, model, tier and budget. They differ only in what
they can do.

| Arm | Tools |
|---|---|
| `brokered` | this harness — Python action space, persistent kernel |
| `files` | `list_files` + `read_file`, the conventional pair |
| `shell` | one `shell` tool in the data directory |

The `files` arm's `read_file` decompresses `.gz` and takes a line range, which is
more than most implementations give you. That is deliberate: the arm has to fail
on **volume**, not on encoding, or the result is about gzip rather than about the
action space.

The `shell` arm can `awk` and `python -c`, so it can genuinely win. That is a
finding, not a flaw in the control: *code* is the capability, and a shell is
one way to get it. What a shell does not give you is a persistent
kernel: every call is a fresh process, so each exploratory pass re-reads the
corpus from disk.

## Run it

```bash
make evals-data                          # all three arms, writes evals/data/BOARD.md
python -m evals.data.run --arm brokered  # one arm
python -m evals.data.run --rows 2000     # a smaller corpus
```

Needs an API key. It costs a real model call in each of three arms — capped per
arm by the suite's own budget — so it is a deliberate target rather than part of
`make test`, and the board is committed as a dated artifact.

The offline half runs under `make test` with no key: the corpus generator, the
scorer, and the isolation property below.

## Look at what happened afterwards

Every arm leaves a record, so the run is inspectable long after it finishes.

| Arm | Record |
|---|---|
| `brokered` | a real session — `trace.jsonl` and the audit chain, under `.sessions/data-eval-<stamp>/brokered/` |
| `files`, `shell` | `arm-<name>/transcript.md` and `transcript.json` — every tool call and result |

The control arms are not sessions, so the loop records itself; without that the
board's control rows would be numbers with nothing behind them.

Bake all of it into one self-contained page — the run prints this command with
the paths filled in when it finishes:

```bash
uv run pyharness-watch .sessions/data-eval-<stamp>/brokered \
  --static .sessions/data-eval-<stamp>/site \
  --title "Throughput suite" \
  --doc "Board=evals/data/BOARD.md" \
  --doc "Control arm: files=.sessions/data-eval-<stamp>/arm-files/transcript.md" \
  --doc "Control arm: shell=.sessions/data-eval-<stamp>/arm-shell/transcript.md"
```

That is the same renderer behind `evals/demo/site/`: no server, no external
requests, one page per arm. Pass `--keep` to the run itself if you also want the
corpus left on disk to show.

## The corpus is not committed

`evals/data/gen.py` regenerates it from a seed, byte-for-byte. Nothing but the
generator is in git, which is why the answer key is *derivable* rather than
archived.

Two consequences worth knowing before editing it:

- **The answer key is read back off disk**, not taken from the generator's
  intent. Otherwise the key and the data could drift apart silently and the
  board would publish a discrimination the corpus no longer has.
- **`gen.verify()` asserts the suite still discriminates** — that every naive
  shortcut still yields a different answer from the correct one. Tune a constant
  in a way that flattens the gap and generation fails rather than the board
  quietly going green.

## What the exit code gates on

It fails if the corpus stops discriminating, or if the brokered arm cannot
produce an answer at all — the harness being broken.

It does **not** fail on getting a question wrong. How many of three a model gets
right is a fact about the model, and gating a build on it would turn someone
else's release notes into a red build here. The score is published with its
denominator instead, the same rule the demo suite uses.

## Related

- [The `run_python` action space](../explanation/action-space.md) — the claim
  this suite exists to test.
- [Run the adversarial suite](run-the-adversarial-suite.md) — the containment
  half of the evidence.
