# repo2skill

repo2skill is an unpublished local alpha that turns repository files into reviewable, source-linked guidance for coding agents. It records byte hashes and evidence locators, derives claims with explicit provenance, stages `AGENTS.md` and an Agent Skill, and exports only content covered by its ownership manifest. The P0 workflow is no-AI by default and does not execute repository commands or request the network during product operations.

The current alpha supports shallow JavaScript/TypeScript, Python, Go, and Rust extraction, plus generic configuration and CI coverage. Unsupported or incomplete inputs remain visible as coverage information. Hashes and locators document the bytes observed; they do not prove a claim is complete or semantically correct. Review generated claims before export.

## Install for use

Python 3.11 or newer is required. For a command-line install from this source checkout, install pipx and use it to install the project:

```powershell
python -m pip install --user pipx
python -m pipx install .
python -m pipx ensurepath
```

Close and reopen the terminal so the updated PATH takes effect. Then run `repo2skill --help`, or use `repo2skill init` and `repo2skill generate --no-ai` from the repository you want to process. This alpha is not published to PyPI, so `pip install repo2skill` is not available. You can also install a locally built wheel with `python -m pipx install "path/to/repo2skill-0.1.0-py3-none-any.whl"`.

Package setup may contact the configured package index to install dependencies. This is separate from repository operations: generation and freshness checks do not use AI or request the network.

## Maintainer quickstart

From a checkout, install the package and development tools in an editable virtual environment, then inspect the available commands:

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

## Commit maintenance quickstart

The optional local maintenance workflow follows committed Git changes and queues bounded update jobs for an installed headless agent. From a Git repository, initialize and enable it, then establish the first baseline explicitly:

```powershell
repo2skill init
repo2skill maintain setup
repo2skill maintain catch-up --rebuild
repo2skill maintain status
repo2skill maintain approve <job-id>
repo2skill maintain status
```

Catch-up reads the committed `HEAD`; dirty and untracked working-tree edits are not model input. It queues an approval-gated job and never advances the applied baseline itself. A successful verified apply advances the baseline. `maintain status` shows the observed hook cursor separately from that baseline. The P0 `check` command remains read-only. Supported hooks observe commit, merge, checkout, and rewrite events, but hooks cannot observe every repository change, so run `maintain catch-up` after missed or unsupported events.

By default, review the exact job and approve it before the agent call. Approval directly authorizes the validated bounded apply; there is no second draft-review approval. `maintain setup --auto-approve` opts into a private revocable grant for newly observed eligible jobs only. To stop workers, run `maintain disable`; it keeps queue/history and output files while removing only unchanged hooks owned by repo2skill. An existing unowned `SKILL.md` or `repo2skill-provenance.json` at the configured Skill output path is reported as a conflict before the agent is probed or invoked and remains unchanged. Other files in that directory are preserved. A project-owned `AGENTS.md` block can be added while preserving bytes outside its markers.

Maintenance records live under `<stateDir>/maintenance/` with schema 1.0, separate from P0 runs and manifests. The schema guide covers reopening old P0 1.0 state and recovery. Current local limits are 256 committed files, 2 MiB per file, 16 MiB total source context, and required source fragments capped at 256 lines / 32 KiB each. In this alpha, a required file that exceeds its fragment cap is rejected before any AI call; richer changed-region selection is not implemented. Keep the project as it is—the limits are tool behavior, not a reason to refactor your repository to fit the tool. Captured hashes, locators, support IDs, authorization references, and ownership checks establish provenance and binding, not semantic truth of model-written prose. The adapter requests bounded structured output and read-only operation; it does not claim complete isolation from local authentication or instruction loading. See the [maintenance CLI contract](docs/engineering/cli-contract.md#maintain) for exit codes and diagnostics, the [maintenance acceptance record](docs/engineering/maintenance-acceptance.md) for tested behavior and limits, and the [scoped maintenance review](docs/engineering/maintenance-final-review.md) for the final reviewed runtime.

## Verification status

The current local verification host is Windows/Python 3.13. The CI workflow is configured for Windows, Linux, and macOS on Python 3.11 and 3.13; those six matrix results remain pending until the workflow runs. Version `0.1.0` has not been published to a package index.

Public documentation: [installation](docs/install.md), [CLI contract](docs/engineering/cli-contract.md), [threat model](docs/threat-model.md), [schema compatibility](docs/schema-migrations.md), and [contributing](CONTRIBUTING.md). [Contributors](CONTRIBUTORS.md) records maintainer and AI assistance; the [upstream source map](docs/engineering/upstream-source-map.md) documents pinned MIT material and the retained notice.
