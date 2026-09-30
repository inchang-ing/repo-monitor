# repo-monitor

GitHub → Feishu watcher for the open-source contribution pipeline
(`E:\zcode-PR\agent.md` documents the pipeline; `STATE.md` is the rule book).

A scheduled GitHub Action polls ~390 watched repos for newly created issues
and this account's open PRs for state changes, then pushes events as cards
to a Feishu group via the Feishu Open API. No third-party dependencies.

- `repos.txt` — watched repos (`owner/repo` = every run, `R owner/repo` = rotating 1/6 slice)
- `monitor.py` — the poller (stdlib only)
- `state.json` — committed back each run (seen-issues set, PR cache, rotation index)

Secrets (repo Settings → Secrets and variables → Actions):
`FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_CHAT_ID`.
