# Throughput suite — brokered vs two control arms

**Produced 2026-08-04** by `python -m evals.data.run` against mid, on a corpus of 298,603 requests across 30 gzipped files (51MB uncompressed — roughly ten times a large context window). Refreshing it costs a real
model call in each of three arms, so it is committed as a dated artifact.
The corpus itself is not committed: it is regenerated from a seed by
`evals/data/gen.py`, and the answer key is derived by reading the written
bytes back rather than from the generator's intent.

## The task

Three questions about a month of service logs: total requests, the day
`/checkout` p95 first breached 500ms, and the busiest hour in UTC.
Every arm gets the same prompt, the same model, the same tier and the
same budget. They differ only in what they can do.

## What is wrong with the data

Five defects, each load-bearing for exactly one question. None is named
in the prompt.

| Defect | What it is |
|---|---|
| `timezone-shift` | from day 8 timestamps are local time (UTC-4) still suffixed 'Z' |
| `field-rename` | from day 10 the 'route' key is named 'path' |
| `duplicate-export` | day 14 was exported twice into one file |
| `unit-change` | from day 19 'latency_ms' holds microseconds, name unchanged |
| `malformed-lines` | truncated lines scattered through the month |

A shortcut that ignores them is *available* and produces a specific set
of plausible wrong numbers — that is what makes a correct answer
evidence rather than arithmetic. Which defects an arm handled is derived
from the integers it returned, never from a keyword scan of its prose.

## The arms

- **brokered** — this harness. Python action space, persistent kernel.
- **files** — `list_files` + `read_file`, the conventional pair. Its
  `read_file` decompresses `.gz` and takes a line range, so the arm
  fails on volume rather than on encoding.
- **shell** — one `shell` tool in the data directory. It can `awk` and
  `python -c`, so it can genuinely win. It has no persistent kernel:
  every call is a fresh process.

## Board
```

  throughput suite — 298,603 requests across 30 files, 51MB uncompressed

    answer key    requests=298603  breach_day=25  peak_hour=15
    the shortcut  requests=309965  breach_day=19  peak_hour=12

  brokered  2/3; $0.3647; 15 steps; defects handled: duplicate-export, field-rename, malformed-lines, unit-change
            requests=ok  breach_day=ok  peak_hour=NAIVE

  files     0/3; $2.6750; 123 steps; no defects handled; BudgetExceeded: budget exhausted: spent $2.6750 of $2.00
            requests=-  breach_day=-  peak_hour=-

  shell     2/3; $0.2261; 23 steps; defects handled: duplicate-export, field-rename, malformed-lines, unit-change
            requests=ok  breach_day=ok  peak_hour=NAIVE

```
