#!/usr/bin/env python3
"""Faux `hermes cron` pour les tests : mêmes arguments et mêmes sorties utiles que la
CLI amont, état dans $HERMES_HOME/cron/jobs.json (format {"jobs": [...]})."""
import argparse
import json
import os
import sys
import uuid
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
JOBS = HOME / "cron" / "jobs.json"


def load():
    try:
        return json.loads(JOBS.read_text())["jobs"]
    except FileNotFoundError:
        return []


def save(jobs):
    JOBS.parent.mkdir(parents=True, exist_ok=True)
    JOBS.write_text(json.dumps({"jobs": jobs}, indent=2))


def sched(expr):
    return {"kind": "cron", "expr": expr, "display": expr}


def find(jobs, ref):
    for j in jobs:
        if j["id"] == ref or j.get("name") == ref:
            return j
    print(f"Failed to update job: Job not found: {ref}")
    sys.exit(1)


def main():
    if os.environ.get("FAKE_HERMES_FAIL"):
        print("Failed to create job: simulated failure")
        return 1
    ap = argparse.ArgumentParser()
    ap.add_argument("group")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("create")
    c.add_argument("schedule")
    c.add_argument("prompt", nargs="?")
    for f in ("--name", "--deliver", "--workdir", "--model", "--provider"):
        c.add_argument(f)
    c.add_argument("--skill", action="append")
    c.add_argument("--paused", action="store_true")
    e = sub.add_parser("edit")
    e.add_argument("job_id")
    for f in ("--name", "--schedule", "--prompt", "--workdir", "--model", "--provider"):
        e.add_argument(f)
    e.add_argument("--clear-skills", action="store_true")
    for name in ("pause", "resume", "run", "remove"):
        sub.add_parser(name).add_argument("job_id")
    r = sub.add_parser("runs")
    r.add_argument("job_id", nargs="?")
    r.add_argument("--limit", type=int, default=20)
    sub.add_parser("list")
    a = ap.parse_args()
    jobs = load()

    if a.cmd == "create":
        jid = uuid.uuid4().hex[:12]
        jobs.append({"id": jid, "name": a.name, "prompt": a.prompt, "schedule": sched(a.schedule),
                     "skills": a.skill or [], "workdir": a.workdir, "deliver": a.deliver,
                     "model": a.model, "enabled": not a.paused,
                     "state": "paused" if a.paused else "scheduled",
                     "paused_at": "2026-01-01T00:00:00" if a.paused else None,
                     "next_run_at": None if a.paused else "2026-10-12T08:00:00+02:00",
                     "last_run_at": None, "last_status": None})
        save(jobs)
        print(f"Created job: {jid}\n  Name: {a.name}\n  Schedule: {a.schedule}")
    elif a.cmd == "edit":
        j = find(jobs, a.job_id)
        for key in ("name", "prompt", "workdir", "model", "provider"):
            if getattr(a, key) is not None:
                j[key] = getattr(a, key)
        if a.schedule:
            j["schedule"] = sched(a.schedule)
        if a.clear_skills:
            j["skills"] = []
        save(jobs)
        print(f"Updated job: {j['id']}")
    elif a.cmd == "pause":
        j = find(jobs, a.job_id)
        j.update(enabled=False, state="paused", paused_at="2026-10-05T10:00:00", next_run_at=None)
        save(jobs)
        print(f"Paused job: {j['name']} ({j['id']})")
    elif a.cmd == "resume":
        j = find(jobs, a.job_id)
        j.update(enabled=True, state="scheduled", paused_at=None, next_run_at="2026-10-12T08:00:00+02:00")
        save(jobs)
        print(f"Resumed job: {j['name']} ({j['id']})")
    elif a.cmd == "run":
        j = find(jobs, a.job_id)
        j.update(last_run_at="2026-10-05T10:00:00+02:00", last_status="ok")
        save(jobs)
        print(f"Triggered job: {j['name']} ({j['id']})")
    elif a.cmd == "remove":
        j = find(jobs, a.job_id)
        save([x for x in jobs if x["id"] != j["id"]])
        print(f"Removed job: {j['name']} ({j['id']})")
    elif a.cmd == "runs":
        print(f"exec1  ok         job={a.job_id}  source=manual  2026-10-05T10:00:00")
    elif a.cmd == "list":
        for j in jobs:
            print(f"{j['id']}  {j['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
