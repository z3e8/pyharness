# Throughput suite — brokered vs two control arms

**Not yet produced.** The suite is built and its offline half runs under
`make test`, but no scored run has happened: producing the board costs a real
model call in each of three arms and needs an API key.

```bash
make evals-data     # runs all three arms and overwrites this file
```

Until then, nothing here should be cited as a result. What exists today is the
machinery and the property tests that keep it honest:

- `evals/data/gen.py` regenerates a ~50MB corpus of service logs from a seed,
  byte-for-byte, with five defects planted in it.
- The answer key is derived by reading the written bytes back, not from the
  generator's intent, and `gen.verify()` asserts every naive shortcut still
  yields a different answer from the correct one.
- `evals/test_data.py` establishes that the corpus is reproducible, that it
  discriminates, that the scorer can fail, and that the answer key is not on
  disk while any arm is running.

See [the how-to](../../docs/how-to/run-the-throughput-suite.md) for what the
task is, what is wrong with the data, and what each arm can do.
