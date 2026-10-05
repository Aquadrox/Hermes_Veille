"""Génère les valeurs VEILLE_UI_PASSWORD_HASH et VEILLE_UI_SECRET à coller dans .env.

    python -m veille_ui.passwd          (ou : make ui-password)
"""
import getpass
import secrets
import sys

from .auth import hash_password

MIN_LENGTH = 12


def main() -> int:
    pw = getpass.getpass("Mot de passe de l'interface : ")
    if len(pw) < MIN_LENGTH:
        print(f"✗ {MIN_LENGTH} caractères minimum", file=sys.stderr)
        return 1
    if getpass.getpass("Confirmation : ") != pw:
        print("✗ les deux saisies diffèrent", file=sys.stderr)
        return 1
    print("\nÀ ajouter dans .env (guillemets simples compris : le hash contient des $) :\n")
    print(f"VEILLE_UI_PASSWORD_HASH='{hash_password(pw)}'")
    print(f"VEILLE_UI_SECRET='{secrets.token_hex(32)}'")
    print("\nPuis : docker compose up -d   (recrée le conteneur avec les nouvelles valeurs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
