#!/usr/bin/env python3
"""Merge queue for repos without GitHub's native one (Team plan, private repos).

One workflow run handles one batch, in stages run by merge-queue.yml:

  plan          pick queued PRs, build the batch branch (main + PRs), output it
  (ci)          the repo's CI workflow tests the batch commit once
  finish        CI green -> merge every PR in order; red -> split or reject
  (deploy)      the repo's deploy workflow ships the new main once
  after-deploy  comment the result; a failed deploy pauses the queue
  (finish and after-deploy end by starting another run if PRs still wait)

State lives in GitHub itself, so any run can pick up where the last one left
off and a dropped or cancelled run loses nothing:

  merge-queue   label: the PR wants in
  mq-priority   label: jumps the line and is the only thing let through while
                the queue is paused (a revert or fix for a broken deploy)
  mq-split      label: was in a batch that failed; now tested one at a time
  mq-paused     label on an open issue: a deploy failed, normal PRs wait
  merge-queue   commit status on a PR head: only the queue sets it, and main's
                branch protection requires it, so nobody can merge around us
"""

import json
import os
import subprocess
import sys

REPO = os.environ["GITHUB_REPOSITORY"]
OWNER, NAME = REPO.split("/")
SERVER = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
RUN_URL = f"{SERVER}/{REPO}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
BASE_BRANCH = os.environ.get("MQ_BASE_BRANCH", "main")
BATCH_BRANCH = os.environ.get("MQ_BATCH_BRANCH", "mq/batch")
MAX_BATCH = int(os.environ.get("MQ_MAX_BATCH", "5"))
REQUIRE_APPROVAL = os.environ.get("MQ_REQUIRE_APPROVAL", "true").lower() == "true"
MERGE_METHOD = os.environ.get("MQ_MERGE_METHOD", "squash")

QUEUE, PRIORITY, SPLIT, PAUSED = "merge-queue", "mq-priority", "mq-split", "mq-paused"
STATUS_CONTEXT = "merge-queue"
LABELS = {
    QUEUE: ("0e8a16", "Ready: the merge queue tests, merges and deploys it"),
    PRIORITY: ("b60205", "Merge queue: goes first, and through a paused queue"),
    SPLIT: ("fbca04", "Merge queue: batch failed, testing this PR on its own"),
    PAUSED: ("d93f0b", "Merge queue is paused after a failed deploy"),
}


# --------------------------------------------------------------------------- helpers

def sh(*args, check=True, capture=True):
    r = subprocess.run(args, text=True, capture_output=capture)
    if check and r.returncode != 0:
        sys.exit(f"command failed: {' '.join(args)}\n{r.stdout}\n{r.stderr}")
    return r


def gh_api(path, method="GET", fields=None, check=True):
    args = ["gh", "api", "-X", method, path]
    if fields is not None:
        args += ["--input", "-"]
    r = subprocess.run(args, text=True, capture_output=True,
                       input=json.dumps(fields) if fields is not None else None)
    if check and r.returncode != 0:
        sys.exit(f"gh api {method} {path} failed: {r.stdout} {r.stderr}")
    return (json.loads(r.stdout) if r.stdout.strip() else None), r.returncode


def graphql(query, **variables):
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        args += ["-F", f"{k}={v}"]
    return json.loads(sh(*args).stdout)["data"]


def output(**kv):
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        for k, v in kv.items():
            f.write(f"{k}={v if isinstance(v, str) else json.dumps(v)}\n")


def comment(pr, body):
    gh_api(f"repos/{REPO}/issues/{pr}/comments", "POST", {"body": body})


def remove_labels(pr, *names):
    for n in names:
        gh_api(f"repos/{REPO}/issues/{pr}/labels/{n}", "DELETE", check=False)


def add_labels(pr, *names):
    gh_api(f"repos/{REPO}/issues/{pr}/labels", "POST", {"labels": list(names)})


def set_status(sha, state, description):
    gh_api(f"repos/{REPO}/statuses/{sha}", "POST", {
        "state": state, "context": STATUS_CONTEXT,
        "description": description[:140], "target_url": RUN_URL})


def ensure_labels():
    for name, (color, desc) in LABELS.items():
        gh_api(f"repos/{REPO}/labels", "POST",
               {"name": name, "color": color, "description": desc}, check=False)


def pause_issue():
    issues, _ = gh_api(f"repos/{REPO}/issues?state=open&labels={PAUSED}&per_page=1")
    return issues[0]["number"] if issues else None


PR_QUERY = """
query($owner: String!, $name: String!, $base: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, baseRefName: $base, labels: ["merge-queue"], first: 50) {
      nodes {
        number title isDraft headRefOid
        labels(first: 30) { nodes { name } }
        latestReviews(first: 50) { nodes { state commit { oid } } }
        timelineItems(itemTypes: [LABELED_EVENT], last: 50) {
          nodes { ... on LabeledEvent { createdAt label { name } } }
        }
      }
    }
  }
}"""


def queued_prs():
    """Open PRs carrying the queue label, in the order they should be served."""
    nodes = graphql(PR_QUERY, owner=OWNER, name=NAME, base=BASE_BRANCH)[
        "repository"]["pullRequests"]["nodes"]
    prs = []
    for n in nodes:
        labels = {l["name"] for l in n["labels"]["nodes"]}
        queued_at = max((e["createdAt"] for e in n["timelineItems"]["nodes"]
                         if e and e["label"]["name"] == QUEUE), default="")
        prs.append({
            "number": n["number"], "title": n["title"], "draft": n["isDraft"],
            "head": n["headRefOid"], "labels": labels, "queued_at": queued_at,
            "reviews": n["latestReviews"]["nodes"],
        })
    # Priority first, then PRs already being retried alone, then label time.
    prs.sort(key=lambda p: (PRIORITY not in p["labels"], SPLIT not in p["labels"],
                            p["queued_at"]))
    return prs


def rejection_reason(pr):
    if pr["draft"]:
        return "it is a draft"
    if not REQUIRE_APPROVAL:
        return None
    states = [r["state"] for r in pr["reviews"]]
    if "CHANGES_REQUESTED" in states:
        return "a reviewer requested changes"
    # The approval must be on the exact commit being merged: a push after
    # approval means the new code was never reviewed.
    if not any(r["state"] == "APPROVED" and r["commit"] and r["commit"]["oid"] == pr["head"]
               for r in pr["reviews"]):
        return "it has no approval on its latest commit"
    return None


def reject(pr, why):
    remove_labels(pr["number"], QUEUE, SPLIT, PRIORITY)
    set_status(pr["head"], "failure", f"Not merged: {why}")
    comment(pr["number"], f"Removed from the merge queue: {why}.\n\n"
            f"Fix it, then add the `{QUEUE}` label again. ([queue run]({RUN_URL}))")


def git_setup():
    sh("git", "config", "user.name", "merge-queue[bot]")
    sh("git", "config", "user.email", "merge-queue@users.noreply.github.com")


# --------------------------------------------------------------------------- stages

def plan():
    ensure_labels()
    paused = pause_issue()
    prs = queued_prs()
    if paused:
        held = [p["number"] for p in prs if PRIORITY not in p["labels"]]
        if held:
            print(f"Queue paused (issue #{paused}); holding {held}")
        prs = [p for p in prs if PRIORITY in p["labels"]]

    eligible = []
    for p in prs:
        why = rejection_reason(p)
        if why:
            print(f"#{p['number']} rejected: {why}")
            reject(p, why)
        else:
            eligible.append(p)

    if not eligible:
        print("Nothing to do.")
        output(has_batch="false")
        return

    # A PR from a failed batch is tested on its own, so the culprit is found.
    candidates = eligible[:1] if SPLIT in eligible[0]["labels"] else [
        p for p in eligible if SPLIT not in p["labels"]][:MAX_BATCH]

    git_setup()
    sh("git", "fetch", "--quiet", "origin", BASE_BRANCH)
    base_sha = sh("git", "rev-parse", f"origin/{BASE_BRANCH}").stdout.strip()
    sh("git", "checkout", "--quiet", "-B", BATCH_BRANCH, base_sha)

    batch = []
    for p in candidates:
        n = p["number"]
        sh("git", "fetch", "--quiet", "origin", f"{p['head']}")
        r = sh("git", "merge", "--no-ff", "--no-edit", "-m", f"Merge #{n}: {p['title']}",
               p["head"], check=False)
        if r.returncode == 0:
            batch.append({"number": n, "head": p["head"]})
            continue
        sh("git", "merge", "--abort", check=False)
        if not batch:
            reject(p, f"it conflicts with `{BASE_BRANCH}`; merge or rebase it")
        else:
            # Conflicts only with a PR ahead of it: that PR merges first, then
            # this one gets its own turn against the new main.
            print(f"#{n} conflicts with an earlier PR in the batch; deferred")

    if not batch:
        output(has_batch="false")
        return

    sh("git", "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{BATCH_BRANCH}")
    batch_sha = sh("git", "rev-parse", "HEAD").stdout.strip()
    nums = ", ".join(f"#{b['number']}" for b in batch)
    for b in batch:
        set_status(b["head"], "pending", f"Testing in batch with {nums}")
    print(f"Batch {nums} on {base_sha[:7]} -> {batch_sha[:7]}")
    had_priority = any(PRIORITY in p["labels"] for p in candidates
                       if p["number"] in {b["number"] for b in batch})
    output(has_batch="true", base_sha=base_sha, batch_sha=batch_sha, batch=batch,
           had_priority="true" if had_priority else "false")


def finish():
    batch = json.loads(os.environ["BATCH"])
    base_sha, batch_sha = os.environ["BASE_SHA"], os.environ["BATCH_SHA"]
    ci = os.environ["CI_RESULT"]
    nums = ", ".join(f"#{b['number']}" for b in batch)

    if ci == "cancelled":
        print("CI was cancelled; the next run retries the batch.")
        output(merged="false")
        next_run()
        return

    if ci != "success":
        if len(batch) > 1:
            for b in batch:
                add_labels(b["number"], SPLIT)
                set_status(b["head"], "pending", "Batch failed; retrying on its own")
                comment(b["number"], f"Batch {nums} failed CI ([run]({RUN_URL})). "
                        "Retrying each PR on its own to find the cause.")
        else:
            b = batch[0]
            remove_labels(b["number"], QUEUE, SPLIT, PRIORITY)
            set_status(b["head"], "failure", "CI failed in the merge queue")
            comment(b["number"], f"CI failed, so this was not merged ([run]({RUN_URL})).\n\n"
                    f"Fix it, push, and add the `{QUEUE}` label again.")
        output(merged="false")
        next_run()
        return

    # CI is green, but merge only if nothing moved since the batch was built;
    # otherwise main would end up as something CI never saw.
    sh("git", "fetch", "--quiet", "origin", BASE_BRANCH)
    main_now = sh("git", "rev-parse", f"origin/{BASE_BRANCH}").stdout.strip()
    current = {p["number"]: p for p in queued_prs()}
    moved = []
    if main_now != base_sha:
        moved.append(f"`{BASE_BRANCH}` moved ({base_sha[:7]} -> {main_now[:7]})")
    for b in batch:
        p = current.get(b["number"])
        if p is None:
            moved.append(f"#{b['number']} left the queue")
        elif p["head"] != b["head"]:
            moved.append(f"#{b['number']} got new commits")
    if moved:
        print("Not merging, re-testing next run: " + "; ".join(moved))
        output(merged="false")
        next_run()
        return

    for b in batch:
        set_status(b["head"], "success", f"Passed in batch with {nums}")
    merged = []
    for b in batch:
        _, rc = gh_api(f"repos/{REPO}/pulls/{b['number']}/merge", "PUT",
                       {"merge_method": MERGE_METHOD, "sha": b["head"]}, check=False)
        if rc != 0:
            open_pause_issue(f"Merging #{b['number']} failed after {', '.join(merged) or 'nothing'} "
                             f"had merged, so `{BASE_BRANCH}` may hold only part of a tested "
                             "batch. Check it, then close this issue.")
            output(merged="false")
            next_run()
            return
        merged.append(f"#{b['number']}")

    # Sequential merges onto an unchanged main must reproduce the tested tree.
    sh("git", "fetch", "--quiet", "origin", BASE_BRANCH)
    new_main = sh("git", "rev-parse", f"origin/{BASE_BRANCH}").stdout.strip()
    tree_main = sh("git", "rev-parse", f"{new_main}^{{tree}}").stdout.strip()
    tree_tested = sh("git", "rev-parse", f"{batch_sha}^{{tree}}").stdout.strip()
    if tree_main != tree_tested:
        open_pause_issue(f"After merging {nums}, `{BASE_BRANCH}` ({new_main[:7]}) differs from "
                         f"the tested batch ({batch_sha[:7]}). Not deploying. Investigate, "
                         "then close this issue.")
        output(merged="false")
        next_run()
        return

    for b in batch:
        remove_labels(b["number"], QUEUE, SPLIT, PRIORITY)
        comment(b["number"], f"Merged in batch {nums} ([run]({RUN_URL})). Deploying now.")
    output(merged="true", main_sha=new_main)


def open_pause_issue(body):
    num = pause_issue()
    if num:
        comment(num, body)
        return num
    issue, _ = gh_api(f"repos/{REPO}/issues", "POST", {
        "title": "Merge queue paused", "labels": [PAUSED],
        "body": body + f"\n\nOnly PRs labelled `{PRIORITY}` (with `{QUEUE}`) go through until "
                       f"this issue is closed. A successful `{PRIORITY}` deploy closes it."})
    return issue["number"]


def after_deploy():
    batch = json.loads(os.environ["BATCH"])
    result = os.environ["DEPLOY_RESULT"]
    main_sha = os.environ["MAIN_SHA"]
    nums = ", ".join(f"#{b['number']}" for b in batch)
    if result == "success":
        for b in batch:
            comment(b["number"], f"Deployed `{main_sha[:7]}` ([run]({RUN_URL})).")
        num = pause_issue()
        if num and os.environ.get("HAD_PRIORITY") == "true":
            comment(num, f"Priority batch {nums} deployed cleanly; resuming the queue.")
            gh_api(f"repos/{REPO}/issues/{num}", "PATCH", {"state": "closed"})
        next_run()
        return
    num = open_pause_issue(f"Deploying `{main_sha[:7]}` (batch {nums}) failed: [run]({RUN_URL}). "
                           "Services were rolled back to their previous version, but the code "
                           f"is on `{BASE_BRANCH}`. Open a revert or fix PR with labels "
                           f"`{QUEUE}` + `{PRIORITY}`.")
    for b in batch:
        comment(b["number"], f"Merged, but the deploy failed ([run]({RUN_URL})). "
                f"The queue is paused, see #{num}.")
    next_run()


def next_run():
    if os.environ.get("HAD_BATCH") != "true":
        return
    prs = queued_prs()
    if pause_issue():
        prs = [p for p in prs if PRIORITY in p["labels"]]
    if prs:
        print(f"{len(prs)} PR(s) still queued; starting the next run.")
        sh("gh", "workflow", "run", os.environ["MQ_WORKFLOW"], "--ref", BASE_BRANCH,
           "--repo", REPO)


if __name__ == "__main__":
    {"plan": plan, "finish": finish, "after-deploy": after_deploy}[sys.argv[1]]()
