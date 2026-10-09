"""Solde du compte DeepSeek (`GET /user/balance`) : la seule information de facturation que
l'API expose (pas de coût par requête ni d'historique)."""
from __future__ import annotations

import json
import urllib.request

from . import envfile

BALANCE_URL = "https://api.deepseek.com/user/balance"


def balance(timeout: float = 10) -> dict:
    """{'available': bool, 'balances': [{'currency', 'total', 'granted', 'topped_up'}]} ou {'error'}."""
    key = envfile.get("DEEPSEEK_API_KEY")
    if not key:
        return {"error": "DEEPSEEK_API_KEY absente"}
    req = urllib.request.Request(BALANCE_URL, headers={"Authorization": f"Bearer {key}",
                                                       "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001 - simple affichage
        return {"error": f"API DeepSeek injoignable ({type(exc).__name__})"}
    return {
        "available": bool(data.get("is_available")),
        "balances": [{"currency": b.get("currency"), "total": b.get("total_balance"),
                      "granted": b.get("granted_balance"), "topped_up": b.get("topped_up_balance")}
                     for b in data.get("balance_infos", [])],
    }
