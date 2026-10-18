# Contributing

Thanks for contributing to SOLANA-SNIPER. All changes go through **branches and pull requests** — do not push directly to `main`.

## Workflow

1. Fork or clone the repo
2. Sync `main`: `git checkout main && git pull`
3. Create a short branch: `git checkout -b feat/holders`
4. Make changes and commit
5. Run tests: `cargo test`
6. Push: `git push -u origin feat/holders`
7. Open a PR on GitHub and wait for CI

## Branch naming

Use **one word** after the prefix. Abbreviate long ideas.

| Prefix | Use for |
|--------|---------|
| `feat/` | New features |
| `fix/` | Bug fixes |
| `test/` | Tests |
| `ci/` | CI / automation |
| `docs/` | Documentation |
| `chore/` | Hygiene, deps, cleanup |
| `revamp/` | Larger refactors |

Examples: `feat/holders`, `fix/ws`, `ci/integration`, `docs/license`, `test/devnet`

## Commit messages

Short prefix + summary:

```
[feat]: telegram pause command
[fix]: env validation on startup
[docs]: render deploy section
[test]: ws fixture parse test
[ci]: run integration tests in CI
```

Use `+` for compound parts: `[feat]: pause + circuit breaker`

## Before submitting

```bash
cargo check --all-targets
cargo test
```

CI runs the same checks on every PR to `main`.

## Pull requests

- One logical change per PR when possible
- Link related issues if any
- Include a short test plan in the PR body
- Do not commit `.env`, keys, or `*.db` files
