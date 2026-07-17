# Necromancer

An AI agent that resurrects abandoned open-source Python projects.

The current MVP is deterministic execution infrastructure: it snapshots a
repository, creates a disposable candidate, installs it in a fresh venv, and
records pytest collection separately from test execution.

```bash
PYTHONPATH=src python -m necromancer run /path/to/repository
```

The command prints the root `result.json`. Each run also contains
`process.json`, phase-local capture artifacts under `collection/` and `test/`,
their JUnit XML backups, and command stdout/stderr logs.
