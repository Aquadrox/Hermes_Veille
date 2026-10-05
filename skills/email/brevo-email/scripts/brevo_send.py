#!/usr/bin/env python3
"""Send an email via the Brevo transactional API.

Reads BREVO_API_KEY (and optional BREVO_SENDER_EMAIL / BREVO_SENDER_NAME) from the
environment, then from ~/.hermes/.env. No SMTP client needed.

Examples:
  brevo_send.py --to a@b.com --subject "Hi" --text "Hello"
  brevo_send.py --to a@b.com --subject "Report" --body-file r.md --attach r.pdf --verify
  brevo_send.py --to a@b.com --subject "X" --html "<h1>hi</h1>" --from-name Alerts
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.brevo.com/v3"
ENV_FILE = os.path.expanduser("~/.hermes/.env")
TERMINAL_EVENTS = {"delivered", "error", "blocked", "bounced", "spam", "deferred"}


def _load_env_file(path):
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                env[k.strip()] = v
    except OSError:
        pass
    return env


def _conf():
    file_env = _load_env_file(ENV_FILE)

    def get(key, default=None):
        return os.environ.get(key) or file_env.get(key) or default

    return get


def _api(method, path, api_key, payload=None):
    headers = {"api-key": api_key, "accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API_BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=90) as r:
        body = r.read().decode()
    return json.loads(body) if body.strip() else {}


def _addrs(value):
    return [{"email": a.strip()} for a in value.split(",") if a.strip()]


def main():
    ap = argparse.ArgumentParser(description="Send email via the Brevo API")
    ap.add_argument("--to", help="recipient(s), comma-separated")
    ap.add_argument("--to-file", dest="to_file", help="read the recipient from a file (first non-empty line)")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--text", help="plain-text body")
    ap.add_argument("--html", help="HTML body")
    ap.add_argument("--body-file", help="read the plain-text body from a UTF-8 file")
    ap.add_argument("--from", dest="sender", help="sender email (else BREVO_SENDER_EMAIL)")
    ap.add_argument("--from-name", dest="sender_name", help="sender display name")
    ap.add_argument("--reply-to", dest="reply_to")
    ap.add_argument("--cc", help="cc recipient(s), comma-separated")
    ap.add_argument("--attach", action="append", default=[], help="file to attach (repeatable)")
    ap.add_argument("--verify", action="store_true", help="poll the events API for delivery status")
    args = ap.parse_args()

    if args.to_file:
        with open(os.path.expanduser(args.to_file), encoding="utf-8") as fh:
            first = next((ln.strip() for ln in fh if ln.strip()), "")
        if not first:
            print("ERROR: no recipient found in --to-file", file=sys.stderr)
            sys.exit(2)
        args.to = first
    if not args.to:
        print("ERROR: provide --to or --to-file", file=sys.stderr)
        sys.exit(2)

    get = _conf()
    api_key = get("BREVO_API_KEY")
    if not api_key:
        print("ERROR: BREVO_API_KEY not found (env or ~/.hermes/.env)", file=sys.stderr)
        sys.exit(2)

    sender = args.sender or get("BREVO_SENDER_EMAIL")
    sender_name = args.sender_name or get("BREVO_SENDER_NAME")
    if not sender:
        print("ERROR: no sender (use --from or set BREVO_SENDER_EMAIL)", file=sys.stderr)
        sys.exit(2)

    text = open(args.body_file, encoding="utf-8").read() if args.body_file else args.text
    if not text and not args.html:
        print("ERROR: provide --text, --html or --body-file", file=sys.stderr)
        sys.exit(2)

    payload = {
        "sender": {**({"name": sender_name} if sender_name else {}), "email": sender},
        "to": _addrs(args.to),
        "subject": args.subject,
    }
    if args.html:
        payload["htmlContent"] = args.html
    if text:
        payload["textContent"] = text
    if args.cc:
        payload["cc"] = _addrs(args.cc)
    if args.reply_to:
        payload["replyTo"] = {"email": args.reply_to}
    if args.attach:
        payload["attachment"] = [
            {
                "content": base64.b64encode(open(p, "rb").read()).decode(),
                "name": os.path.basename(p),
            }
            for p in args.attach
        ]

    try:
        resp = _api("POST", "/smtp/email", api_key, payload)
    except urllib.error.HTTPError as e:
        print(f"HTTP ERROR {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)

    msg_id = resp.get("messageId")
    print(f"OK: email accepted — messageId {msg_id} — to {args.to}")

    if args.verify and msg_id:
        target = urllib.parse.quote(payload["to"][0]["email"])
        for _ in range(6):
            time.sleep(10)
            try:
                ev = _api("GET", f"/smtp/statistics/events?limit=20&email={target}", api_key)
            except Exception as exc:  # noqa: BLE001 - best-effort verification
                print(f"(verification unavailable: {exc})", file=sys.stderr)
                return
            for item in ev.get("events", []):
                if item.get("messageId") == msg_id:
                    reason = f" — {item.get('reason')}" if item.get("reason") else ""
                    print(f"status: {item.get('event')}{reason}")
                    if item.get("event") in TERMINAL_EVENTS:
                        return
        print("status: no event yet (propagation delay)")


if __name__ == "__main__":
    main()
