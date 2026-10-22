"""Contrat du garde-fou avec Hermes : exécuté par le Python de Hermes DANS l'image (smoke test, test
d'intégration), avec l'identifiant du job d'une veille et le dossier de ses rapports.

    /opt/hermes/.venv/bin/python guard-probe.py <job_id> <dossier rapports>

Vérifie, par le système de plugins de Hermes lui-même, que veille-guard est chargé, qu'il refuse une
commande interdite et permet celles de la veille (sa première commande `veillectl`, avec le timeout du prompt) ;
que ce timeout (guard.COMMAND_TIMEOUT) reste sous les DEUX plafonds effectifs de Hermes, délai de tout appel
d'outil et premier plan du terminal, lus par Hermes lui-même (incident du 22/10/2026 : guard 1 200 s, effectif
420 s) ; et qu'une exécution terminée sans rapport est déclarée en échec (`[CRON_FAILURE]`), appels d'outils et
réponse finale reliés par le tour Hermes. Affiche les outils des exécutions planifiées.
"""
import os
import sys
from pathlib import Path

from agent.tool_executor import _resolve_concurrent_tool_timeout, _resolve_sequential_tool_timeout
from cron.scheduler import _cron_failure_marker_error, _resolve_cron_enabled_toolsets
from hermes_cli.config import load_config
from hermes_cli.plugins import discover_plugins, invoke_hook, resolve_pre_tool_block
from tools.terminal_tool import FOREGROUND_MAX_TIMEOUT

sys.path.append(os.environ.get("VEILLE_APP", "/opt/veille-app"))  # comme le plugin : après les modules de Hermes
from veille_core import guard  # noqa: E402

COMMAND_TIMEOUT = guard.COMMAND_TIMEOUT
job, rapports = sys.argv[1], sys.argv[2]
# Plafond unique des veilles (D3) sous ceux de Hermes ; None : délai désactivé dans la configuration.
limits = {"délai d'un appel d'outil": _resolve_sequential_tool_timeout(),
          "délai d'un lot d'appels parallèles": _resolve_concurrent_tool_timeout(),
          "premier plan du terminal": FOREGROUND_MAX_TIMEOUT}
low = {name: value for name, value in limits.items() if value is not None and value <= COMMAND_TIMEOUT}
if low:
    sys.exit(f"plafond de Hermes sous celui des veilles ({COMMAND_TIMEOUT} s) : {low}")
discover_plugins()
# Session renommée (compression du contexte) : la tâche identifie la veille, le tour relie ses appels.
ids = {"task_id": f"cron:{job}:probe", "session_id": "20260101_000000_probe", "turn_id": "probe"}
refused = resolve_pre_tool_block("terminal", {"command": "cat /opt/data/.env"}, **ids)
allowed = resolve_pre_tool_block("terminal", {"command": f"ls -t {rapports}/"}, **ids)
# Première commande `veillectl` du type de la veille (scan pour une veille composants), D : un horodatage.
name, doc, _ = guard.VEILLECTL[guard.session(task_id=ids["task_id"]).kind][0]
own = " ".join(filter(None, ("veillectl", name, Path(rapports).parent.name,
                             doc.format(r=rapports).replace("/D-", "/2026-01-01-00h00-"))))
command = resolve_pre_tool_block("terminal", {"command": own, "timeout": COMMAND_TIMEOUT}, **ids)
if not (refused and refused.startswith("veille-guard")):
    sys.exit(f"garde-fou absent : `cat /opt/data/.env` non refusé ({refused!r})")
if allowed is not None or command is not None:
    sys.exit(f"garde-fou trop strict : `ls` des rapports ou `{own}` (timeout {COMMAND_TIMEOUT}) refusé "
             f"({allowed!r}, {command!r})")
# Fin de cette exécution, qui n'a rien publié : échec selon Hermes lui-même.
final = invoke_hook("transform_llm_output", response_text="Terminé.", session_id=ids["session_id"],
                    turn_id="probe", model="", platform="cron")
if not any(isinstance(r, str) and "aucun rapport" in (_cron_failure_marker_error(r) or "") for r in final):
    sys.exit(f"garde-fou : exécution sans rapport non déclarée en échec ({final!r})")
toolsets = ", ".join(sorted(_resolve_cron_enabled_toolsets({}, load_config())))
under = ", ".join(f"{k} {v:g} s" for k, v in limits.items() if v is not None)
print(f"  garde-fou : chargé, refuse `cat /opt/data/.env`, permet `ls` et `veillectl {name}` ({COMMAND_TIMEOUT} s, "
      f"sous : {under}), échec sans rapport ; outils cron : {toolsets}")
