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


def delegated_step(v: Veille) -> str:
    """Étape déléguée à Claude (option claude_research), placée avant les étapes que l'agent ne fait plus qu'en
    repli ; vide sans l'option (le gabarit est alors le même qu'avant elle)."""
    if not v.claude_research:
        return ""
    return delegated_components(v) if v.kind == "composants" else delegated_report(v)


def _call(v: Veille, output: str) -> str:
    return (f'   {{"command": "veillectl research claude {v.slug} {v.dir}/rapports/{output}", '
            f'"timeout": {guard.COMMAND_TIMEOUT}}}')


_WAIT = ("   Tant que la sortie commence par « EN COURS », relance EXACTEMENT le même appel, sans rien faire d'autre "
         "entre deux appels (la sortie dit l'avancement ; la commande déclare elle-même l'échec si la recherche "
         "n'aboutit pas).\n")


def delegated_report(v: Veille) -> str:
    """Veille thématique : Hermes orchestre, Claude recherche ET rédige ; les étapes 1 à 7 ne servent qu'en repli."""
    return (
        "0. RECHERCHE ET RÉDACTION DÉLÉGUÉES À CLAUDE (option de cette veille) : Claude fait la recherche web ET "
        "rédige le rapport (D-veille.md, D-charts.json, D-mail.json) à partir du brief et du rapport précédent. Tu "
        "n'as rien à chercher ni à rédiger.\n"
        "   a. Obtiens D (voir RÉPERTOIRES), puis lance, D remplacé par sa valeur, l'appel exact de l'outil "
        f"terminal :\n{_call(v, 'D-veille.md')}\n"
        "   La rédaction dure de plusieurs minutes à une demi-heure.\n" + _WAIT +
        "   b. « OK: » : les trois fichiers sont écrits. Ne fais PAS les étapes 1 à 7 (ne lis ni n'écris rien) : "
        "passe directement à l'étape 8 (PUBLIE). Si elle signale un fichier invalide, corrige-le selon son message "
        "(patch), puis relance-la. Pour l'étape 9, sers-toi des messages clés donnés par la sortie.\n"
        "   c. « ÉCHEC » (pont injoignable, recherche ou rédaction en échec) : fais toi-même les étapes 1 à 9.\n\n"
    )


def delegated_components(v: Veille) -> str:
    """Veille composants : Hermes orchestre, Claude fait la recherche ET l'analyse du lot, et sa réponse devient
    le fichier D-pcn.json ; les étapes 2 à 4 (recherche par l'agent) ne servent plus qu'en repli."""
    return (
        "1 bis. RECHERCHE ET ANALYSE DÉLÉGUÉES À CLAUDE (option de cette veille) : Claude fait la recherche web ET "
        "l'analyse du lot de l'étape 1 (avis, statuts, impact et urgence, remplaçants, synthèse), et écrit lui-même "
        "le fichier de sortie. Tu n'as rien à lire, chercher ni rédiger.\n"
        f"   a. Lance, D remplacé par sa valeur, l'appel exact de l'outil terminal :\n{_call(v, 'D-pcn.json')}\n"
        "   La recherche dure de plusieurs minutes à plus d'une heure (grand lot : sous-lots enchaînés).\n" + _WAIT +
        "   b. « OK: » : le fichier D-pcn.json est écrit. Ne fais PAS les étapes 2, 3 et 4 (n'écris ni ne modifie ce "
        "fichier) : passe directement à l'étape 5.\n"
        "   c. « ÉCHEC » (pont injoignable, recherche en échec) : fais toi-même les étapes 2, 3 et 4, puis la 5.\n\n"
    )


def _quote_safe(value: str) -> str:
    # Valeurs de l'utilisateur (nom, sujet) : ni guillemet, ni substitution, sur une ligne.
    return value.replace('"', "'").replace("`", "'").replace("$", "").replace("\\", "/").replace("\n", " ")


def render(v: Veille) -> str:
    tpl_path = paths.defaults_dir() / TEMPLATES.get(v.kind, TEMPLATE_NAME)
    tpl = Template(tpl_path.read_text(encoding="utf-8"))
    values = {k: _quote_safe(val) for k, val in variables(v).items()}
    # Règles et contrat des outils multilignes, construits à partir du chemin du volume (slug validé par SLUG_RE).
    return tpl.substitute(values, security=guard.prompt_rules(v.dir),
                          tools=guard.prompt_tools(v.kind, v.dir, v.slug, v.claude_research),
                          delegated=delegated_step(v), command_timeout=guard.COMMAND_TIMEOUT)


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
