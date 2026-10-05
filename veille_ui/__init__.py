"""Interface web de gestion des veilles (FastAPI + Jinja + HTMX).

Accès prévu : tunnel SSH vers 127.0.0.1 uniquement. Défense en profondeur malgré
tout : mot de passe (argon2), session signée, jeton CSRF sur chaque POST,
vérification de l'en-tête Host, CSP stricte.
"""
