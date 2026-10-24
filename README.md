# repo-monitor

GitHub → Feishu watcher for the open-source contribution pipeline
(the parent workspace's `README.md` documents the pipeline; `RULES.md` holds its rules).

A scheduled GitHub Action polls ~390 watched repos for newly created issues
and this account's open PRs for state changes, then pushes events as cards
to a Feishu group via the Feishu Open API. No third-party dependencies.

The polling schedule is defined only in [the workflow](.github/workflows/monitor.yml).
Change it there when needed. Rotating repos are split into `T3_SLICES` groups
in `monitor.py`; one group is scanned per run, so a full cycle takes that many runs.

- `repos.txt` — watched repos (`owner/repo` = every run, `R owner/repo` = rotating slice)
- `monitor.py` — the poller (stdlib only)
- `state.json` — committed back each run (seen issues, PR cache, rotation index,
  successful scan time for each repository)

Issue pages are scanned back to each repository's own last successful scan.
Scan times use whole seconds; the boundary second is checked again and deduplicated
against seen issues so GitHub's second-resolution timestamps do not hide new issues.
An unsuccessful page discards that repository's partial results and leaves its
scan time unchanged. Existing state files are migrated automatically: repositories
without a scan time use a fixed lookback of at least seven days (or the old
`last_check`, if earlier). This can send older, previously unseen issues once.

PR search and review lists are paginated. Cached open PRs that disappear from
search are checked individually for merge/closure. Failed queries retain the
previous PR snapshot or review count; an unknown review count starts with a
silent baseline when the query first succeeds. Closed PR snapshots are retained.

If neither a repository issue scan nor the open-PR search completes successfully,
the run exits with an error before contacting Feishu or saving state. GitHub Actions
then reports a failed run; the previous cache, scan times, and rotation remain intact.
A successful query with no results still counts as success. Partial failures retain
their previous data while successful queries can still produce notifications.

After initialization, a successful round with no events saves scan progress without
requesting a Feishu token. Feishu credentials and authentication are needed only
when sending the initial online card or event notifications.

Issue cards contain at most 12 entries. Notification is best-effort: a Feishu
authentication or send failure never fails the round. Scan progress is saved
either way, so watermarks and the rotation keep advancing, and the round's
events are appended to `undelivered.jsonl` (kept at the 200 most recent lines,
committed together with `state.json`) instead of being dropped or retried by
crashing runs. As a second fallback that does not require anyone to watch a
file, rounds with undelivered events also comment them on (or open) a
`monitor-digest` labeled issue in this repository while it runs inside GitHub
Actions — GitHub emails the account owner about its own issues, so a blocked
Feishu channel still surfaces within a round. The digest issue is closed
automatically once a round delivers notifications again. A permanently blocked
channel therefore shows green runs, a readable digest, and an open tracking
issue instead of an endless red retry loop. A fresh installation starts with a
silent baseline and an online card; that start time also applies to rotation
slices first scanned later.

State writes use an atomic replacement. A process lock held for the entire run
prevents overlapping local runs that share `state.json`; the persistent
`state.json.lock` file contains no credentials and need not be committed.
Separate GitHub Actions runners must also be serialized by workflow concurrency.

Run the offline regression tests from this directory:

```sh
python -S -B -m unittest discover -s tests
```

The tests replace GitHub and Feishu calls with fake responses and never send
notifications.

Secrets (repo Settings → Secrets and variables → Actions):
`FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_CHAT_ID`.
