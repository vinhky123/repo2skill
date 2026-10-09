# Upstream source and dependency map

This map records the reuse boundary for `repo2skill`. The audited upstream is
[`osmaneb23/agents-md`](https://github.com/osmaneb23/agents-md) at immutable
commit `346b5f78dcbb48d9a700df98ac7f5f8e02f751ea`. Its full MIT notice is
preserved in `THIRD_PARTY_NOTICES` and packaged in every wheel and source
distribution.

## Current reuse map

No upstream source file is copied. P0-01 infrastructure was implemented
locally from the repo2skill design contract. P0-05 translates the audited
shallow scanning rules below into typed facts with local read provenance.

| Upstream path/rule | Pinned commit | Local path/rule | Reuse form | Verification |
|---|---|---|---|---|
| `scanner.py:56-73`, `scan_repo` four-ecosystem dispatch | `346b5f78dcbb48d9a700df98ac7f5f8e02f751ea` | `extractors/registry.py`, `ecosystem_extractors` | Translated dispatch; all four collectors survive in mixed repositories | `tests/extractors/test_mixed.py`; Task 12 `tests/integration/test_no_ai_alpha.py` |
| `scanner.py:152-205`, `_scan_javascript`; `413-426`, manager selection; `568-591`, script and single-test commands | `346b5f78dcbb48d9a700df98ac7f5f8e02f751ea` | `extractors/javascript.py`, `javascript.package` | Translated; literal package script is an observed `package-script.v1` task, manager-prefixed argv is a separate derived command, and source conventions are ledgered reads | `tests/extractors/test_javascript.py` |
| `scanner.py:323-373`, `_scan_javascript_conventions`; `496-508`, `_looks_like_api_wrapper` | `346b5f78dcbb48d9a700df98ac7f5f8e02f751ea` | `extractors/javascript.py`, HTTP client wrapper heuristic | Translated with a bounded source set and one source-line evidence record; glob/file reads affect the input digest | `tests/fixtures/ecosystems/javascript/src/client.ts`, `tests/extractors/test_javascript.py` |
| `scanner.py:208-241`, `_scan_python`; `446-456`, manager selection; `527-538`, project script; `594-631`, defaults | `346b5f78dcbb48d9a700df98ac7f5f8e02f751ea` | `extractors/python.py`, `python.project` | Translated; lock presence and manager selection have separate derivations, command defaults are templates, and manifest values are lossless explicit projections | `tests/extractors/test_python.py` |
| `scanner.py:791-818`, `_infer_python_single_test`; `831-849`, `_first_python_test_target` | `346b5f78dcbb48d9a700df98ac7f5f8e02f751ea` | `extractors/python.py`, pytest test-target heuristic | Translated through bounded AST parsing of a selected, ledgered test source and pytest configuration; no fixture/test code is executed | `tests/fixtures/ecosystems/python/tests/test_widget.py`, `tests/extractors/test_python.py` |
| `scanner.py:244-254`, `_scan_go` | `346b5f78dcbb48d9a700df98ac7f5f8e02f751ea` | `extractors/go.py`, `go.module` | Translated; module/dependency facts use source-line locators and fixed test commands remain templates | `tests/extractors/test_go.py` |
| `scanner.py:257-266`, `_scan_rust` | `346b5f78dcbb48d9a700df98ac7f5f8e02f751ea` | `extractors/rust.py`, `rust.cargo` | Translated; Cargo facts use safe TOML parsing, fixed commands remain templates, and integration-test convention cites its source | `tests/extractors/test_rust.py` |

The translated collectors parse only captured bytes obtained from
`ExtractionContext.read`, retain finalized source reads, allocate IDs through
`ExtractionSession`, and return typed records. Parse failures produce local
diagnostics and invalid coverage. Upstream silent parse failures, shell-string
commands, source fingerprint omissions, and first-command rendering behavior
are not carried over. The pinned MIT notice remains in `THIRD_PARTY_NOTICES`
and is packaged in every wheel and source distribution. The Task 12 mixed alpha
fixture verifies the public CLI provenance and export lifecycle across these
ecosystems. Release checks compare the installed wheel's notice bytes with the
repository notice, load both packaged templates, and verify the sdist contains
the integration tests and fixtures.

## Audited direct dependencies

Metadata was checked against the official PyPI project records on
2026-10-07 before setting bounded major-version ranges. “OS independent” is
the platform classifier supplied by each runtime dependency; repo2skill
itself targets Windows, Linux, and macOS on Python 3.11 or newer.

| Dependency | Audited release | repo2skill bound | License | Python | Platform metadata | Purpose |
|---|---:|---|---|---|---|---|
| [pathspec](https://pypi.org/project/pathspec/1.1.1/) | 1.1.1 | `>=1.1.1,<2` | MPL-2.0 | `>=3.9` | OS Independent; pure Python wheel | Gitignore-compatible matching |
| [PyYAML](https://pypi.org/project/PyYAML/6.0.3/) | 6.0.3 | `>=6.0.3,<7` | MIT | `>=3.8` | OS Independent; wheels for supported desktop platforms plus sdist | Bounded generic YAML parsing |

The build and test bounds were checked at the same time: setuptools 84.0.0
(MIT, Python `>=3.10`), wheel 0.48.0 (MIT, Python `>=3.9`), build 1.6.1
(MIT, Python `>=3.10`), and pytest 9.1.1 (MIT, Python `>=3.10`; classifiers
include Windows, macOS, POSIX, and Unix). They are development or build tools,
not runtime imports.

## Update rule

For each future adaptation, record the upstream path, pinned commit, local
path or rule ID, whether the reuse is copied or translated, and the fixture
that verifies it. Do not infer a stable upstream Python API from internal
modules.
