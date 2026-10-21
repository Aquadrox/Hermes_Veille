"""Contrat du garde-fou avec Hermes : exécuté par le Python de Hermes DANS l'image (smoke test, test
d'intégration), avec l'identifiant du job d'une veille et le dossier de ses rapports.

    /opt/hermes/.venv/bin/python guard-probe.py <job_id> <dossier rapports>

Vérifie, par le système de plugins de Hermes lui-même, que veille-guard est chargé, qu'il refuse une
commande interdite et permet une commande de la veille ; affiche les outils des exécutions planifiées.
"""
import sys

from cron.scheduler import _resolve_cron_enabled_toolsets
from hermes_cli.config import load_config
from hermes_cli.plugins import discover_plugins, resolve_pre_tool_block

job, rapports = sys.argv[1], sys.argv[2]
discover_plugins()
ids = {"task_id": f"cron:{job}:probe", "session_id": "20260101_000000_probe"}  # session renommée : la tâche fait foi
refused = resolve_pre_tool_block("terminal", {"command": "cat /opt/data/.env"}, **ids)
allowed = resolve_pre_tool_block("terminal", {"command": f"ls -t {rapports}/"}, **ids)
if not (refused and refused.startswith("veille-guard")):
    sys.exit(f"garde-fou absent : `cat /opt/data/.env` non refusé ({refused!r})")
if allowed is not None:
    sys.exit(f"garde-fou trop strict : `ls` des rapports refusé ({allowed!r})")
toolsets = ", ".join(sorted(_resolve_cron_enabled_toolsets({}, load_config())))
print(f"  garde-fou : chargé, refuse `cat /opt/data/.env`, permet `ls` des rapports ; outils cron : {toolsets}")
