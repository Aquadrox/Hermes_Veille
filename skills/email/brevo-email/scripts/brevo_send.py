#!/usr/bin/env python3
"""Send an email via the Brevo transactional API.

Reads BREVO_API_KEY (and optional BREVO_SENDER_EMAIL / BREVO_SENDER_NAME) from the
environment, then from --env-file (default ~/.hermes/.env). No SMTP client needed.

Recipient files hold one address per line; blank lines and lines starting with `#`
are ignored. With only BCC recipients, the sender itself is used as the visible `To`
(Brevo requires at least one `to`), so recipients never see each other.

Examples:
  brevo_send.py --to a@b.com --subject "Hi" --text "Hello"
  brevo_send.py --to a@b.com --subject "Report" --body-file r.md --attach r.pdf --verify
  brevo_send.py --bcc-file recipients.txt --subject "Digest" --text "Body" --verify
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


def _conf(env_file=ENV_FILE):
    file_env = _load_env_file(env_file)

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


def _split(value):
    """Comma-separated string -> list of stripped, non-empty addresses."""
    return [a.strip() for a in (value or "").split(",") if a.strip()]


def read_recipients(path):
    """One address per line; blank lines and `#` comments ignored."""
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        return [
            ln.strip()
            for ln in fh
            if ln.strip() and not ln.lstrip().startswith("#")
        ]


def _dedupe(addrs):
    seen, out = set(), []
    for a in addrs:
        if a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


def _read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _encode_attachment(path):
    with open(path, "rb") as fh:
        return {"content": base64.b64encode(fh.read()).decode(), "name": os.path.basename(path)}


def build_payload(args, sender, sender_name):
    """Build the Brevo payload from parsed args. Raises ValueError on bad input."""
    to = _split(args.to)
    if args.to_file:
        to += read_recipients(args.to_file)
    bcc = _split(args.bcc)
    if args.bcc_file:
        bcc += read_recipients(args.bcc_file)
    to, bcc = _dedupe(to), _dedupe(bcc)
    if not to and not bcc:
        raise ValueError("provide --to, --to-file, --bcc or --bcc-file (no recipient found)")

    text = _read_text(args.body_file) if args.body_file else args.text
    if not text and not args.html:
        raise ValueError("provide --text, --html or --body-file")

    payload = {
        "sender": {**({"name": sender_name} if sender_name else {}), "email": sender},
        # BCC only: the sender is the visible recipient, so nobody sees the list.
        "to": [{"email": a} for a in (to or [sender])],
        "subject": args.subject,
    }
    if bcc:
        payload["bcc"] = [{"email": a} for a in bcc]
    if args.html:
        payload["htmlContent"] = args.html
    if text:
        payload["textContent"] = text
    if args.cc:
        payload["cc"] = [{"email": a} for a in _split(args.cc)]
    if args.reply_to:
        payload["replyTo"] = {"email": args.reply_to}
    if args.attach:
        payload["attachment"] = [_encode_attachment(p) for p in args.attach]
    return payload, (to or bcc)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Send email via the Brevo API")
    ap.add_argument("--to", help="recipient(s), comma-separated")
    ap.add_argument("--to-file", dest="to_file", help="recipients file (one address per line)")
    ap.add_argument("--bcc", help="bcc recipient(s), comma-separated")
    ap.add_argument("--bcc-file", dest="bcc_file", help="bcc recipients file (one address per line)")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--text", help="plain-text body")
    ap.add_argument("--html", help="HTML body")
    ap.add_argument("--body-file", help="read the plain-text body from a UTF-8 file")
    ap.add_argument("--from", dest="sender", help="sender email (else BREVO_SENDER_EMAIL)")
    ap.add_argument("--from-name", dest="sender_name", help="sender display name")
    ap.add_argument("--reply-to", dest="reply_to")
    ap.add_argument("--cc", help="cc recipient(s), comma-separated")
    ap.add_argument("--attach", action="append", default=[], help="file to attach (repeatable)")
    ap.add_argument("--env-file", default=ENV_FILE, help="fallback file for BREVO_* (default ~/.hermes/.env)")
    ap.add_argument("--verify", action="store_true", help="poll the events API for delivery status")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    get = _conf(os.path.expanduser(args.env_file))
    api_key = get("BREVO_API_KEY")
    if not api_key:
        print(f"ERROR: BREVO_API_KEY not found (env or {args.env_file})", file=sys.stderr)
        return 2

    sender = args.sender or get("BREVO_SENDER_EMAIL")
    sender_name = args.sender_name or get("BREVO_SENDER_NAME")
    if not sender:
        print("ERROR: no sender (use --from or set BREVO_SENDER_EMAIL)", file=sys.stderr)
        return 2

    try:
        payload, real_recipients = build_payload(args, sender, sender_name)
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        resp = _api("POST", "/smtp/email", api_key, payload)
    except urllib.error.HTTPError as e:
        print(f"HTTP ERROR {e.code}: {e.read().decode()}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"NETWORK ERROR: {e.reason}", file=sys.stderr)
        return 1

    msg_id = resp.get("messageId")
    n_bcc = len(payload.get("bcc", []))
    shown = ", ".join(a["email"] for a in payload["to"])
    print(f"OK: email accepted — messageId {msg_id} — to {shown}" + (f" (+{n_bcc} bcc)" if n_bcc else ""))

    if args.verify and msg_id:
        # Events are indexed per recipient: track a real recipient, not the sender.
        target = urllib.parse.quote(real_recipients[0])
        for _ in range(6):
            time.sleep(10)
            try:
                ev = _api("GET", f"/smtp/statistics/events?limit=20&email={target}", api_key)
            except Exception as exc:  # noqa: BLE001 - best-effort verification
                print(f"(verification unavailable: {exc})", file=sys.stderr)
                return 0
            for item in ev.get("events", []):
                if item.get("messageId") == msg_id:
                    reason = f" — {item.get('reason')}" if item.get("reason") else ""
                    print(f"status: {item.get('event')}{reason}")
                    if item.get("event") in TERMINAL_EVENTS:
                        return 0
        print("status: no event yet (propagation delay)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
