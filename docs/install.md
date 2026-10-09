# Install and run locally

repo2skill 0.1.0 is an unpublished local alpha with no package-index release. The supported Python floor is 3.11; local Task 12 verification was performed on Windows/Python 3.13. The six CI combinations (Windows, Linux, and macOS on Python 3.11 and 3.13) are configured but remain unverified until their workflow runs.

## Install for use

Python 3.11 or newer is required. To install the command from a source checkout, run these commands in the repository root:

```powershell
python -m pip install --user pipx
python -m pipx install .
python -m pipx ensurepath
```

Close and reopen the terminal after `ensurepath` so the updated PATH takes effect. The `repo2skill` command is then available directly; from a target repository, run `repo2skill init`, `repo2skill generate --no-ai`, or `repo2skill --help`.

To install a locally built wheel instead, run `python -m pipx install "path/to/repo2skill-0.1.0-py3-none-any.whl"`. Version 0.1.0 has not been published to PyPI, so `pip install repo2skill` is not available.

Package installation may contact the configured package index to install pipx or repo2skill's dependencies. This setup traffic is separate from repository operations: generation and freshness checking make no network requests and use no AI.

## Maintainer editable install

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
