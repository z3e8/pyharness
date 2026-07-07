# Your first session

By the end of this you'll have run a task end-to-end and understood what the
agent actually did.

## 1. Set up

```bash
make setup                    # creates .env and installs the package (one-time)
# then edit .env and set ANTHROPIC_API_KEY
```

Verify the install without spending anything (tests need no API key):

```bash
make test
```

## 2. Run a task

The quickest path is the interactive CLI:

```bash
make run
```

```
> Write fib.py that prints the first 10 Fibonacci numbers, run it, and show me the output.
```

Or drive it from Python:

```python
from pyharness import Session, Budget

session = Session(".sessions/demo", budget=Budget(limit_usd=2.0))
try:
    print(session.run("Write fib.py, run it, and confirm the output."))
finally:
    session.close()
```

## 3. What just happened

The orchestrator didn't call a `write_file` tool. It emitted one **`run_python`**
call — real Python — that the harness executed in a persistent kernel:

```python
write("fib.py", "a, b = 0, 1\nfor _ in range(10):\n    print(a); a, b = b, a+b")
print(bash("python fib.py"))
```

Then it read the printed output and replied in plain text. A few things to notice:

- **`write` and `bash` are builtins** — always in scope, called by bare name. The
  full set is in [Builtins](../reference/builtins.md).
- **The kernel persists.** Had it defined a variable, the next `run_python` call
  could use it. Only what it `print()`s comes back into its context.
- **Every side effect was brokered.** The `write` and the `bash` both went through
  [the broker](../explanation/broker.md): policy → audit → budget → execute. Look
  at `.sessions/demo/audit.jsonl` — each call is there, in a tamper-evident chain.

## 4. See it live

Run the same thing with observability and watch the turn unfold as a trace:

```bash
make dev        # Phoenix + the agent → http://localhost:6006
```

## Next steps

- [The `run_python` action space](../explanation/action-space.md) — why Python
  instead of JSON tools.
- [Add a tool or save a skill](../how-to/add-a-tool-or-skill.md) — extend what the
  agent can do.
- [Use the secrets vault](../how-to/use-the-vault.md) — give it credentials safely.
