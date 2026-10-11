# Contributing

repo2skill 0.1.0 is an unpublished local alpha, supported on Python 3.11 and newer. The current implementation was locally verified on Windows/Python 3.13. CI is configured for Windows, Linux, and macOS on Python 3.11 and 3.13; those six combinations remain unverified until their workflows run.

Create an isolated environment from a checkout:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Run the focused test for the change, then the full suite and package build before reporting completion:

```sh
python -m pytest tests/path/to/test_file.py -v
python -m pytest -q
python -m build
repo2skill --help
```

Do not claim platform coverage from configuration alone. Report the command, platform, Python version, and actual result. Integration fixtures are synthetic and hermetic: tests must not access a model or network, execute repository-provided scripts, or include real secrets. Update the upstream source map whenever an upstream rule or file is adapted; preserve the pinned commit and full MIT notice.

Use each human contributor's own configured Git identity as the commit author. For work assisted by OpenAI Codex, record it with the commit trailer `Assisted-by: OpenAI Codex`; do not invent an email address or account for the tool.
