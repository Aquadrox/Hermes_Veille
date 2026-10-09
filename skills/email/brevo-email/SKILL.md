---
name: brevo-email
description: "Use when sending email via Brevo API (attachments)."
version: 1.1.0
author: agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [email, brevo, api, transactional, attachments]
    related_skills: [himalaya]
---

# Brevo Email (API)

Send transactional email through Brevo's HTTP API (`api.brevo.com`) — text or HTML body, CC, reply-to, and file attachments — with a single `POST`. No SMTP client, no mailbox configuration.

**This is the user's default way to send email.** Credentials live in `~/.hermes/.env` as `BREVO_API_KEY`, plus optional `BREVO_SENDER_EMAIL` / `BREVO_SENDER_NAME`.

## When to Use

- Send a report, alert, digest, or summary by email (optionally with a PDF/CSV attachment).
- Any task that must deliver mail from a server with no local MTA or SMTP account.
- For *reading / managing* a mailbox, use the `himalaya` skill instead.

## Prerequisites

- `BREVO_API_KEY` in the environment or in `~/.hermes/.env`. Hermes loads `.env` at startup, but a **running process may predate the key** — helper scripts must also fall back to reading the file directly (the bundled script does).
- A **validated sender** in the Brevo account (see Pitfalls — this is the #1 failure cause).

## Key Facts

| Item | Value |
|---|---|
| Send endpoint | `POST https://api.brevo.com/v3/smtp/email` |
| Auth header | `api-key: <BREVO_API_KEY>` |
| List senders | `GET /v3/senders` |
| Account / quota | `GET /v3/account` |
| Delivery events | `GET /v3/smtp/statistics/events?email=<addr>&limit=N` |
| Attachment field | `{"content": "<base64>", "name": "file.pdf"}` |
| Success | HTTP `201` + `{"messageId": "<...>"}` |

## Procedure

Use the bundled helper (run `--help` for all flags):

```bash
SCRIPT=~/.hermes/skills/email/brevo-email/scripts/brevo_send.py

# Simple text
python3 "$SCRIPT" --to someone@example.com --subject "Hello" --text "Body"

# With attachment + delivery verification
python3 "$SCRIPT" --to someone@example.com --subject "Weekly report" \
  --body-file report.md --attach report.pdf --verify

# HTML, CC, custom sender name
python3 "$SCRIPT" --to a@b.com --cc c@d.com --subject "X" \
  --html "<h1>Hi</h1>" --from-name "Alerts"

# Large HTML body from a file (no command-line size limit), with a text fallback
python3 "$SCRIPT" --bcc-file recipients.txt --subject "Digest" --html-file digest.html --text "Body"

# Recipients read from a file (one address per line, `#` comments allowed)
python3 "$SCRIPT" --to-file recipients.txt --subject "Digest" --text "Body"

# Several recipients who must NOT see each other: BCC (the sender is the visible To)
python3 "$SCRIPT" --bcc-file recipients.txt --subject "Digest" --text "Body" --verify

# Credentials from another .env (e.g. on a host without Hermes)
python3 "$SCRIPT" --env-file /path/to/.env --to ops@x.com --subject "Alert" --text "…"
```

Or raw curl:

```bash
curl -s -X POST https://api.brevo.com/v3/smtp/email \
  -H "api-key: $BREVO_API_KEY" -H "Content-Type: application/json" \
  -d '{"sender":{"email":"from@validated.com"},"to":[{"email":"to@x.com"}],
       "subject":"Hi","textContent":"Body"}'
```

## Common Pitfalls

- **Sender must be validated — the #1 gotcha.** Brevo returns success (HTTP 201 / SMTP 250) even when the sender is NOT authorized, then **rejects delivery after queueing**. The only visible trace is the events API: `event:"error"`, `reason:"Sending has been rejected because the sender you used <addr> is not valid. Validate your sender or authenticate your domain"`. Always send from an address listed by `GET /v3/senders`. Never use the SMTP relay login (`xxxx@smtp-brevo.com`) as the From — it is a login, not a sender.
- **From gets rewritten** to `<local>@<accountid>.brevosend.com` unless the account has an authenticated custom domain. Harmless — the recipient just sees a "via brevosend.com" label.
- **Accepted is not delivered.** A 201 only means queued. Prove delivery by polling the events API (`--verify`) or via Brevo → Statistics → Email → logs.
- **Free plan has a monthly quota** (check `GET /v3/account` → `plan`). A quota-exhausted account silently stops delivering.
- **Prefer the API over raw `smtplib`**: the API adds `Date` / `Message-ID` / MIME correctly, whereas a hand-rolled `smtplib` message often lacks them and gets spam-filtered.
- **Never hardcode the API key** in a skill, script, or repo — it belongs in `~/.hermes/.env` (mode 600).

## Verification Checklist

- Response is HTTP `201` with a `messageId`.
- `--verify` (or the events API) shows `event:"delivered"` for that `messageId`.
- If it shows `error` / `blocked`, read the `reason` field — almost always an unvalidated sender.
