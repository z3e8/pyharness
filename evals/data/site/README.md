# The throughput suite as pages

The three arms of `evals/data/BOARD.md`, baked into self-contained HTML beside
the board itself. Open `index.html` in a browser — no server, no build step, no
network.

| Page | What it is |
|------|------------|
| `brokered.html` | The brokered arm's full session: every cell it ran against 51MB of gzipped logs, and the five defects it had to find before any answer meant anything. |
| `control-arm-files.html` | The same task with conventional file tools. |
| `control-arm-shell.html` | The same task with a shell. |
| `board.html` | `../BOARD.md`, rendered through the same markdown renderer as the transcripts. |

The two control arms have no session of their own — they are not brokered runs,
so there is no trace to replay. They ride in as `--doc` transcripts, which is
why their pages are shorter and have no step timeline.

## Why these are committed

Same reason as [`../../demo/site/`](../../demo/site/): `.sessions/` is
gitignored, so the traces behind these pages are not in the repo, and
regenerating them costs a real model call in each of three arms. The pages are
the artifact, the way `../BOARD.md` is.

`make site-data` rebuilds them (`DATA_RUN=` to point at another run). The board
is the artifact of record; `board.html` is a rendering of it and is never edited
by hand.

## On publishing these

Verified free of the operator's home directory before they were committed. Note
that this is currently a property of *these files*, not a guarantee of the
writer: `obs/static.py` redacts trace content but not `--doc` content, so a
future rebake from a transcript containing absolute paths would reintroduce
them. Check before committing a regenerated set.
