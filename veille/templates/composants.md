# Brief de recherche — Veille composants (PCN / fins de vie)

> *Ce brief guide la recherche web de l'agent. La liste des composants, le rapport, le PDF et
> l'e-mail sont gérés par l'application : ne décris ici que COMMENT chercher.*

## Objectif

Détecter au plus tôt les avis qui imposent une action d'approvisionnement ou de requalification :

| Type | Signification | Priorité |
|---|---|---|
| `EOL` / `PDN` | fin de vie, arrêt de production annoncé | critique |
| `OBSOLETE` | référence déjà obsolète | critique |
| `LTB` | date de dernière commande (Last Time Buy) | critique |
| `NRND` | déconseillé pour les nouvelles conceptions | élevée |
| `PCN` | modification (site de production, matériau, marquage, boîtier, datasheet…) | normale |

## Sources à privilégier (dans cet ordre)

1. **Pages d'avis des fabricants** (« Product Change Notifications », « Discontinued products »,
   « Obsolescence ») : document PDF de l'avis, avec son numéro.
2. **Distributeurs** : Digi-Key, Mouser, Farnell / element14, Arrow, TME, RS — fiche produit
   (statut de cycle de vie) et onglet « PCN / Notifications ».
3. Agrégateurs d'obsolescence publics, uniquement en complément d'une source 1 ou 2.

## Règles de fiabilité

- Un avis = une source ouverte et vérifiée qui cite la référence ou sa gamme. Pas d'URL devinée.
- Reprends le **numéro d'avis** du fabricant quand il existe : il sert au dédoublonnage.
- Dates au format `AAAA-MM-JJ` ; `ltb_date` = date limite de commande, `eol_date` = dernière livraison.
- Un avis de **gamme** (ex. « famille STM32F1 ») liste toutes les références suivies concernées.
- Un PCN purement administratif (changement d'adresse, de logo) est classé `AUTRE`.

## Mots-clés utiles

PCN, PDN, EOL, LTB, NRND, « end of life », « last time buy », « discontinued », « obsolete »,
« product change notice », « mature », « not recommended for new designs ».
