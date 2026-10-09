"""Rendu du prompt d'une veille depuis le gabarit livré avec l'image.

Le job Hermes ne contient qu'un prompt COURT et stable qui renvoie vers le fichier
rendu : mettre à jour le gabarit (nouvelle image) ou la veille (interface) prend
effet au run suivant, sans recréer le job.
"""
from __future__ import annotations

from string import Template

from . import paths, schedule
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
    pdf = v.pdf
    title = pdf.title or v.name
    return {
        "name": v.name,
        "slug": v.slug,
        "subject": v.subject or v.name,
        "dir": str(v.dir),
        "schedule_human": schedule.describe(v.schedule),
        "period_desc": schedule.PERIODS.get(v.period, schedule.PERIODS["custom"]),
        "pdf_title": title,
        "pdf_header": pdf.header or title,
        "pdf_footer": pdf.footer,
        "pdf_audience": pdf.audience,
        "pdf_accent": pdf.accent,
        "batch_size": str(v.batch_size),
    }


def _quote_safe(value: str) -> str:
    # Les valeurs sont injectées entre guillemets dans des lignes de commande.
    return value.replace('"', "'").replace("`", "'").replace("$", "").replace("\\", "/").replace("\n", " ")


def components_step(v: Veille) -> str:
    """Étape « chapitre composants » d'une veille qui en inclut un (vide sinon : prompt inchangé).

    Construite hors de _quote_safe (elle contient des retours à la ligne) à partir de valeurs
    sûres : slugs validés par SLUG_RE et chemin du volume.
    """
    if v.kind != "veille" or not v.components:
        return ""
    return (
        "\n\n6 bis. CHAPITRE COMPOSANTS (PCN / fins de vie) — exécute :\n"
        f"   veillectl components chapter {v.components} --for {v.slug} --append {v.dir}/rapports/D-veille.md\n"
        "   La commande ajoute elle-même le chapitre à la fin du rapport : ne le modifie pas, ne le résume pas.\n"
        "   Si elle affiche une ligne « PREFIXE_SUJET=… », place ce préfixe AU DÉBUT de l'objet de l'e-mail "
        "(étape 8) et ajoute au texte de l'e-mail une ligne sur les fins de vie détectées.\n"
        "   Si elle affiche une ligne « PIECE_JOINTE=<chemin> », ajoute `--attach <chemin>` (liste Excel des "
        "composants) à la commande d'envoi de l'étape 8, en plus du PDF."
    )


def render(v: Veille) -> str:
    tpl_path = paths.defaults_dir() / TEMPLATES.get(v.kind, TEMPLATE_NAME)
    tpl = Template(tpl_path.read_text(encoding="utf-8"))
    values = {k: _quote_safe(val) for k, val in variables(v).items()}
    return tpl.substitute(values, components_step=components_step(v))


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
