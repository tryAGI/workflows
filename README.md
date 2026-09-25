# tryAGI Reusable Workflows

Centralized GitHub Actions reusable workflows for the [tryAGI](https://github.com/tryAGI) organization.

## Available Workflows

### mkdocs-pages.yml

Builds and deploys a MkDocs site to GitHub Pages using the shared tryAGI docs theme.

**Inputs:**

| Input | Required | Description |
|-------|----------|-------------|
| `theme-source` | No | Pip install target for the shared theme package. Defaults to `tryAGI/docs` `theme/` on `main`. |
| `run-docs-sync` | No | Runs `autosdk docs sync .` before the build. Defaults to `true`. |
| `docs-sync-command` | No | Override for the docs sync command. |
| `dotnet-version` | No | .NET SDK version used when docs sync is enabled. |
| `python-version` | No | Python version used for MkDocs. |
| `site-dir` | No | Output directory for the built site. |

**Usage:**
```yaml
name: MKDocs Deploy
on:
  workflow_dispatch:
  push:
    branches:
      - main
    paths:
      - 'docs/**'
      - 'mkdocs.yml'
      - 'autosdk.docs.json'
      - '.github/workflows/mkdocs.yml'
      - 'src/tests/IntegrationTests/**'
      - 'README.md'

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: "pages"
  cancel-in-progress: false

jobs:
  deploy:
    uses: tryAGI/workflows/.github/workflows/mkdocs-pages.yml@main
    secrets: inherit
```

**Behavior:**
- Checks out the caller repository
- Optionally runs `autosdk docs sync`
- Installs `mkdocs-material`, `mkdocs-copy-to-llm`, and the shared `tryagi` theme package
- Builds the site and deploys it to GitHub Pages

### auto-merge.yml

Auto-approves and squash-merges pull requests from trusted actors (Dependabot, HavenDV).

**Usage:**
```yaml
name: Auto-approve and auto-merge bot pull requests
on:
  workflow_run:
    workflows:
      - Test
    types:
      - completed

permissions:
  contents: write
  pull-requests: write

jobs:
  auto-merge:
    uses: tryAGI/workflows/.github/workflows/auto-merge.yml@main
    secrets: inherit
```

**Behavior:**
- Runs in a privileged base-branch context only after the unprivileged `Test` workflow completes successfully
- Resolves the associated PR through the Actions API and re-verifies its author, base branch, draft/state, and exact tested head SHA
- Accepts only non-draft PRs from `dependabot[bot]` or `HavenDV` targeting `main`
- Only runs for repos owned by `tryAGI`
- Never checks out PR code or consumes artifacts from the unprivileged workflow
- Auto-approves the PR
- Enables auto-merge with squash strategy

### auto-update.yml

Checks for OpenAPI spec updates, regenerates SDK code, and opens a PR if changes are detected.

**Inputs:**

| Input | Required | Description |
|-------|----------|-------------|
| `library-path` | Yes | Path to the SDK library directory (e.g., `src/libs/Anthropic`) |

**Secrets:**

| Secret | Source | Description |
|--------|--------|-------------|
| `PERSONAL_TOKEN` | Org secret via `secrets: inherit` | GitHub token with repo permissions for creating PRs |

**Usage:**
```yaml
name: Opens a new PR if there are OpenAPI updates
on:
  schedule:
    - cron: '0 */3 * * *'
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write
  actions: write

jobs:
  auto-update:
    uses: tryAGI/workflows/.github/workflows/auto-update.yml@main
    with:
      library-path: src/libs/MySDK
    secrets: inherit
```

> **Important:** The caller MUST declare `permissions` at the workflow level. Reusable workflows cannot escalate permissions beyond what the caller grants.

**Behavior:**
- Checks out the repo and creates a timestamped branch
- Sets up .NET 10.0
- Runs `generate.sh` in the specified `library-path` directory
- If changes are detected, commits, pushes, and creates a PR
- Uses concurrency control (`auto-update` group) to prevent parallel runs

## Consuming These Workflows

1. Create the caller workflow in your repo's `.github/workflows/` directory
2. Use `uses: tryAGI/workflows/.github/workflows/<workflow>.yml@main`
3. Add `secrets: inherit` to pass organization secrets
4. Set required `permissions` in the caller workflow

## Automatic stable SDK releases

`generated-sdk-publish.yml` classifies the tested NuGet packages after its build, tests, and trim check succeed on `main` when the caller sets `auto-stable-release: true`. This input defaults to `false` so the shared workflow can be deployed before callers opt in. Classification produces a downloadable `release-impact-report` artifact and a job summary. `publish-stable` defaults to `false`; set it to `true` for a caller only after reviewing its report. Other SDK pipelines can call `auto-stable-release.yml` after their own gates. The shared `.NET` workflow exposes the same two inputs for callers that have a NuGet package artifact and `contents: write` permission.

The release job compares every package in the repository with its latest stable version on NuGet using the pinned Microsoft `ApiCompat` tool. Binary breaks and parameter-name changes that break named arguments select **major**. Compatible public additions select **minor**. Changes to package source without an API change select **patch**. Changes limited to tests, docs, or CI skip the stable release. The highest impact wins for a repository's package family. Repositories without a stable tag or published stable package start at `0.1.0`; while major is zero, breaking changes increment the minor component by default.

Behavioral, wire-format, and provider-side changes are not completely visible to an assembly comparison. Add a file under `.release-impact/` in the same PR when one of those changes needs a higher bump:

```json
{"bump":"major","reason":"The response JSON format changed for existing operations."}
```

Each changed file in that directory applies only to the next release after its commit. The classifier fails when the declaration has no reason or an invalid bump. It never lowers the bump detected by ApiCompat.

In publish mode, the stable job rebuilds with `MINVERVERSIONOVERRIDE` (plus explicit `Version` and `PackageVersion` for projects without MinVer), checks that the package IDs match the tested candidates, publishes to NuGet, waits for every version to appear on the public feed, and only then pushes the `vX.Y.Z` tag and creates the GitHub release. It skips a stale `main` run if a newer commit has arrived. A missing NuGet key skips stable publication and produces no tag. Calls using custom CI can set `candidate-source: build` and, if packaging is explicit, `package-command: pack`.

The release job creates the GitHub release itself because a tag pushed with the workflow's `GITHUB_TOKEN` does not start another workflow run. The normal `main` job continues to publish `-dev` packages.

## New SDK Projects

SDKs scaffolded with `autosdk init` automatically include caller workflows pointing to this repo.
