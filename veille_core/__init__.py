"""Cœur multi-veilles : stockage, planification, rendu du prompt, pont Hermes.

Utilisé par la CLI `veillectl` et par l'interface web. Seules les opérations de
`service` sont destinées aux appelants : elles orchestrent `store` (fichiers du
volume) et `hermes` (jobs cron), en gardant les deux cohérents.
"""
