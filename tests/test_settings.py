"""Paramètres de l'interface : adresses d'alerte du chien de garde (stockage, priorité, page, watchdog)."""
import importlib.util
import json
import re
import stat
import textwrap
from pathlib import Path

import pytest
from test_mcp import tools
from test_ui import client, login  # noqa: F401 - fixture partagée

from veille_core import service, settings, store
from veille_mcp.server import build_server

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("watchdog", REPO / "scripts" / "watchdog.py")
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


@pytest.fixture(autouse=True)
def no_env_mail(env, monkeypatch):
    for k in ("ALERT_EMAIL", "BREVO_SENDER_EMAIL"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def mails(tmp_path, monkeypatch):
    """Faux skill brevo-email (vrai sous-processus) : journalise ses arguments."""
    script, log = tmp_path / "fake_brevo.py", tmp_path / "mails.jsonl"
    script.write_text(textwrap.dedent("""
        import json, os, sys
        if os.environ.get("FAKE_BREVO_FAIL"):
            print("HTTP ERROR 401"); sys.exit(1)
        with open(os.environ["FAKE_BREVO_LOG"], "a") as fh:
            fh.write(json.dumps(sys.argv[1:]) + "\\n")
        print("OK: email accepted")
    """))
    monkeypatch.setenv("BREVO_SCRIPT", str(script))
    monkeypatch.setenv("FAKE_BREVO_LOG", str(log))
    return lambda: [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def write_env(env, text):
    (env / ".env").write_text(text)


# ----------------------------------------------------------------- stockage et priorité
def test_origin_priority_ui_then_env_then_sender(env):
    assert settings.alert_status() == {"saved": [], "effective": [], "origin": None, "env_ignored": False,
                                       "error": None, "updated_at": None}
    write_env(env, "BREVO_SENDER_EMAIL=sender@example.org\n")
    st = settings.alert_status()
    assert st["effective"] == ["sender@example.org"] and st["origin"] == "sender"
    write_env(env, "BREVO_SENDER_EMAIL=sender@example.org\nALERT_EMAIL='ops@example.org, dsi@example.org'\n")
    st = settings.alert_status()
    assert st["effective"] == ["ops@example.org", "dsi@example.org"] and st["origin"] == "env"
    assert service.save_alert_emails("admin@example.org\nAdmin@example.org; rssi@example.org", actor="test")
    st = settings.alert_status()
    assert st["effective"] == st["saved"] == ["admin@example.org", "rssi@example.org"]  # dédoublonnées
    assert st["origin"] == "ui" and st["env_ignored"] and st["updated_at"]


def test_save_file_mode_unchanged_clear_and_audit(env):
    assert service.save_alert_emails("admin@example.org", actor="test") is True
    assert stat.S_IMODE(settings.path().stat().st_mode) == 0o644  # lu par le chien de garde (root, hôte)
    assert service.save_alert_emails(" admin@example.org \n", actor="test") is False  # inchangé : pas d'écriture
    assert service.save_alert_emails("", actor="test") is True  # vide : retour au .env
    assert settings.alert_status()["saved"] == [] and settings.alert_status()["origin"] is None
    assert service.save_alert_emails("", actor="test") is False
    audit = [e for e in service.read_audit() if e["action"] == "settings-alert-emails"]
    assert [e["detail"] for e in audit] == ["0 adresse(s)", "1 adresse(s)"]
    write_env(env, "ALERT_EMAIL=Admin@example.org\n")
    service.save_alert_emails("admin@example.org", actor="test")
    assert settings.alert_status()["env_ignored"]
    write_env(env, "ALERT_EMAIL=admin@example.org\n")
    assert not settings.alert_status()["env_ignored"]  # même adresse : rien d'ignoré
    assert "admin@example.org" not in json.dumps(audit)


@pytest.mark.parametrize("text,match", [
    ("pas-une-adresse", "adresse"),
    ("a@example.org\n<script>alert(1)</script>", "adresse"),
    ("\n".join(f"a{i}@example.org" for i in range(settings.MAX_ALERT_EMAILS + 1)), "au plus"),
])
def test_invalid_rejected_and_nothing_written(env, text, match):
    with pytest.raises(store.StoreError, match=match):
        service.save_alert_emails(text, actor="test")
    assert not settings.path().exists()


def test_corrupted_file_falls_back_and_is_replaced(env):
    write_env(env, "ALERT_EMAIL=ops@example.org\n")
    settings.path().parent.mkdir(parents=True, exist_ok=True)
    settings.path().write_text("{pas du json")
    st = settings.alert_status()
    assert "illisible" in st["error"] and st["effective"] == ["ops@example.org"] and st["origin"] == "env"
    assert service.save_alert_emails("admin@example.org", actor="test")
    assert settings.alert_status()["error"] is None and settings.alert_status()["origin"] == "ui"


# ----------------------------------------------------------------- e-mail de test
def test_send_test_alert(env, mails, monkeypatch):
    with pytest.raises(service.ServiceError, match="aucune adresse"):
        service.send_test_alert(actor="test")
    service.save_alert_emails("admin@example.org\nrssi@example.org", actor="test")
    assert "OK" in service.send_test_alert(actor="test")
    args = mails()[0]
    assert args[args.index("--to") + 1] == "admin@example.org,rssi@example.org" and "--bcc-file" not in args
    assert args[args.index("--env-file") + 1] == str(env / ".env")
    monkeypatch.setenv("FAKE_BREVO_FAIL", "1")
    with pytest.raises(service.ServiceError, match="401"):
        service.send_test_alert(actor="test")
    assert [e["detail"] for e in service.read_audit() if e["action"] == "test-alert"] == ["échec", "ok"]


# ----------------------------------------------------------------- chien de garde (hôte)
ORIGINS = {"ui": "interface", "env": ".env", "sender": "expéditeur Brevo", None: None}
MALFORMED = ['"admin@example.org"', "5", "[1, null]", "[]", '["sans-arobase"]', '{"x@example.org": 1}', "{corrompu"]


@pytest.mark.parametrize("env_text,ui,raw", [
    ("", None, None),
    ("BREVO_SENDER_EMAIL=sender@example.org\n", None, None),
    ("BREVO_SENDER_EMAIL=sender@example.org\nALERT_EMAIL=ops@example.org\n", None, None),
    ("ALERT_EMAIL='ops@example.org , dsi@example.org'\n", None, None),
    ("ALERT_EMAIL=ops@example.org\n", "admin@example.org\nrssi@example.org", None),
    *[("ALERT_EMAIL=ops@example.org\n", None, '{"alert_emails": ' + p + "}") for p in MALFORMED],
    ("ALERT_EMAIL=ops@example.org\n", None, "{corrompu"),
])
def test_watchdog_resolves_like_the_interface(env, env_text, ui, raw):
    """Même source de vérité des deux côtés : l'interface affiche ce que le chien de garde utilisera."""
    write_env(env, env_text)
    if raw:
        settings.path().parent.mkdir(parents=True, exist_ok=True)
        settings.path().write_text(raw)
    elif ui:
        service.save_alert_emails(ui, actor="test")
    to, origin = watchdog.alert_recipients(str(env), watchdog.load_env(str(env / ".env")))
    st = settings.alert_status()
    assert (to.split(",") if to else []) == st["effective"] and origin == ORIGINS[st["origin"]]


@pytest.mark.parametrize("payload", MALFORMED)
def test_malformed_settings_page_and_save(client, env, payload):  # noqa: F811
    """Fichier édité à la main : la page s'affiche (repli .env), un enregistrement le remplace."""
    write_env(env, "ALERT_EMAIL=ops@example.org\n")
    settings.path().parent.mkdir(parents=True, exist_ok=True)
    settings.path().write_text('{"alert_emails": ' + payload + "}")
    csrf = login(client)
    r = client.get("/settings")
    assert r.status_code == 200 and "ops@example.org" in r.text and "repli .env" in r.text
    assert service.save_alert_emails("admin@example.org", actor="test")
    assert settings.alert_status()["saved"] == ["admin@example.org"] and csrf


def test_watchdog_signals_recipient_change_to_old_and_new(env, tmp_path, capsys):
    """Le fichier des paramètres est sur le volume (accessible au conteneur) : pas de détournement silencieux."""
    write_env(env, "ALERT_EMAIL=ops@example.org\n")
    state = tmp_path / "state.json"

    def check(*extra):
        watchdog.main(["--volume", str(env), "--skip-container", "--env-file", str(env / ".env"),
                       "--state-file", str(state), "--brevo-script", "/nonexistent", *extra])
        return capsys.readouterr().out

    assert "changement de destinataires" not in check("--dry-run")  # premier contrôle : rien à comparer
    watchdog.save_state(str(state), {"alert_to": "ops@example.org"})
    service.save_alert_emails("pirate@example.org", actor="test")
    out = check("--dry-run")
    assert "destinataires des alertes modifiés" in out and "À      : ops@example.org,pirate@example.org" in out
    assert "Auparavant : ops@example.org" in out
    out = check()  # envoi en échec (script absent) : l'ancien destinataire est conservé, nouvel essai
    assert "changement de destinataires signalé : False" in out
    assert json.loads(state.read_text())["alert_to"] == "ops@example.org"
    watchdog.save_state(str(state), {"alert_to": "pirate@example.org"})
    assert "changement de destinataires" not in check("--dry-run")  # inchangé : silence
    assert "changement de destinataires" not in check("--dry-run", "--to", "x@example.org")  # --to : ignoré


def test_watchdog_main_uses_interface_address(env, tmp_path, capsys):
    service.save_alert_emails("admin@example.org", actor="test")
    write_env(env, "ALERT_EMAIL=ops@example.org\n")
    code = watchdog.main(["--volume", str(env), "--skip-container", "--env-file", str(env / ".env"),
                          "--state-file", str(tmp_path / "state.json"), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 1  # volume sans veille : problème signalé, e-mail (simulé) à l'adresse de l'interface
    assert "alertes → admin@example.org (interface)" in out and "À      : admin@example.org" in out
    watchdog.main(["--volume", str(env), "--skip-container", "--env-file", str(env / ".env"), "--to", "x@example.org",
                   "--state-file", str(tmp_path / "state.json"), "--dry-run"])
    assert "alertes → x@example.org (--to)" in capsys.readouterr().out  # option explicite prioritaire


# ----------------------------------------------------------------- interface
def test_settings_page_requires_login_and_csrf(client):  # noqa: F811
    assert client.get("/settings", follow_redirects=False).status_code == 303
    login(client)
    r = client.post("/settings/save", data={"alert_emails": "admin@example.org"})
    assert r.status_code == 403 and not settings.path().exists()


def test_settings_page_save_test_and_errors(client, env, mails):  # noqa: F811
    csrf = login(client)
    write_env(env, "ALERT_EMAIL=ops@example.org\n")
    page = client.get("/settings").text
    assert 'href="/settings"' in page and "ops@example.org" in page and "repli .env" in page
    r = client.post("/settings/save", data={"csrf": csrf, "alert_emails": "admin@example.org"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings?ok=settings-saved"
    page = client.get("/settings?ok=settings-saved").text
    assert "Paramètres enregistrés" in page and "ALERT_EMAIL du .env est ignoré" in page
    assert re.search(r'<textarea name="alert_emails"[^>]*>admin@example.org</textarea>', page)
    r = client.post("/settings/save", data={"csrf": csrf, "alert_emails": "admin@example.org"}, follow_redirects=False)
    assert r.headers["location"] == "/settings?ok=settings-unchanged"
    # erreur : saisie conservée, échappée (aucun HTML réfléchi), fichier intact
    r = client.post("/settings/save", data={"csrf": csrf, "alert_emails": "</textarea><script>x()</script>"})
    assert r.status_code == 400 and "<script>x()" not in r.text and "&lt;/textarea&gt;&lt;script&gt;" in r.text
    assert settings.alert_status()["saved"] == ["admin@example.org"]
    r = client.post("/settings/test", data={"csrf": csrf}, follow_redirects=False)
    assert r.headers["location"] == "/settings?ok=alert-mail" and len(mails()) == 1
    assert client.post("/settings/other", data={"csrf": csrf}).status_code == 404


def test_test_alert_error_is_shown(client, monkeypatch, mails):  # noqa: F811
    csrf = login(client)
    r = client.post("/settings/test", data={"csrf": csrf})
    assert r.status_code == 400 and "aucune adresse d&#39;alerte" in r.text and mails() == []


def test_not_exposed_to_mcp(env):
    """Telegram / MCP ne doivent pas pouvoir détourner les alertes de panne."""
    names = " ".join(tools(build_server()))
    assert "alert" not in names and "setting" not in names
    source = (REPO / "veille_mcp" / "server.py").read_text(encoding="utf-8")
    assert not re.search(r"alert_status|save_alert_emails|send_test_alert|\bsettings\b", source)
