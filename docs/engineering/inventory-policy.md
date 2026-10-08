# Inventory and repository identity policy

The P0 inventory is a deterministic metadata scan. It records regular-file and redirect metadata, but it does not open source or binary files to hash them. `contentHash` is normally null. A detected `.gitignore` is the exception: the scanner captures it once under the configured read budgets, derives both its inventory hash and its control observation from that same stable buffer, and never transfers the bytes to later stages.

## Eligibility and precedence

Paths are normalized to NFC with `/` separators. The scanner sorts canonical paths before selecting the `maxInventoryEntries` prefix and rejects case-folded or Unicode-normalized collisions. It never follows a symlink, junction, or other reparse point. Redirect metadata is recorded with `kind="symlink"`; a target outside the scan root is represented as `<external-target>` rather than exposing an absolute path.

Eligibility is decided in this order:

1. `.git`, `.repo2skill`, `.superpowers`, configured `excluded_paths`, nested repositories, Git submodules, mount boundaries, and redirected ancestors are final boundaries.
2. Explicit `ScanPolicy.exclude` patterns, generated paths, and security-sensitive paths are final exclusions. Policy separators are normalized before matching, and explicit excludes are positive gitignore-style patterns without negation.
3. An exact normalized include may restore an untracked path excluded only by an ordinary root-confined `.gitignore`. An include glob never bypasses ignore rules. Tracked Git paths and the ancestor directories needed to reach them remain eligible despite ordinary ignore rules.
4. Otherwise, root-confined per-directory `.gitignore` sources apply in hierarchy and last-match order. An ignored directory is pruned, so a descendant control cannot re-include a path after its parent was excluded.

P0 does not read `.git/info/exclude`, `core.excludesFile`, or global ignore sources. Git may report a path as ignored because of one of those external inputs; if the P0 inventory still considers that path eligible, repository identity classifies it as an ordinary untracked input.

An include value beginning with `literal:` names an exact repository path even when the suffix contains glob metacharacters, for example `literal:ignored/a[b].txt`. Windows separators in that suffix are normalized before path validation. Unprefixed values without glob metacharacters are also exact paths; unprefixed glob values are patterns and never override ordinary ignore rules.

Generated directory names are `node_modules`, `.venv`, `venv`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.nox`, `dist`, `build`, `target`, `vendor`, `.next`, `.nuxt`, and `coverage`. Generated file suffixes are `.pyc`, `.pyo`, `.min.js`, `.min.css`, and `.map`. Lockfiles remain eligible.

Security exclusions are `.env` and `.env.*` except `.env.example`, `.env.sample`, and `.env.template`; directories `.ssh`, `.aws`, and `.gnupg`; files `.git-credentials`, `.netrc`, `.npmrc`, `.pypirc`, `id_rsa`, and `id_ed25519`; and suffixes `.pem`, `.key`, `.p12`, and `.pfx`.

## Bounds and control observations

The default caps are 100,000 inventory entries, 5,000 opened files, 2 MiB per opened file, 64 MiB of decoded input, and 200 KiB of snippets per run. Inventory only spends opened-file and decoded-byte budgets on ignore controls. A control file is charged once: opening its handle charges one opened file, while only a complete successful decode charges its full raw-byte length. A cap or read/decode failure emits a required diagnostic and coverage gap, retains the detected control path, and fails closed for paths whose eligibility depends on that control.

With a control sink, each visited directory contributes at most one `.gitignore` file observation. File observations precede the final required `**/.gitignore` aggregate observation. The aggregate records a stable hash of the sorted complete match set, including the empty set. Any control or traversal failure changes it to `capped` or `io-error` with no match-set hash.

`repo2skill.safe_fs.open_directory(path)` returns a context-managed directory guard. `GuardedDirectory.names()`, `lstat(name)`, `open_directory(name)`, `open_file(name)`, and `readlink(name)` are the supported operations for safe traversal. Child names are single path components. `GuardedFile.read_bounded(limit)` and `stable_after_read()` support bounded same-handle reads. On POSIX these operations are descriptor-relative with no-follow flags; on Windows the guards retain handles without delete sharing and validate reparse attributes and final paths before returning. Keep ancestor guards open while traversing descendants.

Inventory diagnostic and coverage reason codes use the `inventory.*` namespace. `inventory.entry-cap` and `inventory.gitignore-control-*` represent required incomplete scans; `inventory.path-collision` is fatal. Boundary, generated, security, explicit-exclude, ordinary-ignore, special-file, and redirect skips use `inventory.boundary-excluded`, `inventory.generated-excluded`, `inventory.security-excluded`, `inventory.explicit-excluded`, `inventory.gitignored`, `inventory.special-file`, and `inventory.redirect-not-followed`.

Mount detection uses device changes and the host's `ismount` support. This reliably catches ordinary mount points but cannot portably identify every same-device bind mount. The boundary helper is isolated so a platform-specific source such as Linux mount information can extend it later.

## Repository identity

Git reads use fixed built-in argv with `shell=False`, bounded time/output, `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`, and `-c core.fsmonitor=false`. The scanner does not invoke aliases, hooks, filters, textconv, submodule recursion, or repository commands.

Inside Git, `auto` resolves to `worktree`; outside Git it resolves to `no-git`. An unborn worktree is valid with no commit. Explicit `head` requires an existing commit and no eligible tracked, untracked, or exactly included ignored changes. Explicit `worktree` outside Git is an `identity.not-git` error. Configured state and live output paths are removed from dirty classification, including deleted tracked paths.

Git repository IDs use `canonical_record_id("repo", {"kind": "git", "commonDir": ..., "scanRoot": ...})`. `commonDir` is an NFC, separator-normalized, platform-case-normalized absolute Git common directory. `scanRoot` is the NFC path from the Git toplevel to the scan root, or `.` at the toplevel. Linked worktrees at the same relative scan root therefore share an ID, while scans of different repository subdirectories do not.

No-Git IDs use `canonical_record_id("repo", {"kind": "no-git", "projectId": ..., "root": ...})` with the similarly normalized absolute runtime root. Absolute identity inputs are used only to derive the ID; source records retain normalized repository-relative paths.
