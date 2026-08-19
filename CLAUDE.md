# Working in this repo

## Test after every feature or fix, not just at the end

This project has classes of bug that are easy to reintroduce silently:
unpaginated DynamoDB `Scan` calls, tool functions returning `None` on
data drift instead of an error dict, schema migrations that only run on
the write path (so a fresh read-only DB crashes), and environment
variables that can silently override AWS credentials
(`AWS_BEARER_TOKEN_BEDROCK`). Each has a regression test — see
`tests/test_tools.py`, `tests/test_runtime.py`,
`tests/test_bearer_token_env.py`.

**Rule: after implementing or changing any feature, run the test suite
before considering the change done — not as a separate cleanup pass at
the end of a session.**

```sh
uv run python -m pytest              # unit tests — fast, free, no AWS calls
uv run python -m tests.run_eval      # LLM eval — real Bedrock cost, run before
                                       # committing a prompt/tool change, not
                                       # on every trivial edit
uv run python -m scripts.dev_server  # local server launch — runs pytest +
                                       # a live Bedrock preflight check first,
                                       # refuses to start if either fails.
                                       # Starts mcp_server.server (:8787)
                                       # and web.server (:8788). Use this,
                                       # not a bare `python -m mcp_server.server`
                                       # or `python -m web.server`.
```

If you add a feature or fix a bug that isn't covered by an existing
test, add one in the same turn — see `tests/test_runtime.py` and
`tests/test_tools.py` for the existing patterns (monkeypatched fakes,
no live AWS/Bedrock calls). `mcp-context-inspector` (the dependency
providing `metrics/`) follows the same pattern for its own DynamoDB
backend — a hand-rolled in-memory table stub, not moto. A bug fix
without a regression test is only half done.

## Where things live

- `docs/PROJECT.md` — the full design history and reasoning (why
  choices were made, what was tried and rejected). Read this before
  making an architectural change, not the code alone.
- `README.md` — how to actually run/test/deploy this, kept current.
- `agent/runtime.py` is boilerplate (rarely touched); `agent/app.py` +
  `tools/` + `agent/system_prompt.txt` are this use case's config. See
  `docs/PROJECT.md`'s boilerplate/config split section before adding a
  new tool or changing the loop mechanics.

## Standing rules for this repo

- Pause for confirmation before major architectural changes (new
  cloud dependency, new storage backend, new auth model) — don't just
  build ahead on momentum.
- Anything destructive (dropping the local DB, deleting deployed AWS
  resources) stays a manual, explicit step — never automate deletion.
- If AWS/Bedrock credentials aren't available, say "unvalidated"
  rather than assuming a change works.
