"""Contrat du moteur de recherche web avec Hermes : exécuté par le Python de Hermes DANS l'image (smoke test),
plugin deepseek-search-fetch activé et `web.backend` = deepseek (comme l'amorçage).

    /opt/hermes/.venv/bin/python web-probe.py

Un faux service DeepSeek Search & Fetch écoute sur la boucle locale (seul autre hôte que le plugin accepte) :
aucune clé réelle, aucun appel facturé. Vérifie, par les outils web_search / web_extract de Hermes eux-mêmes,
que le plugin est choisi, qu'il s'authentifie et que ses résultats reviennent à l'agent.
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "sk-sonde"
SEARCH = {"results": [{"title": "Sonde veille", "url": "https://example.org/sonde", "summary": "Résumé.",
                       "rerank_score": 2.1, "authority": 3, "date": "2026-10-20T08:00:00Z"}]}
FETCH = {"title": "Page sonde", "content": "\\# Titre de la sonde"}  # le service échappe la ponctuation
seen = []


class FakeDeepSeek(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - nom imposé par http.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append((self.path, self.headers.get("Authorization"), body))
        payload = {"/v1/tools/search": SEARCH, "/v1/tools/fetch": FETCH}.get(self.path)
        data = json.dumps(payload or {"error": {"message": "inconnu"}}).encode()
        self.send_response(200 if payload else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDeepSeek)
threading.Thread(target=server.serve_forever, daemon=True).start()
# Lus à l'import du plugin : avant la découverte des plugins.
os.environ.update(DEEPSEEK_API_KEY=KEY, DEEPSEEK_BASE_URL=f"http://127.0.0.1:{server.server_port}")

from hermes_cli.plugins import discover_plugins  # noqa: E402
from tools import web_tools  # noqa: E402

discover_plugins()
backends = (web_tools._get_search_backend(), web_tools._get_extract_backend())
if backends != ("deepseek", "deepseek"):
    sys.exit(f"moteur web : {backends} au lieu de deepseek")
found = json.loads(web_tools.web_search_tool("sonde veille hermes", 3))
hits = (found.get("data") or {}).get("web") or []
if not found.get("success") or not hits or hits[0].get("title") != "Sonde veille":
    sys.exit(f"web_search ne passe pas par DeepSeek : {found}")
# Adresse IP publique littérale : le filtre anti-SSRF de Hermes la laisse passer sans résolution DNS.
page = json.loads(asyncio.run(web_tools.web_extract_tool(["https://93.184.215.14/sonde"])))
if "# Titre de la sonde" not in json.dumps(page, ensure_ascii=False):
    sys.exit(f"web_extract ne passe pas par DeepSeek : {page}")
if [p for p, _, _ in seen] != ["/v1/tools/search", "/v1/tools/fetch"] or {a for _, a, _ in seen} != {f"Bearer {KEY}"}:
    sys.exit(f"requêtes inattendues vers DeepSeek : {seen}")
print(f"  moteur web : deepseek (recherche et lecture) ; requêtes authentifiées : {', '.join(p for p, _, _ in seen)}")
