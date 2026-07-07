# Run with observability

*Goal: see the agent's loop, prompts, and responses in a UI.*

```bash
make dev    # starts Phoenix (background) + the agent → http://localhost:6006
make down   # stop Phoenix (keeps data); `make clean` also deletes its data
```

<!-- TODO: what shows up in Phoenix; the Langfuse profile (`make up-langfuse`,
:3000) and when to prefer it; relevant .env keys (PYHARNESS_TELEMETRY_*). -->
