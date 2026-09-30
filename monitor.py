#!/usr/bin/env python3
"""GitHub → Feishu watcher.

Runs on GitHub Actions every 10 minutes. Polls:
  1. watched repos for NEWLY CREATED open issues (tiered, rotating slice),
  2. our open PRs for state changes (merged / new comments / new reviews),
and pushes events as cards to a Feishu group via the Feishu Open API.

State lives in state.json (committed back by the workflow). First run is a
silent baseline + an "online" card. Standard library only.
"""
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.github.com"
FEISHU = "https://open.feishu.cn/open-apis"
STATE_FILE = Path("state.json")
REPOS_FILE = Path("repos.txt")
T3_SLICES = 6
NOISE = ("ScreenContextAgent",)
OWNER = "inchang-ing"


def http(method, url, payload=None, token=None, headers=None):
    h = {"Accept": "application/vnd.github+json", "User-Agent": "repo-monitor"}
    if payload is not None:
        h["Content-Type"] = "application/json"
    if token:
        h["Authorization"] = f"Bearer {token}"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers=h)
    return json.load(urllib.request.urlopen(req, timeout=30))


def gh_get(path, token):
    return http("GET", API + path, token=token)


def feishu_token(cfg):
    r = http("POST", FEISHU + "/auth/v3/tenant_access_token/internal",
             {"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]})
    if r.get("code") != 0:
        raise RuntimeError(f"feishu token failed: {r}")
    return r["tenant_access_token"]


def send_card(token, chat_id, title, body):
    card = {"config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title},
                       "template": "blue"},
            "elements": [{"tag": "markdown", "content": body or " "}]}
    r = http("POST", FEISHU + "/im/v1/messages?receive_id_type=chat_id",
             {"receive_id": chat_id, "msg_type": "interactive",
              "content": json.dumps(card)}, token)
    if r.get("code") != 0:
        raise RuntimeError(f"feishu send failed: {r.get('code')} {r.get('msg')}")
    return r["data"]["message_id"]


def load_tiers():
    always, rotating = [], []
    for line in REPOS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("R "):
            rotating.append(line[2:])
        else:
            always.append(line)
    return always, rotating


def poll_issues(repos, gh_token, last_check, seen):
    """Return newly created open issues since last_check, not yet notified."""
    fresh = []
    for repo in repos:
        try:
            items = gh_get(f"/repos/{repo}/issues?state=open&sort=created"
                           f"&direction=desc&per_page=15", gh_token)
        except Exception as e:
            print(f"WARN: {repo} issues poll failed: {e}")
            continue
        for it in items:
            if "pull_request" in it or "pull_request" in str(it.get("url", "")):
                continue
            created = datetime.fromisoformat(it["created_at"].replace("Z", "+00:00"))
            if created <= last_check:
                continue
            title = it.get("title", "")
            if any(n in title for n in NOISE):
                continue
            key = f"{repo}#{it['number']}"
            if key in seen:
                continue
            labels = ",".join(l["name"] for l in it.get("labels", []))
            fresh.append({"repo": repo, "num": it["number"], "title": title,
                          "labels": labels, "author": it.get("user", {}).get("login", "?"),
                          "url": it.get("html_url", ""), "key": key})
    return fresh


def poll_prs(gh_token, cached_prs):
    """Return PR events (merged / closed / new comments / new reviews)."""
    events = []
    current = {}
    try:
        r = gh_get(f"/search/issues?q=is:pr+author:{OWNER}+is:open&per_page=50", gh_token)
        for it in r.get("items", []):
            repo_full = "/".join(it["repository_url"].split("/")[-2:])
            current[f"{repo_full}#{it['number']}"] = {
                "comments": it.get("comments", 0),
                "state": it.get("state"),
                "merged_at": (it.get("pull_request") or {}).get("merged_at"),
                "url": it.get("html_url", ""),
                "title": it.get("title", ""),
            }
    except Exception as e:
        print(f"WARN: pr search failed: {e}")
        return events, cached_prs

    for key, cur in current.items():
        old = cached_prs.get(key)
        if old is None:
            continue  # newly opened PR — the local automation announces it
        if not old.get("merged_at") and cur.get("merged_at"):
            events.append({"key": key, "kind": "merged", "title": cur["title"],
                           "url": cur["url"]})
        elif old.get("state") == "open" and cur.get("state") == "closed" and not cur.get("merged_at"):
            events.append({"key": key, "kind": "closed", "title": cur["title"],
                           "url": cur["url"]})
        elif cur.get("comments", 0) > old.get("comments", 0):
            events.append({"key": key, "kind": "comments",
                           "delta": cur["comments"] - old.get("comments", 0),
                           "title": cur["title"], "url": cur["url"]})
    # reviews: one cheap call per PR (new reviews arrive as review objects)
    for key, cur in current.items():
        old = cached_prs.get(key)
        repo_full, num = key.rsplit("#", 1)
        try:
            revs = gh_get(f"/repos/{repo_full}/pulls/{num}/reviews?per_page=100", gh_token)
        except Exception:
            continue
        n = len(revs) if isinstance(revs, list) else 0
        cur["reviews"] = n
        if old is not None and n > old.get("reviews", 0) and not any(
                e["key"] == key and e["kind"] == "merged" for e in events):
            events.append({"key": key, "kind": "review", "delta": n - old.get("reviews", 0),
                           "title": cur["title"], "url": cur["url"]})
    return events, current


def main():
    gh_token = os.environ["GITHUB_TOKEN"]
    cfg = {"app_id": os.environ["FEISHU_APP_ID"],
           "app_secret": os.environ["FEISHU_APP_SECRET"],
           "chat_id": os.environ["FEISHU_CHAT_ID"]}
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    init = not bool(state)
    now = datetime.now(timezone.utc)
    last_raw = state.get("last_check")
    last_check = (datetime.fromisoformat(last_raw) if last_raw
                  else now - timedelta(minutes=15))
    seen = state.get("seen", {})
    cached_prs = state.get("prs", {})

    always, rotating = load_tiers()
    rot = int(state.get("rot_index", 0)) % T3_SLICES
    repos = always + rotating[rot::T3_SLICES]
    print(f"polling {len(repos)} repos (rot {rot}/{T3_SLICES}), init={init}")

    new_issues = poll_issues(repos, gh_token, last_check, seen)
    pr_events, current_prs = poll_prs(gh_token, cached_prs)

    ftoken = feishu_token(cfg)

    if init:
        for it in new_issues:
            seen[it["key"]] = now.isoformat()
        for it in new_issues[:5]:
            print(f"baseline issue: {it['key']} {it['title'][:60]}")
        send_card(ftoken, cfg["chat_id"], "GitHub 监测已上线",
                  f"轮询 {len(repos)} 仓(每 10 分钟)· 监测 {len(current_prs)} 个在飞 PR\n"
                  "事件将实时推送到本群:新 issue / PR 合并 / 新评审 / 新评论")
        print("baseline done")
    else:
        if new_issues:
            lines = [f"- [{i['repo']}#{i['num']}]({i['url']}) {i['title'][:80]}"
                     f"{' `' + i['labels'] + '`' if i['labels'] else ''}"
                     for i in new_issues[:12]]
            more = f"\n- …及其他 {len(new_issues) - 12} 条" if len(new_issues) > 12 else ""
            send_card(ftoken, cfg["chat_id"],
                      f"捕获 {len(new_issues)} 条新 issue",
                      "\n".join(lines) + more)
        for ev in pr_events:
            kind = {"merged": ("🎉 PR 已合并", f"[{ev['key']}]({ev['url']}) {ev['title']}"),
                    "closed": ("PR 被关闭", f"[{ev['key']}]({ev['url']}) {ev['title']}"),
                    "review": (f"收到 {ev['delta']} 条新评审",
                               f"[{ev['key']}]({ev['url']}) {ev['title']}"),
                    "comments": (f"新增 {ev['delta']} 条评论",
                                 f"[{ev['key']}]({ev['url']}) {ev['title']}")}[ev["kind"]]
            send_card(ftoken, cfg["chat_id"], kind[0], kind[1])
        if not new_issues and not pr_events:
            print("no events")

    for it in new_issues:
        seen[it["key"]] = now.isoformat()
    cutoff = (now - timedelta(days=7)).isoformat()
    seen = {k: v for k, v in seen.items() if v > cutoff}
    state.update(last_check=now.isoformat(), seen=seen,
                 prs=current_prs, rot_index=(rot + 1) % T3_SLICES)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    print(f"state saved: {len(seen)} seen, {len(current_prs)} prs")


if __name__ == "__main__":
    main()
