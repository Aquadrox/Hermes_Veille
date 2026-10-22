"""Contrat du garde-fou avec Hermes : exécuté par le Python de Hermes DANS l'image (smoke test, test
d'intégration), avec l'identifiant du job d'une veille et le dossier de ses rapports.

    /opt/hermes/.venv/bin/python guard-probe.py <job_id> <dossier rapports>

Vérifie, par le système de plugins de Hermes lui-même, que veille-guard est chargé, qu'il refuse une
commande interdite et permet les commandes de la veille (scan au premier plan compris : plafond de l'image
>= guard.SCAN_TIMEOUT), et qu'une exécution terminée sans rapport est déclarée en échec (`[CRON_FAILURE]`),
appels d'outils et réponse finale reliés par le tour Hermes ; affiche les outils des exécutions planifiées.
"""
import sys
from pathlib import Path

from cron.scheduler import _cron_failure_marker_error, _resolve_cron_enabled_toolsets
from hermes_cli.config import load_config
from hermes_cli.plugins import discover_plugins, invoke_hook, resolve_pre_tool_block

job, rapports = sys.argv[1], sys.argv[2]
discover_plugins()
# Session renommée (compression du contexte) : la tâche identifie la veille, le tour relie ses appels.
ids = {"task_id": f"cron:{job}:probe", "session_id": "20260101_000000_probe", "turn_id": "probe"}
refused = resolve_pre_tool_block("terminal", {"command": "cat /opt/data/.env"}, **ids)
allowed = resolve_pre_tool_block("terminal", {"command": f"ls -t {rapports}/"}, **ids)
scan = resolve_pre_tool_block(
    "terminal", {"command": f"veillectl components scan {Path(rapports).parent.name}", "timeout": 1200}, **ids)
if not (refused and refused.startswith("veille-guard")):
    sys.exit(f"garde-fou absent : `cat /opt/data/.env` non refusé ({refused!r})")
if allowed is not None or scan is not None:
    sys.exit(f"garde-fou trop strict : `ls` des rapports ou scan (timeout 1200) refusé ({allowed!r}, {scan!r})")
# Fin de cette exécution, qui n'a rien publié : échec selon Hermes lui-même.
final = invoke_hook("transform_llm_output", response_text="Terminé.", session_id=ids["session_id"],
                    turn_id="probe", model="", platform="cron")
if not any(isinstance(r, str) and "aucun rapport" in (_cron_failure_marker_error(r) or "") for r in final):
    sys.exit(f"garde-fou : exécution sans rapport non déclarée en échec ({final!r})")
toolsets = ", ".join(sorted(_resolve_cron_enabled_toolsets({}, load_config())))
print(f"  garde-fou : chargé, refuse `cat /opt/data/.env`, permet `ls` et le scan, échec sans rapport ; "
      f"outils cron : {toolsets}")
