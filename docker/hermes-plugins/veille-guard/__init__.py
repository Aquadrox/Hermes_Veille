"""veille-guard — garde-fou des veilles contre l'injection de prompt (règles : veille_core/guard.py).

Plugin livré dans l'image (/opt/hermes/plugins, lecture seule) et activé par l'amorçage. Il refuse,
AVANT exécution, tout appel d'outil d'une exécution planifiée hors des listes blanches de sa veille,
et encadre le contenu externe rendu à l'agent comme non fiable.

Hermes laisse passer l'appel quand un hook lève une exception : chaque erreur du garde-fou (y compris
un échec d'import) refuse donc l'appel d'une exécution planifiée, au lieu de le laisser passer.
"""
from __future__ import annotations

import os
import sys

_APP = os.environ.get("VEILLE_APP", "/opt/veille-app")
if _APP not in sys.path:
    sys.path.append(_APP)  # après les modules de Hermes : n'en masque aucun

try:
    from veille_core import guard
except Exception as exc:  # noqa: BLE001
    guard = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def _cron(task_id: str, session_id: str) -> bool:
    """Exécution planifiée selon Hermes : tâche `cron:…`, session `cron_…`, ou ContextVar posée par le
    planificateur pour tout le run (l'identifiant de session change quand le contexte est compressé)."""
    if task_id.startswith("cron:") or session_id.startswith("cron_"):
        return True
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_CRON_SESSION") == "1"
    except Exception:  # noqa: BLE001 - Hermes sans ce module : les identifiants ci-dessus suffisent
        return False


def _session(task_id, session_id):
    task_id, session_id = task_id or "", session_id or ""
    return guard.session(task_id, session_id, cron=_cron(task_id, session_id))


def _on_pre_tool_call(tool_name: str = "", args=None, task_id: str = "", session_id: str = "", **_):
    try:
        if guard is None:
            reason = f"garde-fou indisponible ({_IMPORT_ERROR})" if _cron(task_id or "", session_id or "") else None
        else:
            reason = guard.enforce(tool_name, args if isinstance(args, dict) else {}, _session(task_id, session_id))
    except Exception as exc:  # noqa: BLE001
        reason = f"garde-fou en erreur ({type(exc).__name__}) : appel refusé"
    return {"action": "block", "message": f"veille-guard : {reason}"} if reason else None


def _on_transform_tool_result(tool_name: str = "", args=None, result=None, task_id: str = "",
                              session_id: str = "", **_):
    try:
        if guard is None:
            return None
        return guard.mark(tool_name, args if isinstance(args, dict) else {}, result, _session(task_id, session_id))
    except Exception:  # noqa: BLE001 - marquage seulement : le résultat reste inchangé
        return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
