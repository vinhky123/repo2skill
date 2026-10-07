# Task P0-01 implementation report

## Scope

Implemented the package/config foundation only: build metadata, minimal
`--help`/`--version` entry point, the three frozen policy/target dataclasses,
versioned defaults, safe root-relative target resolution, attribution, reuse
map, and behavioral/distribution tests. No scan or generate command exists.

Contract rulings applied from the task controller:

- `TargetSpec.livePath` is a normalized repository-relative string;
  `resolve_under` returns an absolute `Path` at the I/O boundary.
- Schema/default values are `1.0`, `scan-policy-v1`, `claim-policy-v1`,
  `generatedMode="exclude"`, `encodings=("utf-8-sig",)`, and a 16,384-byte
  per-target render budget. Both targets keep an empty managed block.
- The corrected target assertion compares the `targetId` values.

## Dependency audit

Official PyPI project pages and JSON metadata were checked on 2026-10-07.
Runtime bounds use pathspec 1.1.1 (`MPL-2.0`, Python `>=3.9`, OS Independent)
and PyYAML 6.0.3 (`MIT`, Python `>=3.8`, OS Independent). Build/test bounds
were checked at setuptools 84.0.0, wheel 0.48.0, build 1.6.1, and pytest
9.1.1. The exact metadata and links are in
`docs/engineering/upstream-source-map.md`.

## TDD evidence

Initial RED command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_distribution.py -v
```

Relevant result: exit 2 during collection with
`ModuleNotFoundError: No module named 'repo2skill'`. Full output is retained
in `task-01-red.log`.

Initial GREEN command after the minimal implementation:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_distribution.py -v
```

Relevant result: `10 passed, 1 skipped`; the skip is Windows only because a
`Path` object normalizes `/` and `\` identically there. Backslash rejection
is independently tested at the persisted `TargetSpec.livePath` boundary,
and the filesystem test exercised an actual Windows symlink/reparse ancestor.
Full output is in `task-01-green.log`.

Self-review found two missing boundary cases and used separate RED/GREEN
cycles:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_config.py::test_resolve_under_preserves_complete_missing_destination -v
.\.venv\Scripts\python.exe -m pytest tests/test_config.py::test_default_targets_are_relative_and_use_a_safe_root_slug -v
```

The first failed because the returned path stopped at `root/safe`; after the
fix it passed with the full `root/safe/missing/destination`. The second failed
with `.agents/skills/repository` for `Path(".")`; after resolving the root name
it passed with `.agents/skills/my-project`.

Final full-suite command:

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

Relevant result: `11 passed, 1 skipped in 4.05s`, exit 0. Full output is in
`task-01-final-tests.log`.

## Build and install evidence

Build command:

```powershell
.\.venv\Scripts\python.exe -m build
```

Relevant result: `Successfully built repo2skill-0.1.0.tar.gz and
repo2skill-0.1.0-py3-none-any.whl`, exit 0. The build used setuptools 84.0.0
and wheel 0.48.0 in isolated environments; output is in `task-01-build.log`.

`python -m zipfile -l` and `tar -tf` confirmed both artifacts contain
`THIRD_PARTY_NOTICES` and the expected package/config files. A fresh temporary
venv then installed the wheel with `pip install --no-deps`; the installed
console script returned `repo2skill 0.1.0` for `--version` and showed only
`-h/--help` and `--version` in help. The distribution test also reads both
notices, checks the full upstream copyright line, and asserts byte-for-byte
equality.

The first combined inspection wrapper used the invalid PowerShell parameter
`Select-Object -Single 1`; the build had succeeded, but artifact inspection
and installation did not run. That wrapper is not counted as smoke evidence.
The successful rerun used these commands with explicit exit checks:

```powershell
$wheel=(Get-ChildItem 'dist\*.whl' | Select-Object -First 1).FullName
$sdist=(Get-ChildItem 'dist\*.tar.gz' | Select-Object -First 1).FullName
.\.venv\Scripts\python.exe -m zipfile -l $wheel
tar -tf $sdist
python -m venv $smoke
& $smokePython -m pip install --no-deps $wheel
& $smokeCli --version
& $smokeCli --help
```

## Files changed

- Packaging: `.gitignore`, `.gitattributes`, `pyproject.toml`, and
  `THIRD_PARTY_NOTICES`.
- Package: `src/repo2skill/{__init__,__main__,cli,config,models}.py`.
- Audit record: `docs/engineering/upstream-source-map.md`.
- Behavioral verification: `tests/test_config.py` and
  `tests/test_distribution.py`.
- Execution record: this report; verbose logs remain ignored beside it.

## Self-review

Reviewed the staged files against F001/F002, the task brief, the canonical
three dataclass field sets, CLI contract path rules, and the controller's
default rulings. `git diff --cached --check` passed. The upstream MIT text was
compared to the audited checkout after canonical CRLF-to-LF conversion and
matched in full. The review found and fixed the two boundary bugs documented
in the RED/GREEN section; no unresolved important finding remains.

## Concerns

One test parameter is skipped on Windows because `pathlib.Path` normalizes
forward and backslash separators into the same value before `resolve_under`
can inspect the spelling. The persisted `TargetSpec.livePath` string rejects
backslashes on every platform, and the same `resolve_under` parameter runs on
POSIX where a backslash remains observable. The Windows run did exercise
drive escapes and a real symlink/reparse ancestor, including a missing leaf.

## Documentation handoff

After the implementation commit, `README.md` was updated to describe the
actual P0-01 milestone and give a Windows local-development quickstart. It
states that only global `--help` and `--version` are runnable and keeps every
generation/lifecycle command marked as future work. The P0 plan's top and
bottom status lines now record Task P0-01 as implemented at `9bb120b` pending
independent review, with P0-02 through P0-12 unexecuted. No task checkbox was
changed, and no package or test file was edited for this documentation-only
handoff.
