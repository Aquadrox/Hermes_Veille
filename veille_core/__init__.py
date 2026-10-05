"""Cœur multi-veilles : stockage, planification, rendu du prompt, pont Hermes.

Utilisé par la CLI `veillectl` et par l'interface web. Seules les opérations de
`service` sont destinées aux appelants : elles orchestrent `store` (fichiers du
volume) et `hermes` (jobs cron), en gardant les deux cohérents.
"""

from pathlib import Path as _Path


def version() -> str:
    """Version de l'application (fichier VERSION à la racine du dépôt / de /opt/veille-app)."""
    try:
        return (_Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0+inconnue"
