# Maintaining Factory images

Factory checks for updates and scans its published images daily. Each Monday it
prepares a weekly update: stable agent versions, their download checksums,
package locks and base-image digests. A relevant finding in a current image can
start the same update workflow earlier.

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
PR names anything left unchanged because of failure, and the cycle reports
incomplete. Failed client or lifecycle checks prevent promotion.

## Run or review an update

Open **Actions → Factory image maintenance → Run workflow**. Leave the weekly
update option enabled to prepare candidates now. Disable it for a version and
scan check. The scheduled weekly run uses the same path.

Automatic PR creation requires the repository's GitHub Actions PR-creation
permission and the Actions variable `FACTORY_AUTOMATED_PRS=true`. GitHub bundles
creation with its workflow-approval permission. Factory's workflow never approves
or merges PRs. Leave the variable unset for manual PR creation.

The PR links to its verification run. Check the version table, blocked components
and image-scan reports before merging. A candidate's code, pins and image list
must match what was tested. If they change during review, rebuild and retest.

Checks cover actual installed versions, anonymous digest pulls, first-launch Pi
and FrostPi, provider creation and failure/retry, restricted networking,
credential isolation, custom profiles and editor continuity. Native-agent checks
run the vendor executables against synthetic authentication and service fixtures.
They do not claim to test a live subscription or vendor model. Run the affected
live-client check in [TESTING.md](../TESTING.md#native-agent-regression-checks)
before approving an agent update that needs that verification.

## Vulnerability reports

Trivy records package inventories and rescans them with its current vulnerability
database. The reports retain every finding. Current candidates require review of
fixable High/Critical findings and CISA-listed known-exploited vulnerabilities.
A scanner finding identifies software to review; it does not prove that the
vulnerable operation is reachable in a Factory sandbox.

Document a reviewed exception in `.github/image-security-exceptions.json` with
its vulnerability ID, package, installed version, image role, exact scan target,
reason and review date. An exception applies only to that combination. An expired
exception no longer approves a finding that is still present. Fixed findings
do not remain blocked by an obsolete exception.
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
never moved, and the GitHub Release retains the image index, component inventories
and scan evidence beyond the lifetime of Actions artifacts. Publishing a candidate
never moves a released digest or updates a running sandbox. The release's image
index records the earlier set for an image rollback.

`python scripts/image_maintenance.py retention` reports the complete released
keep-set. It does not delete images. Retained older images are reported in scans without
blocking a current-image refresh.
