# Install and run locally

repo2skill 0.1.0 is an unpublished local alpha with no package-index release. The supported Python floor is 3.11; local Task 12 verification was performed on Windows/Python 3.13. The six CI combinations (Windows, Linux, and macOS on Python 3.11 and 3.13) are configured but remain unverified until their workflow runs.

Install from a checkout with the development tools:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\repo2skill.exe --help
```

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/repo2skill --help
```

Pip may contact its configured package index while installing the package and its dependencies. That setup traffic is separate from no-AI generation and freshness checking, which make no network requests. The CLI and lifecycle contract is documented in [`cli-contract.md`](engineering/cli-contract.md).

The package currently declares runtime dependencies on `pathspec` and PyYAML. To build a local wheel and source archive after installing development dependencies, run `python -m build`. The release smoke installs the wheel into a fresh environment outside the checkout, checks the installed import location and templates, compares the packaged notice bytes, and invokes the entry point. No public distribution or external installation URL is available.
