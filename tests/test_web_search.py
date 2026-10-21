"""Recherche web par DeepSeek Search & Fetch : plugin vendorisé, image et amorçage.

Le comportement du plugin dans Hermes (outils web_search / web_extract, faux service local) est vérifié par
le smoke test (scripts/web-probe.py), l'amorçage complet par le test d'intégration.
"""
import hashlib
import re

from conftest import REPO

from veille_core import guard

PLUGIN = REPO / "docker" / "hermes-plugins" / "deepseek-search-fetch"
SEED = (REPO / "docker" / "cont-init.d" / "50-seed-veille").read_text()


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split("."))


def test_vendored_plugin_is_the_reviewed_commit_unchanged():
    """Code tiers exécuté dans Hermes avec ses secrets : toute modification passe par une nouvelle relecture."""
    notes = (PLUGIN / "VENDORED.md").read_text()
    assert "b17ae50d243beacfd6ee9171e42f2af29b3c6d65" in notes
    for name in ("__init__.py", "plugin.yaml"):
        assert hashlib.sha256((PLUGIN / name).read_bytes()).hexdigest() in notes, f"{name} modifié sans relecture"
    assert (PLUGIN / "LICENSE").read_text().startswith("MIT License")


def test_plugin_only_talks_to_deepseek_and_never_logs_the_key():
    code = (PLUGIN / "__init__.py").read_text()
    assert '_DEFAULT_BASE = "https://api.deepseek.com"' in code
    assert all(host.endswith(".deepseek.com") for host in re.findall(r"https?://([A-Za-z0-9.-]+)", code))
    assert "subprocess" not in code and "open(" not in code
    assert not re.search(r"log\w*\.\w+\([^)]*api_key", code)


def test_image_meets_the_plugin_hermes_requirement():
    manifest = (PLUGIN / "plugin.yaml").read_text()
    required = re.search(r'requires_hermes: ">=([\d.]+)"', manifest).group(1)
    dockerfile = (REPO / "Dockerfile").read_text()
    base = re.search(r"^FROM nousresearch/hermes-agent:v([\d.]+)$", dockerfile, re.M).group(1)
    assert _version(base) >= _version(required)
    assert "COPY docker/hermes-plugins/deepseek-search-fetch/ /opt/hermes/plugins/deepseek-search-fetch/" in dockerfile
    assert "provides_web_providers:\n  - deepseek" in manifest


def test_seed_pins_deepseek_or_restores_the_hermes_engine():
    assert '"${VEILLE_WEB_BACKEND:-deepseek}" = deepseek' in SEED and '[ -n "${DEEPSEEK_API_KEY:-}" ]' in SEED
    on, off = SEED.split("2 ter. Moteur de recherche web", 1)[1].split("    else\n", 1)
    assert "plugins enable deepseek-search-fetch" in on
    assert "set_config web.backend deepseek" in on  # recherche ET lecture de pages, sans repli
    # Retour arrière : plugin désactivé (Hermes le choisirait encore) et réglage retiré (sinon erreur stricte).
    assert "plugins disable deepseek-search-fetch" in off and "config unset web.backend" in off
    assert SEED.index("2 ter. Moteur") > SEED.index("plugins enable veille-guard")


def test_web_tools_keep_their_names_for_the_guard():
    """Le plugin ne change que le moteur : le garde-fou autorise et marque toujours les mêmes outils."""
    assert {"web_search", "web_extract"} <= set(guard.ALLOWED_ARGS)
    assert not re.search(r"^provides_(tools|hooks):", (PLUGIN / "plugin.yaml").read_text(), re.M)
