"""veille-guard — garde-fou des veilles contre l'injection de prompt (règles : veille_core/guard.py).

Plugin livré dans l'image (/opt/hermes/plugins, lecture seule) et activé par l'amorçage. Il refuse,
AVANT exécution, tout appel d'outil d'une exécution planifiée hors des listes blanches de sa veille,
encadre le contenu externe rendu à l'agent comme non fiable, et déclare en échec à Hermes l'exécution
arrêtée par ses refus ou terminée sans rapport publié.

Hermes laisse passer l'appel quand un hook lève une exception : chaque erreur du garde-fou (y compris
un échec d'import) refuse donc l'appel d'une exécution planifiée, au lieu de le laisser passer.
"""
from __future__ import annotations

import os
import sys

_APP = os.environ.get("VEILLE_APP", "/opt/veille-app")
_CRON_FAILURE = "[CRON_FAILURE]"  # 1re ligne seule de la réponse finale : Hermes (>= v0.22) enregistre un échec
_ANSWER_MAX = 1000  # réponse de l'agent jointe au motif (last_error, lu aussi par le MCP) : bornée
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


def _on_pre_tool_call(tool_name: str = "", args=None, task_id: str = "", session_id: str = "", turn_id: str = "",
                      **_):
    try:
        if guard is None:
            reason = f"garde-fou indisponible ({_IMPORT_ERROR})" if _cron(task_id or "", session_id or "") else None
        else:
            # Exécution planifiée = un seul tour Hermes : son identifiant compte les refus de l'exécution.
            reason = guard.enforce(tool_name, args if isinstance(args, dict) else {}, _session(task_id, session_id),
                                   turn_id or "")
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


def _on_transform_llm_output(response_text: str = "", session_id: str = "", turn_id: str = "", **_):
    """Réponse finale d'une exécution planifiée qui n'a pas publié son rapport, ou que le garde-fou a arrêtée :
    « [CRON_FAILURE] » en 1re ligne, Hermes l'enregistre en échec (last_status « error », last_error : la
    suite). None : réponse inchangée. Une erreur du garde-fou déclare l'échec plutôt qu'un faux succès."""
    try:
        if guard is None:
            reason = f"garde-fou indisponible ({_IMPORT_ERROR})" if _cron("", session_id or "") else None
        else:
            reason = guard.finish(turn_id or "", _session("", session_id))
    except Exception as exc:  # noqa: BLE001
        reason = f"garde-fou en erreur ({type(exc).__name__}) : rapport non vérifié" if _cron("", session_id or "") else None
    if not reason:
        return None
    # Texte de l'agent, exposé au web : encadré comme non fiable partout où last_error est relu.
    answer = f"\n\nRéponse de l'agent :\n{guard.wrap(response_text[:_ANSWER_MAX])}" if guard else ""
    return f"{_CRON_FAILURE}\nveille-guard : {reason}{answer}"


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
    ctx.register_hook("transform_llm_output", _on_transform_llm_output)
