#!/usr/bin/env python3
"""GitHub → Feishu watcher.

Runs on the schedule configured in .github/workflows/monitor.yml. Polls:
  1. watched repos for NEWLY CREATED open issues (tiered, rotating slice),
  2. our open PRs for state changes (merged / new comments / new reviews),
and pushes events as cards to a Feishu group via the Feishu Open API.

State lives in state.json (committed back by the workflow). First run is a
silent baseline + an "online" card. Standard library only.

Notification is best-effort: a failing Feishu channel never fails the round.
State is always saved (poll progress advances), and events that could not be
pushed are appended to undelivered.jsonl (also committed by the workflow) so
nothing is silently dropped while the channel is blocked.
"""
import json
import os
import tempfile
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.github.com"
FEISHU = "https://open.feishu.cn/open-apis"
STATE_FILE = Path("state.json")
REPOS_FILE = Path("repos.txt")
UNDELIVERED_FILE = Path("undelivered.jsonl")
UNDELIVERED_LIMIT = 200
DIGEST_LABEL = "monitor-digest"
DIGEST_TITLE = "Monitoring digest: notification channel unavailable"
DIGEST_ISSUE_LINES = 30
T3_SLICES = 3
NOISE = ("ScreenContextAgent",)
OWNER = "inchang-ing"


@contextmanager
def state_lock():
    """Keep the whole read/poll/notify/write cycle exclusive across processes."""
    with STATE_FILE.with_suffix(STATE_FILE.suffix + ".lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if not lock.tell():
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def save_state(state):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=STATE_FILE.parent, prefix=".state-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, ensure_ascii=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, STATE_FILE)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parse_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def record_undelivered(events, now):
    """Append events the notification channel could not push, capped at the tail."""
    if not events:
        return
    old = UNDELIVERED_FILE.read_text(encoding="utf-8").splitlines() if UNDELIVERED_FILE.exists() else []
    lines = [json.dumps({"ts": now.isoformat(), **ev}, ensure_ascii=False, sort_keys=True)
             for ev in events]
    UNDELIVERED_FILE.write_text("\n".join((old + lines)[-UNDELIVERED_LIMIT:]) + "\n",
                                encoding="utf-8")


def gh_digest_issue(token):
    """Return (number, repo) of the open digest issue, or (None, repo/None).

    GitHub emails the account owner about its own repository's issues, so an
    open issue is the fallback channel that works while Feishu is blocked.
    Runs only inside GitHub Actions (GITHUB_REPOSITORY is set there).
    """
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return None, None
    issues = http("GET", API + f"/repos/{repo}/issues?state=open&labels={DIGEST_LABEL}",
                  token=token)
    for it in issues:
        if "pull_request" not in it:
            return it["number"], repo
    return None, repo


def gh_update_digest_issue(token, events):
    """Comment the round's undelivered events on the digest issue, opening it if needed."""
    number, repo = gh_digest_issue(token)
    if repo is None:
        return
    lines = [f"- [{ev.get('key', '?')}]({ev.get('url', '')}) {ev.get('title', '')}"[:200]
             for ev in events[:DIGEST_ISSUE_LINES]]
    if len(events) > DIGEST_ISSUE_LINES:
        lines.append(f"- … and {len(events) - DIGEST_ISSUE_LINES} more in undelivered.jsonl")
    body = ("Events the Feishu channel could not deliver this round.\n\n"
            "Note: each entry is timestamped when it was captured — an event may "
            "already have been handled since. Always re-check the PR's current "
            "reviews/comments before acting on an entry.\n\n"
            + "\n".join(lines))
    if number is None:
        r = http("POST", API + f"/repos/{repo}/issues",
                 {"title": DIGEST_TITLE, "body": body, "labels": [DIGEST_LABEL]}, token=token)
        print(f"digest issue #{r.get('number')} opened")
    else:
        http("POST", API + f"/repos/{repo}/issues/{number}/comments",
             {"body": body}, token=token)
        print(f"digest issue #{number} updated")


def gh_close_digest_issue(token):
    """Close the digest issue once the notification channel works again."""
    number, repo = gh_digest_issue(token)
    if repo is None or number is None:
        return
    http("PATCH", API + f"/repos/{repo}/issues/{number}", {"state": "closed"}, token=token)
    print(f"digest issue #{number} closed (channel recovered)")


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


def poll_issues(repos, gh_token, checked_at, fallback_since, seen, now):
    """Return new issues, watermarks, and whether any repository was fully scanned."""
    fresh = []
    successful = dict(checked_at)
    any_success = False
    for repo in repos:
        try:
            last_check = parse_time(checked_at.get(repo, fallback_since)).replace(microsecond=0)
            repo_fresh = []
            page = 1
            while True:
                items = gh_get(f"/repos/{repo}/issues?state=open&sort=created"
                               f"&direction=desc&per_page=100&page={page}", gh_token)
                if not isinstance(items, list):
                    raise ValueError("issues response is not a list")
                reached_checkpoint = False
                for it in items:
                    created = parse_time(it["created_at"])
                    if created < last_check:
                        reached_checkpoint = True
                        continue
                    if "pull_request" in it:
                        continue
                    title = it.get("title", "")
                    if any(n in title for n in NOISE):
                        continue
                    key = f"{repo}#{it['number']}"
                    if key in seen:
                        continue
                    labels = ",".join(l["name"] for l in it.get("labels", []))
                    repo_fresh.append({"repo": repo, "num": it["number"], "title": title,
                                       "labels": labels,
                                       "author": it.get("user", {}).get("login", "?"),
                                       "url": it.get("html_url", ""), "key": key})
                if reached_checkpoint or len(items) < 100:
                    break
                page += 1
        except Exception as e:
            print(f"WARN: {repo} issues poll failed: {e}")
            continue
        fresh.extend(repo_fresh)
        successful[repo] = now.replace(microsecond=0).isoformat()
        any_success = True
    return fresh, successful, any_success


def pr_snapshot(item, old=None):
    if item.get("state") not in ("open", "closed"):
        raise ValueError("PR response has no valid state")
    return {**(old or {}), "comments": item["comments"], "state": item["state"],
            "merged_at": item.get("merged_at") or (item.get("pull_request") or {}).get("merged_at"),
            "url": item["html_url"], "title": item["title"]}


def poll_prs(gh_token, cached_prs):
    """Return PR events, snapshots, and whether the open-PR search completed."""
    events = []
    current = {}
    try:
        page = 1
        while True:
            r = gh_get(f"/search/issues?q=is:pr+author:{OWNER}+is:open"
                       f"&per_page=100&page={page}", gh_token)
            if r.get("incomplete_results") or r.get("total_count", 0) > 1000:
                raise ValueError("PR search is incomplete or exceeds the 1000-result limit")
            items = r["items"]
            if not isinstance(items, list):
                raise ValueError("PR search items are not a list")
            for it in items:
                repo_full = "/".join(it["repository_url"].split("/")[-2:])
                key = f"{repo_full}#{it['number']}"
                current[key] = pr_snapshot(it, cached_prs.get(key))
            if len(items) < 100:
                if len(current) < r.get("total_count", 0):
                    raise ValueError("PR search returned fewer results than expected")
                break
            page += 1
            if page > 10:
                if len(current) < r.get("total_count", 0):
                    raise ValueError("PR search returned fewer results than expected")
                break
    except Exception as e:
        print(f"WARN: pr search failed: {e}")
        return events, cached_prs, False

    review_keys = set(current)
    for key, old in cached_prs.items():
        if key in current:
            continue
        current[key] = dict(old)
        if old.get("state") != "open":
            continue
        repo_full, num = key.rsplit("#", 1)
        try:
            detail = gh_get(f"/repos/{repo_full}/pulls/{num}", gh_token)
            current[key] = pr_snapshot(detail, old)
            review_keys.add(key)
        except Exception as e:
            print(f"WARN: {key} detail poll failed: {e}")

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
    for key in sorted(review_keys):
        cur = current[key]
        old = cached_prs.get(key)
        repo_full, num = key.rsplit("#", 1)
        try:
            n, page = 0, 1
            while True:
                revs = gh_get(f"/repos/{repo_full}/pulls/{num}/reviews"
                              f"?per_page=100&page={page}", gh_token)
                if not isinstance(revs, list):
                    raise ValueError("reviews response is not a list")
                n += len(revs)
                if len(revs) < 100:
                    break
                page += 1
        except Exception as e:
            print(f"WARN: {key} reviews poll failed: {e}")
            continue
        cur["reviews"] = n
        if old is not None and "reviews" in old and n > old["reviews"] and not any(
                e["key"] == key and e["kind"] == "merged" for e in events):
            events.append({"key": key, "kind": "review", "delta": n - old.get("reviews", 0),
                           "title": cur["title"], "url": cur["url"]})
    return events, current, True


def main():
    with state_lock():
        run()


def run():
    gh_token = os.environ["GITHUB_TOKEN"]
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    init = not bool(state)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    last_raw = state.get("last_check")
    fallback_since = state.get("issue_backfill_since")
    if fallback_since is None:
        fallback = now if init else now - timedelta(days=7)
        if last_raw:
            fallback = min(fallback, parse_time(last_raw))
        fallback_since = fallback.isoformat()
    seen = state.get("seen", {})
    cached_prs = state.get("prs", {})

    always, rotating = load_tiers()
    rot = int(state.get("rot_index", 0)) % T3_SLICES
    repos = always + rotating[rot::T3_SLICES]
    print(f"polling {len(repos)} repos (rot {rot}/{T3_SLICES}), init={init}")

    new_issues, checked_at, issues_ok = poll_issues(
        repos, gh_token, state.get("issue_checked_at", {}), fallback_since, seen, now)
    pr_events, current_prs, prs_ok = poll_prs(gh_token, cached_prs)
    if not issues_ok and not prs_ok:
        raise RuntimeError("All GitHub polls failed; state preserved")

    pending = list(new_issues) + list(pr_events)
    delivered = True
    if init or pending:
        cfg = {"app_id": os.environ["FEISHU_APP_ID"],
               "app_secret": os.environ["FEISHU_APP_SECRET"],
               "chat_id": os.environ["FEISHU_CHAT_ID"]}
        ftoken = None
        try:
            ftoken = feishu_token(cfg)
        except Exception as exc:
            delivered = False
            print(f"WARN: feishu auth failed; round events go to {UNDELIVERED_FILE}: {exc}")
        if init:
            for it in new_issues:
                seen[it["key"]] = now.isoformat()
            for it in new_issues[:5]:
                print(f"baseline issue: {it['key']} {it['title'][:60]}")
            if ftoken is not None:
                try:
                    send_card(ftoken, cfg["chat_id"], "GitHub 监测已上线",
                              f"本轮扫描 {len(repos)} 仓 · 监测 {len(current_prs)} 个在飞 PR\n"
                              "按工作流配置定时运行，发现事件后推送到本群:新 issue / PR 合并 / 新评审 / 新评论")
                    print("baseline done")
                except Exception as exc:
                    print(f"WARN: baseline card failed (ignored): {exc}")
        elif ftoken is not None:
            try:
                for start in range(0, len(new_issues), 12):
                    batch = new_issues[start:start + 12]
                    lines = [f"- [{i['repo']}#{i['num']}]({i['url']}) {i['title'][:80]}"
                             f"{' `' + i['labels'] + '`' if i['labels'] else ''}"
                             for i in batch]
                    send_card(ftoken, cfg["chat_id"],
                              f"捕获 {len(batch)} 条新 issue",
                              "\n".join(lines))
                for ev in pr_events:
                    if ev["kind"] == "review":
                        title = f"收到 {ev['delta']} 条新评审"
                    elif ev["kind"] == "comments":
                        title = f"新增 {ev['delta']} 条评论"
                    else:
                        title = {"merged": "🎉 PR 已合并", "closed": "PR 被关闭"}[ev["kind"]]
                    send_card(ftoken, cfg["chat_id"], title,
                              f"[{ev['key']}]({ev['url']}) {ev['title']}")
            except Exception as exc:
                delivered = False
                print(f"WARN: feishu send failed; round events go to {UNDELIVERED_FILE}: {exc}")
        if not init and not delivered:
            record_undelivered(pending, now)
    else:
        print("no events")

    if not init and pending:
        try:
            if not delivered:
                gh_update_digest_issue(gh_token, pending)
            else:
                gh_close_digest_issue(gh_token)
        except Exception as exc:
            print(f"WARN: digest issue update failed (ignored): {exc}")

    for it in new_issues:
        seen[it["key"]] = now.isoformat()
    cutoff = (now - timedelta(days=7)).isoformat()
    seen = {k: v for k, v in seen.items() if v > cutoff}
    state.update(last_check=now.isoformat(), seen=seen,
                 prs=current_prs, rot_index=(rot + 1) % T3_SLICES,
                 issue_checked_at=checked_at, issue_backfill_since=fallback_since)
    save_state(state)
    print(f"state saved: {len(seen)} seen, {len(current_prs)} prs")


if __name__ == "__main__":
    main()
