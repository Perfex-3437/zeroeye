# Pre-commit Hook

The Kickama repository includes a pre-commit hook that automates code quality checks and diagnostic generation before every commit.

## What it does

1. **Python syntax validation** — checks all staged `.py` files for syntax errors
2. **Python style checks** — runs `pycodestyle` on staged Python files (warnings only, not blocking)
3. **Shell script syntax validation** — checks staged shell scripts with `bash -n`
4. **Diagnostic build** — runs `python3 build.py` with a countdown timer
5. **Stages diagnostic artifacts** — automatically `git add`s the generated `diagnostic/build-*.logd` and `diagnostic/build-*.json` files
6. **Skips rebuild if unchanged** — compares file hashes against the last commit to avoid unnecessary rebuilds

## Installation

### Via Make (recommended)

```bash
make install-hooks
```

This creates a symlink from `.git/hooks/pre-commit` to `tools/pre-commit`.

### Manual installation

```bash
ln -sf ../../tools/pre-commit .git/hooks/pre-commit
chmod +x tools/pre-commit
```

## Usage

Once installed, the hook runs automatically on `git commit`.

```bash
git commit -m "my changes"
```

The hook will print its progress, including a countdown timer during `build.py` execution. If any check fails, the commit is aborted with a clear error message.

## Skipping the hook

To temporarily bypass the hook:

```bash
git commit --no-verify -m "urgent fix"
```

Or set the environment variable:

```bash
SKIP_PRE_COMMIT=1 git commit -m "message"
```

## Requirements

- Python 3.8+
- `pycodestyle` (optional — for style checks; install via `pip install pycodestyle`)

## Files

| File | Purpose |
|---|---|
| `tools/pre-commit` | The pre-commit hook script |
| `Makefile` | Contains `install-hooks` target |
| `docs/pre-commit-hook.md` | This documentation |
