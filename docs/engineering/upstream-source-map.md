# Upstream source and dependency map

This map records the reuse boundary for `repo2skill`. The audited upstream is
[`osmaneb23/agents-md`](https://github.com/osmaneb23/agents-md) at immutable
commit `346b5f78dcbb48d9a700df98ac7f5f8e02f751ea`. Its full MIT notice is
preserved in `THIRD_PARTY_NOTICES` and packaged in every wheel and source
distribution.

## Current reuse map

No upstream source file or translated rule is copied or adapted in P0-01.
The package bootstrap, frozen policy records, defaults, and path confinement
code were implemented locally from the repo2skill design contract. Future
reuse must add a row before adapted code is committed.

| Upstream path | Pinned commit | Local path/rule | Reuse form | Verification |
|---|---|---|---|---|
| _None in P0-01_ | `346b5f78dcbb48d9a700df98ac7f5f8e02f751ea` | _None_ | No source copied | Distribution test verifies the notice |

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
