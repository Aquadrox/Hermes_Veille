"""Serveur MCP des veilles : piloter l'application depuis Hermes (Telegram, CLI…).

Transport stdio, sans port réseau : Hermes lance `veille-mcp` (dans le conteneur,
ou via `docker exec -i` depuis l'hôte). Toute la logique passe par
`veille_core.service`, comme l'interface web et `veillectl`.
"""
