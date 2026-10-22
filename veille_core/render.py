"""Rendu du prompt d'une veille depuis le gabarit livré avec l'image.

Le job Hermes ne contient qu'un prompt COURT et stable qui renvoie vers le fichier
rendu : mettre à jour le gabarit (nouvelle image) ou la veille (interface) prend
effet au run suivant, sans recréer le job.
"""
from __future__ import annotations

from string import Template

from . import guard, paths, schedule
from .store import Veille, atomic_write

TEMPLATE_NAME = "job-prompt.tmpl.md"
TEMPLATES = {"veille": TEMPLATE_NAME, "composants": "job-prompt-composants.tmpl.md"}


def short_prompt(v: Veille) -> str:
    return (
        f"Veille « {v.name} ». Lis le fichier {v.dir}/job-prompt.md et exécute "
        "STRICTEMENT et intégralement les instructions qu'il contient. "
        "Tu travailles en autonomie, sans poser de question."
    )


def variables(v: Veille) -> dict[str, str]:
    return {
        "name": v.name,
        "slug": v.slug,
        "subject": v.subject or v.name,
        "dir": str(v.dir),
        "schedule_human": schedule.describe(v.schedule),
        "period_desc": schedule.PERIODS.get(v.period, schedule.PERIODS["custom"]),
        "batch_size": str(v.batch_size),
    }


def _quote_safe(value: str) -> str:
    # Valeurs de l'utilisateur (nom, sujet) : ni guillemet, ni substitution, sur une ligne.
    return value.replace('"', "'").replace("`", "'").replace("$", "").replace("\\", "/").replace("\n", " ")


def render(v: Veille) -> str:
    tpl_path = paths.defaults_dir() / TEMPLATES.get(v.kind, TEMPLATE_NAME)
    tpl = Template(tpl_path.read_text(encoding="utf-8"))
    values = {k: _quote_safe(val) for k, val in variables(v).items()}
    # Règles et contrat des outils multilignes, construits à partir du chemin du volume (slug validé par SLUG_RE).
    return tpl.substitute(values, security=guard.prompt_rules(v.dir), tools=guard.prompt_tools(v.kind, v.dir, v.slug),
                          command_timeout=guard.COMMAND_TIMEOUT)


def write(v: Veille) -> bool:
    """Rend et écrit job-prompt.md ; renvoie True si le contenu a changé."""
    text = render(v)
    path = v.dir / "job-prompt.md"
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except FileNotFoundError:
        pass
    atomic_write(path, text)
    return True
