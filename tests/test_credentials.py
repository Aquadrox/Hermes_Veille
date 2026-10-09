"""Clés d'API des sources saisies dans l'interface : stockage, priorité du .env, page Sources, MCP."""
import json
import re
import stat

import pytest

from veille_core import credentials, digikey, service

SECRET = "Zx9-super-secret-value-1234"


@pytest.fixture(autouse=True)
def no_env_keys(env, monkeypatch):
    for k in ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)


def save(**values):
    return service.save_credentials("digikey", values, actor="test")


def test_save_get_status_and_file_mode(env):
    assert credentials.status("digikey")["configured"] is False and digikey.credentials() is None
    assert save(DIGIKEY_CLIENT_ID="client-id-abcd", DIGIKEY_CLIENT_SECRET=SECRET) == [
        "DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"]
    assert digikey.credentials() == ("client-id-abcd", SECRET)
    st = credentials.status("digikey")
    assert st["configured"] and not st["env"] and st["updated_at"]
    assert st["fields"]["DIGIKEY_CLIENT_SECRET"] == {"label": "Client Secret", "secret": True,
                                                      "origin": "ui", "masked": "••••1234"}
    assert SECRET not in json.dumps(st)
    assert stat.S_IMODE(credentials.path().stat().st_mode) == 0o600
    # champ vide = inchangé ; même valeur = rien à écrire
    assert save(DIGIKEY_CLIENT_ID="", DIGIKEY_CLIENT_SECRET=SECRET) == []
    assert save(DIGIKEY_CLIENT_ID="client-id-efgh") == ["DIGIKEY_CLIENT_ID"]
    assert digikey.credentials() == ("client-id-efgh", SECRET)
    audit = json.dumps(service.read_audit())
    assert "credentials-save" in audit and SECRET not in audit and "client-id" not in audit


@pytest.mark.parametrize("value", ["court", "avec espace dedans", "x" * 300, "inject\nNEXT=1", "<script>alert"])
def test_invalid_values_rejected(value):
    with pytest.raises(credentials.CredentialsError, match="format invalide"):
        save(DIGIKEY_CLIENT_SECRET=value)
    assert not credentials.path().exists()


def test_env_has_priority_and_is_read_only(env, monkeypatch):
    save(DIGIKEY_CLIENT_ID="client-id-ui-1", DIGIKEY_CLIENT_SECRET=SECRET)
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "client-id-from-env")
    assert digikey.credentials() == ("client-id-from-env", SECRET)
    st = credentials.status("digikey")
    assert st["env"] and st["fields"]["DIGIKEY_CLIENT_ID"]["origin"] == "env"
    with pytest.raises(credentials.CredentialsError, match="défini dans le .env"):
        save(DIGIKEY_CLIENT_ID="client-id-ui-2")


def test_delete_unknown_and_corrupted(env):
    with pytest.raises(credentials.CredentialsError, match="source inconnue"):
        service.save_credentials("mouser", {})
    save(DIGIKEY_CLIENT_ID="client-id-abcd", DIGIKEY_CLIENT_SECRET=SECRET)
    assert service.delete_credentials("digikey") is True and service.delete_credentials("digikey") is False
    assert digikey.credentials() is None
    credentials.path().write_text("{pas du json")
    st = credentials.status("digikey")
    assert st["configured"] is False and "illisible" in st["error"]
    assert credentials.get("DIGIKEY_CLIENT_ID") is None
    save(DIGIKEY_CLIENT_ID="client-id-abcd", DIGIKEY_CLIENT_SECRET=SECRET)  # remplace le fichier illisible
    assert credentials.status("digikey")["error"] is None and digikey.credentials()


def test_connection_test(env, monkeypatch):
    with pytest.raises(service.ServiceError, match="incomplets"):
        service.check_credentials("digikey")
    save(DIGIKEY_CLIENT_ID="client-id-abcd", DIGIKEY_CLIENT_SECRET=SECRET)
    monkeypatch.setattr(digikey.Client, "check", lambda self: None)
    assert "réussie" in service.check_credentials("digikey")

    def refuse(self):
        raise digikey.DigiKeyError("authentification Digi-Key refusée (HTTP 401)")
    monkeypatch.setattr(digikey.Client, "check", refuse)
    with pytest.raises(service.ServiceError, match="HTTP 401"):
        service.check_credentials("digikey")


def test_client_check_gets_token():
    from test_digikey import TOKEN, Fake
    f = Fake(TOKEN)
    f.client().check()
    assert f.requests[0].full_url.endswith("/v1/oauth2/token")


# ------------------------------------------------------------- interface
@pytest.fixture
def ui(env, monkeypatch):
    from fastapi.testclient import TestClient
    from test_ui import PASSWORD, login

    from veille_ui import auth
    monkeypatch.setenv("VEILLE_UI_PASSWORD_HASH", auth.hash_password(PASSWORD))
    monkeypatch.setenv("VEILLE_UI_SECRET", "s" * 64)
    auth._failures.clear()
    from veille_ui.app import create_app
    client = TestClient(create_app(), base_url="http://localhost")
    return client, login(client)


def test_ui_sources_page(ui, monkeypatch):
    client, csrf = ui
    page = client.get("/sources")
    assert page.status_code == 200 and "non configurée" in page.text and 'href="/sources"' in page.text
    assert "style=" not in page.text and "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert client.post("/sources/digikey/save", data={"DIGIKEY_CLIENT_SECRET": SECRET}).status_code == 403  # CSRF
    r = client.post("/sources/digikey/save", follow_redirects=False,
                    data={"csrf": csrf, "DIGIKEY_CLIENT_ID": "client-id-abcd", "DIGIKEY_CLIENT_SECRET": SECRET})
    assert r.status_code == 303 and r.headers["location"] == "/sources?ok=creds-saved"
    page = client.get("/sources?ok=creds-saved").text
    assert SECRET not in page and "client-id-abcd" not in page and "••••1234" in page
    assert re.search(r'name="DIGIKEY_CLIENT_SECRET" type="password" value=""', page)
    assert "configurée" in page and "Tester la connexion" in page
    r = client.post("/sources/digikey/save", follow_redirects=False, data={"csrf": csrf})
    assert r.headers["location"] == "/sources?ok=creds-unchanged"
    r = client.post("/sources/digikey/save", data={"csrf": csrf, "DIGIKEY_CLIENT_SECRET": "a b"})
    assert r.status_code == 400 and "format invalide" in r.text and "a b" not in r.text
    monkeypatch.setattr(digikey.Client, "check", lambda self: None)
    assert "Connexion à Digi-Key réussie." in client.post("/sources/digikey/test", data={"csrf": csrf}).text
    assert client.post("/sources/inconnue/save", data={"csrf": csrf}).status_code == 400
    assert client.post("/sources/digikey/bidon", data={"csrf": csrf}).status_code == 404
    r = client.post("/sources/digikey/delete", data={"csrf": csrf}, follow_redirects=False)
    assert r.headers["location"] == "/sources?ok=creds-deleted" and digikey.credentials() is None


def test_ui_env_defined_keys_are_read_only(ui, monkeypatch):
    client, _ = ui
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "client-id-from-env")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", SECRET)
    page = client.get("/sources").text
    assert "configurée (.env)" in page and SECRET not in page and "Supprimer les clés" not in page
    assert page.count(" disabled>") == 2


def test_ui_components_page_suggests_configuring_digikey(ui, env):
    client, _ = ui
    service.create_veille(name="BOM", subject="", schedule_expr=service.COMPONENTS_SCHEDULE,
                          recipients=["a@example.org"], brief="# b\n", kind="composants")
    assert "configurer les clés Digi-Key" in client.get("/veilles/bom").text
    save(DIGIKEY_CLIENT_ID="client-id-abcd", DIGIKEY_CLIENT_SECRET=SECRET)
    assert "configurer les clés Digi-Key" not in client.get("/veilles/bom").text


def test_mcp_health_reports_sources_without_values(env):
    pytest.importorskip("mcp")
    from test_mcp import call, data

    from veille_mcp.server import build_server
    save(DIGIKEY_CLIENT_ID="client-id-abcd", DIGIKEY_CLIENT_SECRET=SECRET)
    result = call(build_server(), "health")
    assert data(result)["sources"] == [{"name": "Digi-Key", "configured": True, "from_env": False}]
    assert SECRET not in json.dumps(result.model_dump(mode="json"))
