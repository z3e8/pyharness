"""Scored evaluation suites.

Separate from `tests/` on purpose: `tests/` answers "is it broken", these answer
"how good is it, in a number someone else can check". Not part of the shipped
wheel (`[tool.hatch.build.targets.wheel]`) and not on pytest's default
`testpaths`, so `make test` stays fast; the free suites get their own CI hook.
"""
