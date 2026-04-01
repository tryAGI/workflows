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
  pull_request:
    branches:
      - main

permissions:
  contents: write
  pull-requests: write

jobs:
  auto-merge:
    uses: tryAGI/workflows/.github/workflows/auto-merge.yml@main
    secrets: inherit
```

**Behavior:**
- Triggers on non-draft PRs from `dependabot[bot]` or `HavenDV`
- Only runs for repos owned by `tryAGI`
- Fetches Dependabot metadata (for Dependabot PRs)
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

## New SDK Projects

SDKs scaffolded with `autosdk init` automatically include caller workflows pointing to this repo.
