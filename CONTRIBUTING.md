# Contributing to ZeroEye

Thank you for your interest in contributing to ZeroEye! This guide will help you set up your development environment and submit high-quality pull requests.

## Quick Start

```bash
# Clone the repository
git clone https://github.com/lobster-trap/zeroeye.git
cd zeroeye

# Run the build
python3 build.py
```

## Prerequisites

- **Python 3.10+** — Required for build system and tools
- **Node.js 18+** — Required for frontend (TypeScript/React)
- **Git** — For version control

Optional dependencies for specific modules:
- **Rust (cargo)** — Backend engine
- **Go** — Market module
- **Java (JDK 21+)** — Compliance module
- **C (gcc/make)** — Frailbox module

## Build System

ZeroEye uses a unified Python build script:

```bash
python3 build.py              # Build all modules
python3 build.py --clean      # Clean build artifacts
python3 build.py --module backend  # Build specific module
python3 build.py --release    # Release mode
```

### Build Diagnostics

Each build generates encrypted diagnostic artifacts in the `diagnostic/` directory:
- `diagnostic/build-<commit>.logd` — Encrypted build log
- `diagnostic/build-<commit>.json` — Metadata (includes decrypt password)

**These diagnostic files must be included in your PR submissions.**

## Pull Request Workflow

1. **Fork** the repository on GitHub
2. **Create a branch** with a descriptive name:
   ```bash
   git checkout -b fix/description-of-fix
   # or
   git checkout -b feat/description-of-feature
   ```
3. **Make your changes** following the code style guidelines below
4. **Run the build** to generate diagnostic artifacts:
   ```bash
   python3 build.py
   ```
5. **Commit your changes** with clear commit messages:
   ```bash
   git commit -m "fix: describe what you fixed (#issue-number)"
   ```
6. **Push** to your fork and create a Pull Request
7. **Use the PR template** at `.github/pull_request_template.md` for your submission
8. **Include diagnostic artifacts** from `diagnostic/` in your PR description

## Code Style

- **Python**: Follow PEP 8. Use type hints for function signatures.
- **TypeScript/React**: Use ESLint and Prettier configurations provided in the repo.
- **General**: Refer to `.editorconfig` for basic formatting rules.
- Write module-level docstrings for all Python files.
- Keep functions focused and single-purpose.

## Bounty Program

Active bounties are tracked as GitHub Issues with the `bounty` label. Each bounty has a clearly defined scope and acceptance criteria. To claim a bounty:

1. Comment on the issue expressing interest
2. Submit a PR following the guidelines above
3. Include build diagnostic artifacts in your PR

## Getting Help

- Open an issue for questions or problems
- Refer to the project's README for additional documentation

## License

Contributions are accepted under the same license(s) as the repository (MIT / Apache 2.0 / GPLv3 / BSD 2-Clause).
