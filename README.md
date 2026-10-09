# repo2skill

repo2skill is an unpublished local alpha that turns repository files into reviewable, source-linked guidance for coding agents. It records byte hashes and evidence locators, derives claims with explicit provenance, stages `AGENTS.md` and an Agent Skill, and exports only content covered by its ownership manifest. The P0 workflow is no-AI by default and does not execute repository commands or request the network during product operations.

The current alpha supports shallow JavaScript/TypeScript, Python, Go, and Rust extraction, plus generic configuration and CI coverage. Unsupported or incomplete inputs remain visible as coverage information. Hashes and locators document the bytes observed; they do not prove a claim is complete or semantically correct. Review generated claims before export.

## Local quickstart

Python 3.11 or newer is required. From a checkout, install the package and development tools, then inspect the available commands:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\repo2skill.exe --help
```

Pip may use its configured package index during setup. This is separate from no-AI repository operations.

Start a local project lifecycle with:

```powershell
.\.venv\Scripts\repo2skill.exe init
.\.venv\Scripts\repo2skill.exe generate --no-ai
.\.venv\Scripts\repo2skill.exe review list --status pending
.\.venv\Scripts\repo2skill.exe review accept --claim <claim-id> --run <run-id>
.\.venv\Scripts\repo2skill.exe export --run <run-id>
.\.venv\Scripts\repo2skill.exe check
```

Accept only claims you have reviewed. `review list` and `generate` report the IDs needed by the review command. Export updates manifest-owned spans and preserves hand-written bytes outside them; conflicts stop the export for manual review.

## Verification status

The current local verification host is Windows/Python 3.13. The CI workflow is configured for Windows, Linux, and macOS on Python 3.11 and 3.13; those six matrix results remain pending until the workflow runs. Version `0.1.0` has not been published to a package index.

Read the [MVP design](docs/superpowers/specs/2026-10-07-repo2skill-mvp-design.md), [CLI contract](docs/engineering/cli-contract.md), and [feature catalog](docs/product/feature-catalog.md) for canonical behavior and scope. Maintainer guidance is in [installation](docs/install.md), [contributing](CONTRIBUTING.md), [threat model](docs/threat-model.md), [schema compatibility](docs/schema-migrations.md), and the [alpha release checklist](docs/release-checklist.md). The [upstream source map](docs/engineering/upstream-source-map.md) documents the pinned MIT material and retained notice.
