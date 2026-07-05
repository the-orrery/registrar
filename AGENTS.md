# registrar — for agents

`registrar` is the tool skeleton for the local workspace asset registry and
lifecycle dry-run control plane.

Boundaries:

- Tool skeleton lives in this repo: CLI, schema helpers, doctor/relocate rules,
  tests, examples, and docs.
- Real registry data must live outside this repo. Use `REGISTRAR_REGISTRY_ROOT`
  or `--registry-root` for sandboxed dry-runs or alternate registries.
- Runtime/cache output belongs in user data/cache directories, not in the
  skeleton repo.
- v0 commands are read-only or explicit dry-run by default. Do not add automatic
  move/delete/GC behavior.

Development:

```sh
uv sync
uv run registrar --help
uv run poe check
```
