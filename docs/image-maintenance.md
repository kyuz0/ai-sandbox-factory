# Maintaining Factory images

Factory checks available versions and scans its published images daily. Each Monday it
prepares a weekly update: stable agent versions, their download checksums,
package locks and base-image digests. A relevant finding in a current image can
start the same update workflow earlier. Daily scans inspect the published set;
the blocking security gate applies to the replacement candidates. An already
verified pending update is not rebuilt merely because the old release still has
the findings it fixes.

Daily scans cover the current image index, the two latest releases with prebuilt
images, and verified pending updates. Shared digests are scanned once. Older
releases keep their original evidence.

The workflow publishes **candidates**, tests their exact digests, then prepares one **Update tested Factory container images**
PR when automatic PR creation is enabled. Otherwise it prints the verified
branch’s comparison link for manual PR creation. Merging that PR runs
the normal release checks. A build or a successful version command alone does
not qualify an image for promotion.

## What updates

- Pi, Codex, Claude Code and Antigravity. Controller and advisor images share
  the same native-agent pins.
- code-server, Betterwright and the other pinned npm tools, Python document
  packages, mitmproxy, AWS CLI and fd.
- Base-image digests within the selected Node, Python and Debian families,
  OS package updates, and bundled npm and pip versions. npm stays in its
  supported major version so a maintenance update does not change how native
  dependency installation works. The maintained Dockerfiles also invalidate
  cached OS-update layers for local builds when an update is promoted.

Factory's own patched FrostPi commit remains a reviewed source pin. Maintenance
does not replace the fork or automatically import upstream commits. User
profiles and provider packages are outside this workflow.

The updater reports previous and available versions. A failed component keeps
its existing pins; successful components can form a tested partial update. The
version report names failed components, and the cycle reports
incomplete. Failed client or lifecycle checks prevent promotion.

## Run or review an update

Open **Actions → Factory image maintenance → Run workflow** and select `main`. Leave the weekly
update option enabled to prepare candidates now. Disable it for a version and
scan check; an unresolved protected-image finding can still trigger replacement
candidates. The scheduled weekly run uses the same path.

Automatic PR creation requires the repository's GitHub Actions PR-creation
permission and the Actions variable `FACTORY_AUTOMATED_PRS=true`. GitHub bundles
creation with its workflow-approval permission. Factory's workflow never approves
or merges PRs. Leave the variable unset for manual PR creation.

The automatic PR links to its verification run. For a manual PR, use
`.github/image-maintenance.json` on the verified branch and the run's
`weekly-image-versions` artifact. Check the version table, blocked components
and image-scan reports before merging. A candidate's code, pins and image list
must match what was tested. If they change during review, rebuild and retest.

Checks cover actual installed versions, anonymous digest pulls, first-launch Pi
and FrostPi, provider creation and failure/retry, restricted networking,
credential isolation, custom profiles and editor continuity. Native-agent checks
run the vendor executables against synthetic authentication and service fixtures.
They do not claim to test a live subscription or vendor model. Run the affected
live-client check in [TESTING.md](../TESTING.md#native-agent-regression-checks)
before approving an agent update that needs that verification.

## Vulnerability reports and ignores

Trivy records package inventories and rescans them with its current vulnerability
database. The reports retain every finding. Agent/editor image findings are informational
(`pi-common`, `pi-stock`, `native-common`). Credential-companion and gateway
candidates are blocked by fixable High/Critical findings and CISA-listed
known-exploited vulnerabilities unless a scoped exception has been reviewed.
A finding identifies installed software; it does
not establish that a Factory protection has failed.

Authenticated editor startup, file grants, restricted networking and companion
credential isolation remain blocking tests for every release. A demonstrated
failure of these protections blocks promotion regardless of the image role.

Document a reviewed exception in `.github/image-security-exceptions.json` with
its vulnerability ID, package, installed version, image role, exact scan target
and reason. Record when it was reviewed with `reviewed_on`. Each entry applies only
to that combination; it does not hide the finding from the report. Add
`review_before` only when the exception should expire. A newer package version or
a different finding requires its own review. Fixed findings do not remain blocked
by an obsolete entry.

Use upstream releases and this reviewed ignore list for upstream-bundled findings.
Factory does not maintain custom vendor security patches or dependency overrides.
Workspace reports need no per-CVE ignore entries.
Scanner or advisory-feed failures remain failures, never a clean result.

Downloaded native executables may not expose enough package metadata for full
scanner coverage. Their explicit version checks and vendor updates remain
necessary. A clean scanner report is not a guarantee that every binary was
recognized.

## Failure emails

**Factory failure notifications** reports failed and timed-out runs of this
repository's checks/release, image-candidate and maintenance workflows. It posts
one comment per failed run attempt on the issue selected by the repository
Actions variable `FACTORY_FAILURE_ISSUE`, mentioning the repository owner.

Enable email for **Participating, @mentions and custom** in GitHub notification
settings. Keep the configured issue open. No SMTP service is required. Actions'
**Failed workflows only** emails are a useful additional notification, but their
recipient depends on who triggered the run.

To resend a missed notification, run **Factory failure notifications** with the
failed run ID and attempt. The notifier ignores an attempt it already reported.
It reads run metadata only; it never executes a failed pull request's code or
reads its artifacts. Notifier failures remain visible in Actions.

A pending PR still needs its final review. The daily check distinguishes waiting
for review from a failed or overdue weekly cycle. GitHub schedules are best
effort; an outage that prevents all workflows from running also prevents these
notifications from running.

## Older images

Keep every digest referenced by any Factory release. Release image tags are
never moved, and each GitHub Release retains its image index, component inventories
and scan evidence in `factory-image-evidence.tar.gz`, beyond the lifetime of Actions
artifacts. Publishing a candidate
never moves a released digest or updates a running sandbox. The release's image
index records that release's set; select an earlier release for an image rollback.

`python scripts/image_maintenance.py retention` reports the complete released
keep-set. It does not delete images.
