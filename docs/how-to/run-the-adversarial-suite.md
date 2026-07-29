# Run the adversarial suite

The suite in `evals/` attacks the harness on purpose and scores what happened.
It answers one question — *what does this thing actually stop?* — and produces
the number in [`evals/SCOREBOARD.md`](../../evals/SCOREBOARD.md).

It needs no API key, no network, and no corpus. Attacks drive the agent loop
through a scripted model, so a malicious `run_python` cell reaches the broker
exactly as a real one would and the refusal is asserted.

## Read the results

```bash
cat evals/SCOREBOARD.md      # the committed artifact: headline, per-attack table, gap rationales
```

The scoreboard is committed, so `git log -p evals/SCOREBOARD.md` shows how the
harness's defensive posture changed over time.

## Run it

```bash
make evals    # run the suite and rewrite evals/SCOREBOARD.md
make test     # runs the suite alongside the unit tests (~6s extra)
```

`make evals` is the artifact writer: run it, then commit the diff. If the diff
is empty, nothing about the harness's behaviour changed.

Because the suite also runs under `make test`, an attack whose verdict stops
matching the scoreboard fails the build **in either direction** — a defense that
regresses fails, and so does a gap that silently closes without being written up.

## Reading a verdict

| Bucket | Meaning |
|---|---|
| **blocked** | The defense refused the attack *in the way the attack said it would*. |
| **known gap** | The attack succeeds today, published with the reason it is a stated boundary rather than an oversight. |
| **unexpected** | An undocumented hole. This bucket should be empty; anything in it is news. |
| **errors** | The attack broke. It says nothing about the system and is never counted as a win. |

The bucket split is the point. A suite that only lists wins is marketing, and one
that only lists holes is not credible — the controls are what make the gaps
believable.

## Add an attack

Attacks live in `evals/attacks/`, one module per defended claim. Two rules carry
the credibility of the whole suite:

- **State the property without reference to the implementation.** A property
  phrased in terms of the code passes by construction and proves nothing. Write
  the claim a skeptical outsider would want checked.
- **Name what counts as a refusal.** `blocked_by(fn, *expected)` lists the
  exception types that constitute a legitimate refusal; anything else scores as
  `ERROR`. `refused_with` additionally requires the refusal message to name the
  mechanism, and `judge` requires independent evidence that an exploit really
  ran — both exist because attacks were caught "passing" on a DNS failure and a
  connection error rather than on the defense.

Then verify the attack actually exercises what it claims: break the specific
defense and confirm the verdict flips, and confirm no *other* attack flips with
it. An attack that cannot be made to fail is not testing anything.

`evals/scoreboard.py` holds the scoring model and `evals/support.py` the verdict
helpers and offline network fixtures.

## Related

- [Security & audit](../explanation/security-and-audit.md) — the policy, grant,
  sandbox and audit machinery the attacks target, and the enumerated
  cross-cutting boundaries.
