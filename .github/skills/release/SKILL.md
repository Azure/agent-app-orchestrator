---
name: release
description: Prepare and reconcile Agent Landing Zone Orchestrator releases, including authoritative version discovery, semantic version selection, release branches, VERSION and CHANGELOG updates, release notes, tags, GitHub Releases, and publication approval gates. Use when asked to prepare, version, tag, publish, roll back, or reconcile a release.
---

# Release

Prepare releases safely and reproducibly. Follow `.github/copilot-instructions.md`
and this workflow. This skill is self-contained; do not depend on another skill,
agent, or manifest.

## Safety boundary

Release preparation and release publication are separate phases.

- Preparation may inspect metadata, create `release/X.Y.Z` from `develop`,
  update `VERSION` and `CHANGELOG.md`, run existing validation, push the release
  branch, and open a pull request targeting `main`.
- Publication includes creating or pushing a tag, creating or publishing a
  GitHub Release, publishing a package or container image, or starting any
  deployment.
- Never perform a publication action without explicit human approval given
  after presenting the exact version, commit SHA, tag, release title, release
  notes, artifacts, and publication commands or destinations.
- Approval to prepare a release, merge a pull request, or "continue" is not
  approval to publish. Ask for publication approval as a distinct final gate.
- Never create, modify, close, merge, or otherwise interfere with an unrelated
  active release pull request or its workflow runs.
- Never create, modify, or deploy Azure resources as part of release
  preparation.

## 1. Establish the authoritative release state

Fetch current remote metadata without changing branches or tags:

```bash
git fetch origin develop main --tags --prune
git status --short
git branch --show-current
git log -1 --format=%H origin/develop
git log -1 --format=%H origin/main
git tag --list 'v[0-9]*' --sort=-version:refname
gh release list --limit 100
```

Read the root `VERSION` file and the versioned headings in `CHANGELOG.md`.
Determine the latest released version from all authoritative repository
surfaces:

1. SemVer tags matching exactly `vMAJOR.MINOR.PATCH`.
2. Published and draft GitHub Releases whose tag matches that format.
3. The root `VERSION` value matching exactly `MAJOR.MINOR.PATCH`.
4. Versioned `CHANGELOG.md` headings matching
   `## [vMAJOR.MINOR.PATCH] - YYYY-MM-DD`.

Do not infer the latest release from branch names, package metadata, commit
messages, pull request titles, or a single surface alone. Ignore prerelease
identifiers unless the human explicitly requests a prerelease workflow.

Compare versions using SemVer precedence, not lexical ordering. Record the
commit targeted by every relevant tag and release. If tags, releases,
`VERSION`, and changelog disagree, stop normal preparation and enter
reconciliation. Never silently select one conflicting value.

## 2. Select and verify the next version

Use Semantic Versioning:

- PATCH for backward-compatible fixes.
- MINOR for backward-compatible functionality.
- MAJOR for breaking changes.

The requested or proposed version must be strictly greater than the latest
released stable version and must not already exist as a tag, GitHub Release, or
remote release branch. Present the evidence and proposed increment when the
version was not supplied explicitly.

Use these exact forms for release `X.Y.Z`:

| Surface | Required value |
| --- | --- |
| Branch | `release/X.Y.Z` |
| `VERSION` | `X.Y.Z` |
| Changelog heading | `## [vX.Y.Z] - YYYY-MM-DD` |
| Tag | `vX.Y.Z` |
| GitHub Release title | `vX.Y.Z` |

The tag and GitHub Release title must contain no product prefix or suffix.

## 3. Prepare the release branch

Require a clean worktree. Create the release branch from the fetched
`origin/develop` commit, never from `main` or a local branch with unpushed
changes:

```bash
git switch --create release/X.Y.Z origin/develop
```

On the release branch:

1. Set root `VERSION` to `X.Y.Z` without a `v` prefix.
2. Convert the single top-level `## [Unreleased]` section in `CHANGELOG.md` to
   `## [vX.Y.Z] - YYYY-MM-DD`.
3. Do not leave any `[Unreleased]` heading on the release branch.
4. Do not add unrelated feature work.
5. Confirm branch name, `VERSION`, changelog version, release tag, and release
   title are exactly consistent.

After pushing the release branch, open a pull request from `release/X.Y.Z` to
`main`. The pull request must explain the release purpose, metadata changes,
validation results, and the separate post-merge publication approval gate.

Do not tag a release-branch commit. A release tag may target only the approved
commit on `main` after the release pull request is merged and the merge commit
has been verified.

## 4. Validate the release candidate

Discover and run the repository's existing validation rather than inventing
replacement checks. At minimum, run the current unit suite:

```bash
pytest -q
```

Also run any repository-provided Copilot asset validation, lint, build, or
release checks that exist at preparation time. Do not add tools merely to make
the release pass. Report every command and result. A failure blocks the release;
do not publish with a known failing gate unless a human supplies a documented,
repository-approved exception.

Before opening the pull request, explicitly verify:

- The branch is based on the current `origin/develop`.
- `VERSION` is exactly `X.Y.Z`.
- The changelog has exactly one `vX.Y.Z` heading and no `[Unreleased]`.
- No existing tag or release already uses `vX.Y.Z`.
- The diff contains only release preparation changes.

## 5. Generate safe release notes

Derive notes from the finalized changelog entry and commits included since the
previous release tag. Notes must be useful to repository users and safe for
public disclosure.

Remove or generalize:

- Private Azure subscription, tenant, resource group, resource, cluster,
  registry, vault, endpoint, environment, dashboard, incident, and internal
  service names.
- Internal aliases, email addresses, customer identifiers, access tokens,
  secrets, correlation identifiers, and non-public URLs.
- Private work-item links or operational details that expose internal
  infrastructure.

Preserve public GitHub issue and pull request references when appropriate.
Never copy hidden workflow output, credentials, or private Azure names into
release notes. If sanitization would make a statement misleading, omit it and
flag the omission for human review.

Present the complete sanitized notes for review before publication.

## 6. Publication approval gate

After the release pull request is merged, refresh `main` and verify the exact
target commit and all checks. Then present a publication plan containing:

- Version: `X.Y.Z`
- Commit SHA on `main`
- Tag and release title: `vX.Y.Z`
- Full sanitized release notes
- Every package, image, registry, or deployment destination, or `none`
- Exact publication actions to be performed

Ask a human to approve that publication plan explicitly. Without an
unambiguous approval for this exact plan, stop. Do not create or push the tag,
create or publish the GitHub Release, publish packages or images, or trigger a
deployment.

If approved, perform only the approved actions. Create an annotated
`vX.Y.Z` tag on the verified `main` commit, push that exact tag, and create the
GitHub Release with title exactly `vX.Y.Z`. Re-check the remote tag and release
afterward. Any package, image, or deployment publication requires its
destination to have been included explicitly in the approval.

## 7. Restore `develop`

After the release branch is pushed, update `develop` separately with one empty
`## [Unreleased]` section above the released entry, following
`.github/copilot-instructions.md`. Commit and push that change to `develop`
without copying `[Unreleased]` back to the release branch or `main`.

If branch protection requires a pull request for this update, use a dedicated
feature branch targeting `develop`; do not bypass protection.

## Rollback and reconciliation

Prefer forward reconciliation over destructive history changes.

### Before publication

- Correct metadata on the release branch and update the pull request.
- If the version is invalid or collides, abandon the release branch and prepare
  a new valid version from `origin/develop`.
- Never delete or rewrite another contributor's release branch.

### Tag created but release absent or incorrect

- Stop all further publication.
- Compare the remote tag target with the approved `main` commit.
- If the target is correct, fix or create the GitHub Release only after renewed
  human approval of the reconciled plan.
- If the target is wrong, do not move or delete the public tag automatically.
  Present the discrepancy and obtain explicit approval for the corrective
  action and any replacement version.

### Release created but artifacts or deployment failed

- Do not claim success and do not retry against a different destination.
- Preserve logs and immutable identifiers without exposing secrets.
- Mark a draft release as draft when that is a safe available action; do not
  delete a published release or artifact automatically.
- Reconcile artifact digests, package versions, image tags, release assets, and
  deployment state against the approved plan.
- Require renewed human approval before retrying any publication action.

### Metadata drift after publication

- Treat the pushed tag and its target commit as immutable public history.
- Correct `VERSION`, `CHANGELOG.md`, or release notes through normal reviewed
  changes.
- Never force-push `main`, `develop`, or a release tag.
- If a published artifact cannot be reconciled safely, prepare a new patch
  release rather than overwriting the published version.

Conclude with the pull request URL, validation results, remaining approval
gate, and any reconciliation needed. Never report a release as published until
the remote tag, GitHub Release, and every approved artifact are verified.
