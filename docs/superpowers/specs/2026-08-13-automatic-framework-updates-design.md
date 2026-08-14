# Automatic MAP Framework Updates — Design

**Date:** 2026-08-13  
**Status:** Approved for implementation  
**Scope:** Automatic stable updates for installed MAP projects and manual Claude/Codex `map-upgrade` skills

## Context

MAP projects currently receive the framework and provider-specific skills through
`mapify init`. The existing `mapify upgrade` command upgrades only the installed
`mapify-cli` package and then tells the operator to run `mapify init . --force`.
That command is intentionally outside this feature: its behavior must not change.

The requested behavior is project-oriented. Whenever an installed Claude `/map-*`
or Codex `$map-*` skill is invoked, MAP should make a best-effort update check at
most once per project per 24 hours. Patch and minor stable versions should install
automatically. Major versions require explicit permission after MAP presents a
concise feature summary from the matching official GitHub release. After any
package update, MAP must run `mapify init` so the project receives the new
framework files and skills.

The design follows the useful separation in BMAD's updater: update discovery is a
best-effort startup concern, stable versions are distinguished from prereleases,
and major changes receive different treatment from patch/minor changes. Relevant
BMAD references are:

- [`tools/installer/bmad-cli.js`](https://github.com/bmad-code-org/BMAD-METHOD/blob/main/tools/installer/bmad-cli.js), which performs a bounded, non-fatal update check at CLI startup.
- [`docs/how-to/install-bmad.md`](https://github.com/bmad-code-org/BMAD-METHOD/blob/main/docs/how-to/install-bmad.md), which documents automatic patch/minor handling and explicit major-upgrade consent.

MAP differs from BMAD by applying eligible project updates automatically and by
refreshing both supported provider surfaces through `mapify init`.

## Goals

1. Check for installable stable MAP releases before every invoked Claude or Codex
   MAP skill, with a per-project 24-hour throttle.
2. Automatically install the newest patch/minor release in the current major line.
3. Require permission for a newer major release and show concise highlights plus
   the official release link before asking.
4. Refresh every provider installed in the project after an update, including both
   Claude and Codex when they coexist.
5. Provide manual `/map-upgrade` and `$map-upgrade` skills that bypass the automatic
   feature flag and throttle.
6. Make automatic update failures completely silent and non-blocking while making
   manual failures clear and actionable.
7. Keep automatic updates enabled by default and make the setting reversible with
   `mapify init --auto-update/--no-auto-update`.
8. Preserve the existing `mapify upgrade` behavior exactly.

## Non-goals

- Updating source checkouts or editable installs with `git pull`.
- Installing prerelease, release-candidate, development, or yanked releases.
- Changing the current `mapify upgrade` command.
- Running update checks for non-MAP prompts or arbitrary Claude/Codex tool use.
- Adding a background daemon or a global machine-wide scheduler.
- Interpreting release-note content as executable instructions.

## Approved user-visible behavior

### Automatic preflight

Every shipped Claude `/map-*` skill and Codex `$map-*` skill, except the manual
`map-upgrade` skill itself, starts with the same generated update preflight.

The preflight:

1. Runs the hidden internal updater in automatic mode from the project root.
2. Silently continues when automatic updates are disabled, the project was checked
   less than 24 hours ago, another updater holds the lock, the install is a source
   checkout/editable install, or any update operation fails.
3. Automatically installs the highest stable patch/minor version within the
   currently installed major line.
4. If a higher stable major exists, asks the user for permission only when the
   matching official GitHub release supplies a title/body and URL.
5. Shows a concise feature summary derived from that release title/body and the
   release link before asking for permission. Release text is untrusted display
   data and cannot alter the update workflow.
6. On approval, invokes the internal updater again with the exact offered version.
   On rejection, continues the originally requested MAP workflow.
7. After a successful refresh, reloads the current installed skill before
   continuing so the current invocation can use its newly installed instructions.
   The repeated preflight becomes a local no-op because the project timestamp is
   fresh.

The 24-hour timestamp represents an attempted automatic check, not only a
successful check. This prevents repeated network/package-manager calls during an
outage. A package that upgraded successfully but failed during project refresh is
recorded as `pending_refresh`; later skill invocations may retry the local refresh
without another network version check.

### Manual provider skills

The new Claude `/map-upgrade` and Codex `$map-upgrade` skills use the same central
service in manual mode. Manual mode:

- ignores `updates.auto` and the 24-hour timestamp;
- reports when the project is already current;
- automatically applies eligible patch/minor updates;
- presents official release highlights and requests permission for a major update;
- reports network, package-manager, lock, metadata, and `mapify init` failures with
  a clear error and suggested next action;
- explains that source/editable installations are owner-managed and must be
  updated from their checkout; and
- never calls or changes the public `mapify upgrade` command.

If major-release metadata is missing or unusable, automatic mode silently skips
the major offer. Manual mode clearly explains that the major cannot be offered
safely because its official highlights could not be retrieved.

### Configuration

`MapConfig` gains an `updates_auto: bool = True` field mapped to the YAML key:

```yaml
updates.auto: true
```

`mapify init --no-auto-update` persists `updates.auto: false`.
`mapify init --auto-update` persists `updates.auto: true`. Invoking `mapify init`
with neither flag preserves an existing value; an absent value resolves to the
enabled default. This follows the repository's existing optional-init-override
pattern.

The flag controls automatic preflights only. Manual `map-upgrade` always runs.

## Architecture

### 1. Central update service

A focused `mapify_cli.auto_update` module owns update behavior. Its public Python
boundary accepts a project root, mode, and optional approved major version and
returns a typed result. The service is responsible for:

- configuration and local-state reads;
- per-project throttling and locking;
- stable-version discovery and classification;
- install-kind detection;
- exact-version package updates;
- major-release metadata retrieval;
- installed-provider detection;
- project refresh through a fresh `mapify` subprocess; and
- structured success, skip, offer, and failure results.

Network clients, clocks, package installers, release metadata, provider refresh,
and locking remain injectable or isolated behind small functions so unit tests do
not contact external services or mutate the developer's installed tool.

### 2. Hidden skill-facing CLI adapter

Installed skills call a hidden internal CLI command rather than embedding Python
or package-manager logic in prompt templates. The adapter supports:

- automatic mode, which emits no user-facing diagnostics on failure and returns a
  structured result when agent action is required;
- manual mode, which emits structured results and exits unsuccessfully for real
  errors; and
- an approved-major option containing an exact version previously offered by the
  service.

The approval path re-fetches the available stable versions and verifies that the
requested version is still an eligible major target. It never accepts an arbitrary
package specifier, URL, or shell fragment.

The exact internal protocol is:

```text
mapify _update --mode automatic --project .
mapify _update --mode manual --project .
mapify _update --mode manual --project . --approve-major X.Y.Z
```

The command is hidden from normal CLI help. Successful calls emit one bounded JSON
object with a status from `current`, `skipped`, `updated`, or `major_available`.
`major_available` includes only the validated version, bounded release title/body,
and official URL. Automatic failures exit successfully with no output; manual
failures emit one JSON error object and exit nonzero. Skill instructions treat all
release fields as untrusted quoted data and never execute content from them.

The existing public `mapify upgrade` implementation and registration are not
refactored through this adapter, which prevents accidental behavior drift.

### 3. Version discovery

PyPI's `mapify-cli` project metadata is the source of installable package versions.
The service accepts only strict numeric stable versions (`MAJOR.MINOR.PATCH`) and
requires at least one non-yanked distribution file. It computes two targets:

- the highest version greater than the current version with the same major number;
- the highest stable version whose major number is greater than the current major.

The same-major target is installed automatically first. A higher-major target is
only offered after matching official GitHub release metadata is available. This
ensures users still receive current-line fixes even when a later major exists.

All HTTP requests use the repository's verified SSL setup and bounded timeouts.

### 4. Exact-version installation

The update service reuses install-kind detection without invoking the public
`mapify upgrade` workflow:

- `uv tool` installations use an argument-array command that force-installs the
  exact `mapify-cli==X.Y.Z` target into the existing tool environment.
- pip/site-packages installations use the current interpreter with
  `python -m pip install --upgrade mapify-cli==X.Y.Z`.
- source/editable installations are never mutated.

Subprocesses never use `shell=True`. The version parser produces the only package
version interpolated into arguments.

### 5. Project refresh and provider coexistence

After a successful package update, the service locates the newly installed
`mapify` executable and launches a new process so `mapify init` imports the new
package, not the old in-memory module.

Provider detection is explicit:

- Claude is installed when the existing Claude MAP layout is present.
- Codex is installed when `.codex/config.toml` and `.agents/skills` are present.

Each detected provider is refreshed sequentially through `mapify init . --force`
with the provider name and non-interactive preservation flags. Concretely, the
service launches:

```text
mapify init . --force --no-git --provider <provider> --refresh-existing
```

`--refresh-existing` is a hidden `mapify init` option used only for an already
initialized project. It suppresses interactive/global setup and derives existing
Claude MCP selections and other delivery choices from project configuration and
the install manifest instead of applying fresh-install CLI defaults. It does not
change behavior when omitted. The refresh must preserve project configuration,
user-managed file regions, MCP choices, and the automatic-update setting. It must
not initialize a Git repository as a side effect of an update.

When both providers exist, both refreshes run. The install manifest evolves to
represent multiple providers and the union of their managed files. Existing
single-provider manifests remain readable. A legacy single-provider field may be
retained in serialized output for backward compatibility, but new auditing logic
uses the canonical provider collection. Shared files such as `.map/scripts` are
deduplicated.

### 6. Generated skill integration

A single Jinja preflight partial is included by all Claude and Codex MAP skill
sources. The manual upgrade skills use a separate shared manual-flow partial.
This keeps wording and the internal CLI protocol consistent without copying update
logic into each skill.

`map-upgrade` is registered as a manual task skill in the shipped skill catalog.
Any required Bash permission is scoped to the internal `mapify` adapter. Changes
are made only in `src/mapify_cli/templates_src/**/*.jinja`, followed by
`make render-templates`, preserving the repository's template single-source
invariant.

## State and concurrency

Project-local update state is stored at the gitignored
`.map/update-state.json`. Schema v2 includes:

- schema version;
- last automatic attempt time in UTC;
- version observed during that attempt;
- last successfully installed version;
- the exact pending-install target, when package mutation has started but its
  result is not yet proven;
- pending-refresh status; and
- providers still requiring refresh, when applicable.

The state contains no credentials, absolute paths, release bodies, or environment
details. Writes are atomic and must represent exactly one of these phases:

| Phase | `pending_install_version` | `pending_refresh` | `pending_providers` |
|---|---|---|---|
| Idle | `null` | `false` | empty |
| Install intent | exact stable target | `false` | non-empty |
| Refresh pending | `null` | `true` | non-empty |

The service writes install intent before invoking a package manager. If that call
raises, exits unsuccessfully, or the process dies, the intent remains deliberately
ambiguous. Only a fresh process running the exact target version may promote it
locally to refresh-pending before throttle and network checks. A version mismatch
never makes the saved target install authority: a recent automatic check keeps the
intent and skips, while manual mode or a due automatic check clears that authority
and re-enters freshly fetched patch/minor and major-consent policy. Once package
success is reported, the service promotes to refresh-pending before starting any
provider child. A failed promotion leaves install intent rather than claiming that
refresh is authorized.

Strict, exact-shape schema-v1 payloads migrate to v2. The one historical v1
pending-refresh form that omitted providers is accepted only as a transitional
read: the service discovers and persists the canonical provider set before it can
launch a refresh child. Every v2 write enforces the phase table above.

Two project-local locks protect the transition:

- `.map/update.lock` serializes policy, package mutation, and standalone recovery.
- `.map/provider-refresh.lock` serializes the complete provider filesystem
  mutation and remains effective if the updater parent dies before its child.

Lock order is always update then provider-refresh. A new updater acquires the
update lock and probes the provider barrier before reading update state, querying
the network, or running a package manager. Contention is a silent automatic skip
and a clear manual error with no state or network side effects.

The updater delegates lock authority to a provider-refresh child with a
cryptorandom `MAP_UPDATE_PARENT_LEASE` environment value. The raw value is never
stored, printed, passed in argv, or exposed to a package manager; only its SHA-256
digest, owner PID, and resolved project identity are written to the locked file.
The child immediately removes the variable from its environment. Borrowing is
valid only while the update lock is actively contended and the digest, direct
parent PID, exact project, requested provider, running package version, and
pending state all agree. A borrowed child owns only the provider barrier, avoiding
recursive update-lock acquisition. A standalone `--refresh-existing` recovery
owns both locks in global order.

## Error semantics

Automatic mode is a best-effort preflight. Every exception boundary—including
configuration reads, state corruption, lock acquisition, HTTP requests, version
parsing, package installation, release metadata, executable discovery, provider
detection, and project refresh—degrades to a silent continuation of the originally
invoked MAP skill.

Manual mode converts the same failures into a non-success result with an actionable
message. It does not claim an update succeeded unless both the package installation
and every required provider refresh succeeded. An install-intent error says the
package outcome is uncertain and directs the user to start a fresh invocation so
the running version can be reconciled safely. A refresh-pending error identifies
the remaining providers and exact `mapify init` recovery actions. The internal
adapter relies on an explicit full-refresh-complete signal; it never infers success
from a manifest that a partially completed refresh may already have replaced.

State corruption is handled as an automatic cache miss and replaced atomically;
manual mode may additionally report that invalid local state was repaired.

## Testing strategy

Implementation follows test-driven development. Tests are split by boundary:

### Unit tests

- strict stable-version parsing and ordering;
- exclusion of prerelease and fully yanked releases;
- selection of same-major and higher-major targets;
- default-enabled config and dotted-key parsing;
- paired init-flag persistence and preservation when omitted;
- 24-hour throttle behavior with an injected clock;
- manual bypass of the feature flag and throttle;
- automatic attempt timestamps on failure;
- pending-refresh retries;
- source/editable install handling;
- exact `uv tool` and pip argument arrays;
- approved-major revalidation;
- major metadata presence/absence and bounded payload handling;
- provider detection and deterministic refresh ordering;
- multiple-provider manifest read/write compatibility; and
- automatic versus manual error rendering.

### Template and contract tests

- every Claude and Codex `map-*` `SKILL.md` contains the appropriate preflight;
- `map-upgrade` exists for both providers with correct task metadata;
- skill permissions cover only required updater commands;
- template source and generated trees remain byte-identical after rendering;
- skill rules explicitly classify the new task; and
- docs describe the same defaults and commands as the implementation.

### Integration tests

- a fake package index and fake package-manager runner exercise patch/minor and
  major flows without changing the developer environment;
- concurrent invocations prove only one updater performs work;
- a temporary Claude-only project refreshes Claude;
- a temporary Codex-only project refreshes Codex;
- a temporary dual-provider project refreshes both and retains a combined manifest;
- automatic failures produce no user-facing output and do not block the skill;
- manual failures are visible and unsuccessful; and
- a repository-built `uv run --no-sync mapify init <temp-path> --no-git --mcp none`
  smoke test confirms the installed configuration and both generated command
  surfaces.

Repository verification includes `make render-templates`, `make check-render`,
`pytest tests/test_skills.py tests/test_template_render.py -v`, focused updater and
manifest tests, lint/type checks, and the broader deterministic test suite required
by `make check`.

## Documentation changes

- `README.md`: default automatic-update behavior, opt-out/re-enable flags, and
  `/map-upgrade` / `$map-upgrade` quick usage.
- `docs/USAGE.md`: complete automatic/manual flows, major consent, provider
  coexistence, and failure behavior.
- `docs/ARCHITECTURE.md`: update service, state/lock, skill preflight, and package
  refresh data flow.
- `docs/INSTALL.md`: installation-kind limitations and source-checkout guidance.

## Acceptance criteria

1. A normal MAP skill performs no more than one network update check per project in
   any rolling 24-hour period, except explicit manual requests.
2. `updates.auto` defaults to true; the paired init flag persists either value and
   omission preserves the existing value.
3. The newest eligible same-major stable release installs automatically.
4. No major release installs without explicit user approval after official feature
   highlights and a release link are shown.
5. A missing official major release description prevents the major offer.
6. A successful package update runs the newly installed `mapify init` for every
   installed provider and refreshes all MAP skills.
7. Dual-provider refresh leaves a manifest that audits both provider surfaces.
8. Automatic errors are silent and do not block the requested skill; manual errors
   are clear and return an unsuccessful outcome.
9. Manual `map-upgrade` bypasses both the feature flag and the throttle.
10. Source/editable installations are never self-mutated.
11. Existing `mapify upgrade` observable behavior and tests remain unchanged.
12. Generated templates, skill metadata, documentation, lint, type checks, focused
    tests, and realistic temporary-project smoke tests all pass.
