---
name: orchestrator-release
description: Prepares and validates Agent Landing Zone Orchestrator releases. Use for VERSION, changelog entries, release branches, tags, GitHub Release notes, and synchronized develop follow-up.
---

# Agent Landing Zone Orchestrator release

Read `.github/copilot-instructions.md` and
`.github/instructions/release.instructions.md` completely before changing a
release artifact.

1. Determine the intended semantic version and create `release/X.Y.Z` from
   `develop`.
2. Set root `VERSION` to `X.Y.Z` without a `v` prefix.
3. Replace the single `[Unreleased]` heading with
   `## [vX.Y.Z] - YYYY-MM-DD`; do not leave `[Unreleased]` on the release
   branch.
4. Verify the branch, `VERSION`, changelog, intended tag, and GitHub Release
   title are synchronized, accounting for their required prefix differences.
5. Keep only release preparation on the branch and target the pull request to
   `main`.
6. Validate the exact commit with the relevant pytest, frontend, deployment,
   contract, and Copilot asset checks.
7. Keep changelog, release notes, and published operator documentation
   consistent and free of secrets or private Azure environment names.
8. Use exactly `vX.Y.Z` for both the Git tag and GitHub Release title.
9. After pushing the release branch, restore one empty `[Unreleased]` section
   at the top of `develop`, commit it, and push `develop`.
10. Re-fetch published release notes and verify headings, lists, tables, and
    sanitized content.

Do not publish a tag, release, package, image, or production deployment
without explicit human approval. Report failed validation or version drift as
a blocker rather than filling gaps by assumption.
