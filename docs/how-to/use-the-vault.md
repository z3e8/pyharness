# Use the secrets vault

*Goal: give the agent access to a secret without putting it in plaintext or in
the model's context.*

```bash
pyharness-vault ...   # manage encrypted secrets
```

<!-- TODO: how secrets are stored/encrypted (pyharness/security/vault.py), how
the agent references a secret without it entering context, the CLI surface. -->
