# veille-hermes

Veilles technologiques **conteneurisées** et pilotables depuis une **interface web privée**.

Chaque veille a son sujet, son brief de mission, sa fréquence et ses destinataires. À chaque
échéance, un agent recherche les actualités de la période écoulée et rédige un rapport structuré
(badges de maturité/confiance, sources numérotées, graphiques, encadrés d'analyse) ; l'application
le convertit en **PDF stylé** et l'**envoie par e-mail** (destinataires en copie cachée). L'agent
cherche et lit le web par **DeepSeek Search & Fetch** (voir plus bas) et reste tenu par un **garde-fou
contre l'injection de prompt**.

La veille d'origine, **Industrie 4.0** (chaque lundi à 6h30, en heures creuses DeepSeek), est créée
ou migrée automatiquement.

## Pourquoi un conteneur ?

Une veille n'est pas un simple script : c'est un **job cron interne à
[Hermes Agent](https://github.com/NousResearch/hermes-agent)**, exécuté par le scheduler de son
gateway. L'image **dérive de l'image officielle** `nousresearch/hermes-agent` (version épinglée),
ce qui apporte la supervision s6, un volume de données unique (`/opt/data`) et des mises à jour
par simple reconstruction.

## Démarrage rapide

```bash
# 1. configurer les secrets
cp .env.example .env && chmod 600 .env && $EDITOR .env
#    DEEPSEEK_API_KEY, BREVO_API_KEY, BREVO_SENDER_*, VEILLE_RECIPIENTS
#    (adresse d'alerte du chien de garde : ensuite, dans l'interface, page « Paramètres »)
#    VEILLE_DATA_DIR : dossier des données, HORS du dépôt (défaut proposé : /srv/veille-hermes/data)

# 2. premier déploiement : crée le dossier de données, build, démarrage, vérifications
./scripts/deploy.sh --init

# 3. activer l'interface web : générer le mot de passe, coller les 2 lignes dans .env
make ui-password && docker compose up -d

# 4. déclencher un rapport immédiatement (test de bout en bout)
make veilles && make run-now VEILLE=industrie-4-0
```

À chaque démarrage, le conteneur **s'amorce et se réconcilie tout seul** : migration de
l'ancienne disposition, création de la veille par défaut sur une installation neuve (en pause
si `VEILLE_RECIPIENTS` est vide), puis réalignement de chaque job sur sa veille.

## Interface web

L'interface (FastAPI + HTMX) n'est publiée **que sur `127.0.0.1:8088` de l'hôte** : elle n'est
jamais exposée au réseau. On y accède par un tunnel SSH depuis son poste :

```bash
ssh -N -L 8088:127.0.0.1:8088 utilisateur@serveur      # puis http://localhost:8088
```

ou durablement dans `~/.ssh/config` :

```
Host veille
    HostName serveur.exemple
    User utilisateur
    LocalForward 8088 127.0.0.1:8088
```

Fonctions :

- **tableau de bord** : état de chaque veille (active, en pause, en échec), prochain et dernier
  run, dernier rapport et nombre de rapports (lien vers l'historique). Pause, reprise et
  lancement immédiat en un clic ; avancement d'une analyse composants en cours (étape, composant) ;
- **créer / modifier une veille** : nom, sujet, fréquence (quotidienne, jours ouvrés,
  hebdomadaire, mensuelle ou expression cron, avec aperçu des prochaines exécutions),
  destinataires (puces, autocomplétion sur le **carnet d'adresses**, ajout au toucher ou par
  glisser-déposer, y compris au doigt dans la PWA), brief de mission (à partir d'un modèle, avec aperçu),
  mise en forme du PDF, modèle LLM ;
- **détail** : historique complet des rapports (PDF et Markdown, en tête de page), journal du
  lancement en cours (rafraîchi en direct ; veille composants : avancement étape par étape), coût des exécutions,
  historique d'exécution Hermes, e-mail de test, **archivage** (rien n'est effacé) ;
- **veille composants** : **liste unique de tous les composants**, une ligne chacun (types d'avis en
  badges, fins de vie en rouge et en tête, statut, échéance, lien vers la source), avec **recherche en temps
  réel** (mots-clés sur MPN, fabricant, statut, intitulés et résumés des avis) et un
  **filtre par colonne** (texte, ou liste pour le statut et le type d'avis ; statuts Digi-Key et web sous un
  même libellé, « Obsolète »…) ; un clic sur un composant déplie
  son détail (avis complets, sources, état Digi-Key). Même présentation compacte dans le PDF (tableau puis « Détail par composant », MPN
  cliquable) et l'e-mail ; boutons « Lancer l'analyse »
  (complète) et « Vérifier les statuts Digi-Key » (immédiat, sans LLM ni e-mail) — voir plus bas ;
- **composants** (base des composants) : **tout ce qui a déjà été analysé**, toutes veilles composants
  confondues (actives et archivées), une ligne par référence : statut le plus récent, avis (chacun une fois,
  à sa première détection), veilles qui l'ont analysée. Même tableau que la liste d'une veille (recherche,
  filtres, détail au toucher), plus un filtre **par veille** ; export Excel avec la colonne « Veilles » ;
- **sources** : clés d'API des distributeurs (Digi-Key), saisie masquée et test de connexion ;
- **paramètres** : adresse(s) des alertes du chien de garde, avec e-mail de test ; carnet d'adresses
  (« Oublier » une adresse) ;
- **santé** : état des veilles, crédits Brevo, contenu de la base des composants et journal d'audit.

Sécurité, en plus du tunnel : un mot de passe administrateur (hash argon2 dans `.env`), une
session signée valable 12 h, un jeton CSRF sur chaque action, une limite de tentatives de
connexion, la vérification de l'en-tête `Host` (contre le *DNS rebinding*), une CSP stricte
(aucun script en ligne, HTMX embarqué) et des noms de fichiers en liste blanche. Sans
`VEILLE_UI_PASSWORD_HASH`, l'interface reste **désactivée**.

## Veille composants (PCN et fins de vie)

Une **veille composants** surveille une liste de références électroniques et détecte les avis
publiés par les fabricants ou repris par les distributeurs : **fin de vie** (EOL, PDN, obsolète),
**dernière commande** (LTB), **NRND** et **modifications** (PCN). Les fins de vie sont mises en
évidence partout : bandeau rouge dans l'interface, encadré rouge dans le PDF, objet d'e-mail
préfixé « [EOL ×N] ».

```
dimanche 23h — job de la veille composants
  1. veillectl components scan <slug>      statut Digi-Key de TOUTES les références (déterministe),
                                           par tranches de < 6 min relancées jusqu'à "complete": true
                                           + lot du jour pour la recherche web (40 par défaut)
  2. agent : recherche web (pages PCN des fabricants, Mouser, Digi-Key, Farnell…) -> D-pcn.json
  3. veillectl components finalize …       validation, dédoublonnage, enregistrement, rapport PDF,
                                           liste Excel
lundi 6h30 — job de la veille thématique rattachée
  8. veillectl report publish …            chapitre « Composants — PCN & fins de vie » ajouté au
                                           rapport, PDF, e-mail avec la liste Excel jointe
```

- **Liste** : `MPN ; Fabricant`, une par ligne, collage depuis un tableur accepté
  (interface, `veillectl components add --file bom.csv`, ou Telegram : « Ajoute le LM317T de Texas
  Instruments à la veille composants »). Les colonnes suivantes sont ignorées : les anciennes listes
  `MPN ; Fabricant ; Réf. interne` restent valables (la réf. interne n'est plus utilisée depuis la 0.5.0).
- **Import d'une nomenclature CSV ou Excel** (`.csv`, `.xlsx`, `.xlsm`) : dans le formulaire de la
  veille, « Importer un fichier ». Les colonnes MPN et Fabricant sont reconnues par
  règles (libellés FR / EN usuels, forme des valeurs ; aucun service externe), puis **proposées** :
  feuille, ligne d'en-tête, colonnes et fabricant par défaut se corrigent avant d'appliquer, avec un
  aperçu et les lignes ignorées. « Ajouter à la liste » ou « Remplacer la liste », puis enregistrer
  la veille comme d'habitude.
  - Telegram : envoie le fichier à Hermes. Il le dépose dans `<données>/imports/`, te montre la
    correspondance (`preview_component_import`) et importe après ton accord (`import_components`).
  - CLI : `veillectl components import <slug> bom.xlsx --dry-run`, puis sans `--dry-run`
    (`--replace`, `--mpn COL`, `--manufacturer COL`, `--default-manufacturer X`…).
- **Incrémental** : chaque référence garde la date de sa dernière vérification (Digi-Key et web) ;
  la recherche web ne remonte qu'à cette date (moins 30 jours de marge) et un avis déjà connu
  n'est jamais signalé à nouveau. Avis et dates sont conservés dans `components.json` et
  `notices.json`, avec leurs liens sources.
- **Statut Digi-Key** : une recherche par mot-clé par référence (la fiche exacte de l'API exige un
  Account-Id en OAuth « 2-legged » et ne reconnaît ni une référence de plusieurs fabricants, ni une
  référence saisie sans sa ponctuation, « 2N7002215 » pour « 2N7002,215 »). La fiche retenue est celle
  du fabricant suivi, au MPN identique de préférence, sinon à la ponctuation près (le « + » d'une
  version sans plomb et le point décimal restent significatifs).
- **Référence introuvable chez Digi-Key** (référence de base comme ESP32-WROOM-32E, fabricant non
  distribué) : elle passe une fois **en priorité** dans le lot de recherche web, et l'agent trouve
  lui-même sa fiche (fabricant, Mouser, Farnell, LCSC…) pour établir son **statut web** (actif, NRND,
  dernière commande, obsolète, ou introuvable), avec la fiche en source. Ce statut s'affiche dans le
  détail du composant et dans l'export Excel (colonne « Statut web ») ; NRND, dernière commande et
  obsolète créent l'avis correspondant. Il est revu à chaque passage de la référence dans le lot et
  effacé si Digi-Key la retrouve.
- **Repli alldatasheet** : toujours introuvable (ou restée « introuvable » au passage précédent), l'agent
  ouvre la recherche alldatasheet de la référence (fournie par le scan), retient la fiche du bon fabricant
  et son **« Direct Link »**, puis cherche la fiche produit et les avis PCN / EOL **sur le site du
  fabricant**. alldatasheet n'est qu'un annuaire : il ne fait jamais foi (un statut ou un avis dont la seule
  source est alldatasheet est rejeté). La page consultée et le site du fabricant sont gardés (détail du
  composant, Excel « Statut web (alldatasheet) », `list_components`) et resservent au passage suivant.
  Protégé par Cloudflare et sans API publique, le site n'est consulté que par l'agent, jamais par un robot
  de l'application.
- **Base des composants** (`veilles/.veille.db`) : ce que les veilles savent de chaque référence (statuts
  Digi-Key et web, dernière recherche web, avis) est partagé. Une référence ajoutée à une veille (création,
  ajout, import ; interface, CLI ou Telegram) **reprend les avis déjà connus** des autres veilles, actives ou
  archivées : ils partent avec son premier e-mail. Elle se consulte dans l'interface, page **Composants**. Ses statuts et sa date de recherche web sont repris
  aussi, ce qui évite de la rechercher à nouveau sur le web en priorité (économie de tokens). La base n'est
  qu'un **index** des fichiers des veilles (qui font foi) : reconstruite à chaque démarrage
  (`veillectl catalog rebuild`), elle retire ce qu'une veille réinitialisée avait apporté (ce que d'autres
  veilles en ont déjà repris leur reste) et garde ce qu'une veille archivée savait. Un échec de la base n'interrompt jamais une analyse (tracé dans l'audit).
- **Volume** : le statut Digi-Key couvre toutes les références à chaque exécution (≈ 0,6 à 2,6 s par
  référence, 900 au plus, 15 min de budget par passage) ; la recherche web, coûteuse en tokens, tourne par
  **lots** (`batch_size`). Avec 300 références et des lots de 40, chacune est recherchée sur le web toutes les
  8 semaines environ, et **en priorité** dès que son statut Digi-Key change ou que Digi-Key ne la
  trouve plus.
- **Panne passagère de Digi-Key** (connexion coupée, délai dépassé, HTTP 5xx, HTTP 429) : le passage
  attend puis **reprend à la référence interrompue**, sans réinterroger les précédentes. Les attentes
  sont de 5, 10, 20, 30 et 45 s, puis 60 s au plus (± 20 %) ; le Retry-After d'un 429 est respecté. Au
  10e échec d'un même passage (toutes références confondues), la phase est abandonnée et les références
  restantes partent au lot de recherche web, sans être marquées vérifiées. Elles reprennent au passage
  suivant. Une erreur permanente (HTTP 400, 403, jeton refusé) arrête la phase sans nouvel essai. Le
  budget de temps (15 min) et le plafond d'appels gardent la priorité. Chaque tentative est journalisée
  (`tentative n/10`, erreur, attente). L'abandon, ou des incidents surmontés, sont signalés dans
  l'avancement, l'audit et la sortie du scan.
- **Passage par tranches** (0.7.2) : Hermes abandonne tout appel d'outil au bout de 7 min (420 s) ; chaque
  appel de `veillectl components scan` traite donc au plus ≈ 4 min de Digi-Key, **enregistre** ce qu'il a lu
  et rend `"complete": false` ; l'agent relance le même appel, qui reprend aux références non encore
  vérifiées par ce passage (repris s'il a avancé depuis moins d'une heure, même à cheval sur minuit). Budget,
  plafond d'appels et pannes tolérées valent pour le passage entier ; le lot web n'est tiré qu'à sa fin. Un passage **partiel** (budget atteint, Digi-Key en panne, exécution terminée avant sa fin) est
  publié et **dit comme tel** : encadré « Passage Digi-Key PARTIEL : k référence(s) sur n » dans le PDF et
  l'e-mail, « Digi-Key : k/n (PARTIEL) » en en-tête ; les autres passent en tête du passage suivant.
- **À la demande** (interface ou Telegram) : « Lancer l'analyse » / `run_veille_now` exécute tout de
  suite l'analyse complète ; « Vérifier les statuts Digi-Key » / `check_component_statuses` (ou
  `veillectl components scan <slug> --status-only`) relit en arrière-plan le statut de toutes les
  références, sans LLM, sans coût en tokens et sans e-mail — utile juste après un import de
  nomenclature. Le lot de la prochaine exécution planifiée n'est pas modifié.
- **Réinitialiser la recherche** (page de la veille, carte « Réinitialiser la recherche », avec
  confirmation ; `veillectl components reset <slug>` ; Telegram : `reset_component_search`, qui exige
  de répéter le slug) : avis détectés, statuts Digi-Key et web et dates de recherche sont effacés ;
  la prochaine analyse repart de zéro et re-signale comme nouveaux les avis retrouvés. La liste des
  composants, les rapports et les destinataires restent ; une copie de `components.json` et
  `notices.json` est gardée dans `veilles/.archive/<slug>-reinit-…/`. Refusé pendant un lancement
  manuel.
- **Avancement en direct** de ces lancements manuels : étape 1/3 « Statut Digi-Key » (référence en
  cours, `124 / 310`), 2/3 « Recherche web » (une pastille par composant du lot : à rechercher, en
  cours, fait), 3/3 « Rapport et envoi » ; à la fin, les nouveaux avis par composant, ou l'étape où le
  lancement s'est arrêté. Page de la veille (rafraîchi toutes les 5 s), tableau de bord (10 s) et
  Telegram (« Où en est l'analyse de la veille composants ? », outil `get_run_log`). Pendant la
  recherche web, c'est l'agent qui signale chaque groupe de recherches
  (`veillectl components progress`) : au mieux de ce qu'il fait, et seulement lors d'un lancement
  manuel — une exécution planifiée n'écrit rien et ne coûte aucun appel de plus.
- **Fiche composant et alternatives** (0.8.0) : le passage Digi-Key garde la **fiche** de chaque référence
  (catégorie et caractéristiques : valeur, tolérance, boîtier…), affichée dans le détail du composant. Pour
  un **passif ou un discret** (résistance, condensateur, inductance, ferrite, diode, transistor, quartz) en
  **fin de vie ou en dernière commande** (EOL, PDN, obsolète, LTB), l'application cherche ensuite chez
  Digi-Key des références **actives** de la même sous-catégorie, aux mêmes **caractéristiques critiques**
  (recherche paramétrique, sans LLM ni coût en tokens), et en garde 5 au plus, classées par score (part
  des autres caractéristiques identiques) avec leurs différences. Les circuits intégrés ne sont pas
  concernés : un équivalent paramétrique y serait trompeur.
  - **Critiques** : par défaut, celles de la famille (valeur nominale, boîtier, montage, tension d'un
    condensateur, Vds et Id d'un MOSFET…). Dans la fiche du composant (« Fiche composant »), coche celles
    que toute alternative doit avoir à l'identique : ton choix remplace les défauts (« Revenir aux
    défauts » pour l'annuler) et survit à une réinitialisation de la recherche. Une recherche faite avec
    d'autres critères est signalée jusqu'à la suivante.
  - **Quand** : après chaque passage Digi-Key, pour les fins de vie sans recherche depuis 30 jours (ou
    recherchées avec d'autres critères), dans le temps restant de la tranche et 50 recherches au plus
    (quota de l'offre gratuite). **À la demande** : « Rechercher des alternatives » dans le détail d'un
    composant (même actif), « Alternatives des fins de vie » au-dessus de la liste (en arrière-plan,
    avancement affiché), `veillectl components alternatives <slug> [MPN …]`, ou Telegram
    (`search_alternatives`).
  - **Consulter** : badge « ⇄ n » et filtre « Avec alternatives » de la liste, détail du composant, base
    des composants (lecture seule), Telegram (`get_component_sheet`, `list_alternatives`,
    `set_critical_parameters`).
  - Au moins une critique est exigée (sinon toute référence de la catégorie passerait). Une recherche refusée
    par Digi-Key est affichée dans le détail du composant et refaite après 30 jours (ou à la demande).
  - Ce sont des **suggestions** à qualifier : correspondance des caractéristiques publiées par Digi-Key,
    pas une équivalence validée (brochage, fiabilité, qualification).
- **Livraison** : rattachée à une veille thématique (« Chapitre composants » dans son formulaire),
  la veille composants n'envoie rien elle-même : ses nouveaux avis partent en chapitre de l'e-mail
  de cette veille. Sinon, elle envoie son propre e-mail (HTML lisible sur mobile + PDF).
  Dans les deux cas, la **liste complète des composants au format Excel** est jointe à l'e-mail
  (dernier export conservé à côté du rapport, dans `rapports/` ; s'il échoue, l'e-mail part sans lui).
- **Export Excel** : bouton « Exporter (Excel) » de la liste des composants. Une ligne par
  référence (y compris sans avis, et celles retirées qui ont des avis), fins de vie et dernières
  commandes en tête et en rouge, filtre automatique : statut Digi-Key, statut web, avis, échéance,
  dates de vérification et lien de l'avis le plus critique (sinon de la fiche Digi-Key ou web). Les textes venus du web restent du texte (jamais
  une formule Excel).
- **Digi-Key** (facultatif mais recommandé) : créer une application « Production » sur
  [developer.digikey.com](https://developer.digikey.com) avec l'API *Product Information v4*, puis
  saisir son Client ID et son Client Secret dans l'interface, page **Sources** (bouton « Tester la
  connexion »). Sans ces clés, seule la recherche web est utilisée (le rapport le signale).
  - Les clés saisies ne sont **jamais réaffichées** (aperçu masqué `••••1234`). Elles sont stockées
    dans `veilles/.credentials.json` (droits 0600), **exclues des sauvegardes** (à ressaisir après
    une restauration), et chaque modification est tracée dans le journal d'audit, sans les valeurs.
  - Variante « infrastructure » : `DIGIKEY_CLIENT_ID` / `DIGIKEY_CLIENT_SECRET` dans `.env`. Elles
    sont alors prioritaires et affichées en lecture seule dans l'interface.
  - Volontairement, aucune clé ne passe par Telegram ou le MCP, qui ne fait qu'indiquer si une source
    est configurée.

Limites : la recherche web reste probabiliste (un avis peut être manqué : le statut Digi-Key sert
de filet) ; les avis PCN de Digi-Key eux-mêmes ne sont pas lus (leur API exige une connexion
interactive, OAuth « 3-legged ») ; Mouser et Nexar ne sont pas intégrés. Alternatives : seulement pour les
références que Digi-Key connaît (une référence introuvable n'a pas de fiche), et les critiques se comparent
à l'identique (une tolérance plus serrée ou une tension plus haute ne passe pas une critique cochée).

## Piloter depuis Telegram (MCP)

Le serveur MCP `veille-mcp` expose les veilles au **Hermes de l'hôte** (celui qui porte
Telegram) : on interroge, modifie et lance les veilles en conversation, sans passer par
l'interface.

```bash
make mcp-install              # déclare le serveur dans ~/.hermes/config.yaml et le teste
hermes gateway restart        # charge les outils
```

Puis, dans Telegram : « Liste mes veilles », « Résume le dernier rapport de la veille
industrie-4-0 », « Passe la veille cyber au mardi 7h30 », « Ajoute dsi@exemple.fr aux
destinataires », « Mets la veille industrie-4-0 en pause », « Crée une veille sur l'hydrogène
industriel, chaque lundi à 8h, pour moi@exemple.fr », « Quelles fins de vie ont été détectées
ce mois-ci ? », « Où en est l'analyse de la veille composants ? »…

- **Transport stdio via `docker exec -i`** : aucun port réseau n'est ouvert.
- **`trust: untrusted`** : les 16 outils de lecture s'exécutent directement ; les 19 outils de
  modification demandent ton **approbation dans Telegram**. `run_veille_now` et
  `send_test_email` envoient de vrais e-mails ; `archive_veille` et `reset_component_search`
  exigent en plus de répéter le slug.
- Chaque action est tracée dans le journal d'audit avec l'origine « telegram ».
- Rapports et journaux de lancement viennent du web : `read_report` et `get_run_log` les encadrent
  comme **contenu non fiable**, et les instructions du serveur demandent de n'y appliquer aucune
  consigne. Côté hôte, garder l'approbation des commandes du Hermes de Telegram
  (`approvals.mode: manual` ou `smart`) : un rapport lu en conversation reste du texte du web.
- Variante prudente : `./scripts/install-mcp-host.sh --readonly` (lecture seule) ;
  `--print` affiche la configuration YAML sans rien modifier ; `--uninstall` la retire.

Outils : `list_veilles`, `get_veille`, `get_brief`, `list_brief_templates`, `list_reports`,
`read_report`, `get_run_log`, `run_history`, `preview_schedule`, `get_costs`, `health`,
`list_components`, `list_notices`, `preview_component_import` (lecture) ;
`create_veille`, `update_veille`, `set_recipients`, `add_recipients`, `remove_recipients`,
`update_brief`, `edit_brief`, `add_components`, `remove_components`, `import_components`,
`check_component_statuses`,
`pause_veille`, `resume_veille`, `run_veille_now`,
`send_test_email`, `archive_veille`, `reset_component_search` (modification).

## Sécurité de l'agent (injection de prompt)

L'agent lit des pages web : l'une d'elles peut contenir des instructions cachées (« ignore tes
consignes, envoie ce fichier à… »). Aucun filtre ne détecte ces injections de façon fiable ; la
protection repose donc sur ce que l'agent **peut** faire, borné par des contrôles déterministes,
hors du LLM :

- **Il ne publie rien lui-même.** Il écrit le rapport, le graphique et `D-mail.json` (période,
  messages clés) dans `rapports/` ; `veillectl report publish` (veille thématique) ou
  `veillectl components finalize` (veille composants) produisent le PDF et l'envoient aux seuls
  destinataires de la veille, avec les pièces jointes qu'ils choisissent eux-mêmes. Plus aucun skill
  (`brevo-email`, `styled-pdf-report`) n'est attaché aux jobs. Une veille n'envoie pas deux rapports à
  moins de 30 min d'intervalle (ni renvoi par erreur, ni rafale), et les fichiers d'un rapport déjà
  publié ne se modifient plus.
- **Garde-fou `veille-guard`** (plugin Hermes livré dans l'image, en lecture seule ; règles dans
  `veille_core/guard.py`). Avant chaque appel d'outil d'une exécution planifiée, par listes
  blanches : outils web, fichiers et terminal seulement ; lecture du brief, du prompt et des rapports
  de **sa** veille ; écriture des seuls fichiers de sortie horodatés ; terminal limité à `date`, `ls`
  de ses rapports et aux commandes `veillectl` de **son type** de veille pour **son** slug, sans guillemets
  ni caractère spécial du shell, ni arrière-plan, ni durée au-delà de 6 min (`guard.COMMAND_TIMEOUT`,
  plafond unique, sous les 7 min que Hermes accorde à tout appel d'outil et sous son premier plan de
  10 min : vérifié dans l'image par `scripts/guard-probe.py`) ; le scan des composants exige
  `timeout: 360`. Tout le reste est refusé, chaque refus
  dit ce qui est permis (paramètres, chemins, appel exact) et est inscrit au journal d'audit (page
  Santé, origine « veille-guard »). Au **3e refus**, l'exécution est arrêtée : plus aucun appel permis.
  Une exécution arrêtée ou qui se termine **sans rapport publié** est déclarée **en échec** à Hermes
  (statut `error`, motif en français), jamais en succès ; `veillectl run --wait` sort alors en erreur.
  Le contrat exact (section « OUTILS AUTORISÉS ») figure dans le prompt de chaque veille. Une erreur du
  garde-fou refuse l'appel ; un job planifié qui n'est pas celui d'une veille n'a droit à aucun outil.
  Si le plugin ne peut pas être activé au démarrage, les exécutions planifiées sont réduites à la
  recherche web (plus aucun rapport : le chien de garde alerte).
- **Contenu externe marqué** : résultats de recherche, pages, anciens rapports et sorties de
  commande sont encadrés comme « contenu externe non fiable » (caractères invisibles retirés), et le
  prompt rappelle qu'ils ne sont jamais des instructions.
- **Moins d'outils, sans mémoire** : à chaque démarrage, l'amorçage réapplique
  `platform_toolsets.cron: [web, file, terminal]` (ni navigateur, ni exécution de code, ni
  délégation…), coupe la mémoire persistante, garde `approvals.cron_mode: deny`, désactive
  `tool_call` (appel d'un outil par son nom) et active le plugin. Bonus mesuré : la requête initiale
  d'une exécution passe de 62 à 20 Ko (8 outils au lieu de 24).
- **Chaîne PDF fermée** : le Markdown de l'agent ne peut ni joindre un fichier au PDF (local ou
  encodé dans la page), ni faire appeler une adresse externe : aucune ressource n'est chargée.
- **Scripts de l'image** : `veillectl` exécute les scripts de `/opt/hermes/skills` (lecture seule),
  jamais leur copie du volume, modifiable.
- **Moteur de recherche** : le plugin `deepseek-search-fetch` ne change que le moteur derrière
  `web_search` / `web_extract` (mêmes noms d'outils : garde-fou et marquage inchangés). Code tiers,
  vendorisé à un commit relu et vérifié par empreinte (voir la section suivante).

Limites : une page peut toujours **biaiser le contenu** du rapport (seules les règles de sources du
brief et la relecture y répondent) ; l'agent peut encore transmettre ce qu'il a dans son contexte
(brief, références du lot) en ouvrant une adresse piégée — plus aucun secret ne s'y trouve.

## Recherche web (DeepSeek Search & Fetch)

Les outils `web_search` et `web_extract` de l'agent passent par l'API **Search & Fetch** de DeepSeek
(moteur maison de DeepSeek, [guide](https://api-docs.deepseek.com/guides/search)), avec la clé
`DEEPSEEK_API_KEY` déjà utilisée pour le modèle. Ce n'est pas lié au modèle : n'importe quel LLM piloterait
l'agent avec ce moteur.

- **Pourquoi** : chaque résultat porte sa **date de publication**, un score de pertinence et d'autorité
  (utiles à la période couverte et aux règles de fiabilité du brief) ; un seul fournisseur au lieu des
  offres gratuites tournantes de Hermes.
- **Comment** : plugin Hermes communautaire `deepseek-search-fetch` (Jason Swindle, MIT, non affilié à
  DeepSeek), **vendorisé** dans `docker/hermes-plugins/deepseek-search-fetch/` à un commit relu
  (`VENDORED.md`, empreintes vérifiées par les tests), installé en lecture seule dans l'image, jamais
  téléchargé au démarrage. L'amorçage l'active et règle `web.backend: deepseek` (recherche et lecture de
  pages). Ce réglage ne bascule pas vers un autre moteur ; Hermes retente seulement une fois un appel
  échoué sur ses offres gratuites (`web.keyless_rescue`, réglage par défaut de Hermes conservé : la veille
  continue malgré un incident ponctuel de DeepSeek). Plugin impossible à activer : `ERREUR` dans le
  journal de démarrage et moteur de Hermes.
- **Coût** : 0,75 $ les 1 000 requêtes réussies (échecs et résultats vides gratuits), une requête par
  recherche et par page lue. Compté dans le coût des exécutions (voir plus bas).
- **Limites** : requête de 128 caractères au plus ; une page par requête de lecture ; quotas par compte
  (100 recherches et 25 lectures par seconde selon DeepSeek, moins constaté) ; DeepSeek refuse de lire
  certains sites (politique de sécurité).
- **Revenir au moteur de Hermes** : `VEILLE_WEB_BACKEND=hermes` (toute valeur autre que `deepseek`) dans
  `.env`, puis `docker compose up -d` : plugin désactivé, réglage retiré ; journal :
  `[seed-veille] recherche web : moteur par défaut de Hermes`. Chaque changement de moteur est daté dans
  `veilles/.web-backend` (coût, ci-dessous).

## Consommation et coût

DeepSeek renvoie, à chaque appel, le nombre de tokens consommés : entrée, dont lue en cache,
sortie. Son API ne donne en revanche **ni le coût par requête ni d'historique** : seulement le
solde du compte. Hermes enregistre ces tokens par exécution dans sa base (`state.db`, une
session `cron_<job>_<date>` par run).

L'application en calcule le **coût réel** :
- avec les tarifs de [`veille/pricing.json`](veille/pricing.json), modifiable sans reconstruire
  l'image en le copiant dans `data/veilles/pricing.json` ;
- **heures pleines comprises** : du lundi au vendredi, 01h–04h et 06h–10h UTC, DeepSeek facture le
  **double**. L'estimation de Hermes, elle, n'applique que le tarif creux ;
- **recherche web comprise** (colonne « Web ») : requêtes Search & Fetch lues dans les appels d'outils
  de l'agent (une par recherche, une par page), au tarif de la section `web` (tarif unique, sans heures
  pleines). C'est un **majorant** : échecs, résultats vides et réponses du cache de Hermes ne sont pas
  facturés. Seules comptent les exécutions démarrées quand DeepSeek était le moteur en vigueur
  (`veilles/.web-backend`) : ni celles d'avant la 0.7.0, ni celles du moteur de Hermes. Une section absente
  de la copie du volume (`web` dans une copie antérieure à la 0.7.0) vient du fichier de l'image.

Où le voir :
- **Interface** : section « Coût des exécutions » de chaque veille (durée, tokens, heure pleine ou
  creuse, coût) ; page Santé avec le solde DeepSeek et le coût du mois par veille.
- **Telegram** : « Combien a coûté la veille industrie-4-0 ce mois-ci ? » (outil `get_costs`).
- **CLI** : `docker exec veille-hermes veillectl costs [slug] [--days N]`.

L'aperçu d'une fréquence signale quand elle tombe en heures pleines. Exemple : lundi 8h Paris
= 06h UTC, au tarif ×2. Lundi 6h30 tombe en heures creuses : 04h30 UTC l'été, 05h30 l'hiver.

## Ligne de commande (`veillectl`)

Tout ce que fait l'interface est aussi disponible dans le conteneur :

```bash
docker exec veille-hermes veillectl list
docker exec veille-hermes veillectl show industrie-4-0
docker exec veille-hermes veillectl create --name "Veille Cyber OT" --schedule "30 7 * * 1-5" \
    --recipients rssi@exemple.fr,dsi@exemple.fr --template vierge
docker exec veille-hermes veillectl edit veille-cyber-ot --schedule "0 8 * * 1"
docker exec veille-hermes veillectl pause|resume|run|test-email|archive <slug>
docker exec veille-hermes veillectl costs industrie-4-0
docker exec veille-hermes veillectl reconcile
# veille composants
docker exec veille-hermes veillectl create --kind composants --name "Composants carte X" \
    --schedule "0 23 * * 0" --recipients achats@exemple.fr --components-file /opt/data/bom.csv
docker exec veille-hermes veillectl edit industrie-4-0 --include-components composants-carte-x
docker exec veille-hermes veillectl components list|notices composants-carte-x [--type EOL]
docker exec veille-hermes veillectl components alternatives composants-carte-x [MPN …]  # sinon : fins de vie
docker exec veille-hermes veillectl catalog rebuild|stats     # base des composants partagée
# étape finale du job d'une veille thématique (chapitre composants, PDF, e-mail)
docker exec veille-hermes veillectl report publish <slug> /opt/data/veilles/<slug>/rapports/<D>-veille.md
```

## Exploitation

| Commande | Effet |
|---|---|
| `make veilles` | liste les veilles et leur état |
| `make run-now VEILLE=<slug>` | lance une veille et attend la fin |
| `make logs` | suit les logs du gateway |
| `make verify` | vérifie conteneur, veilles, jobs et interface |
| `make tunnel` | rappelle la commande de tunnel SSH |
| `make mcp-install` / `make mcp-check` | branche le serveur MCP sur le Hermes de l'hôte / liste ses outils |
| `make ui-password` | génère le hash du mot de passe et le secret de l'interface |
| `make backup` | archive horodatée des données (sans `.env`, clés d'API saisies dans l'interface ni nomenclatures déposées pour import, 10 dernières conservées), rangée à côté du dossier de données |
| `make move-data DEST=…` | sort les données du dépôt git (`VEILLE_DATA_DIR`), sans rien perdre |
| `make test` / `make lint` | tests unitaires avec couverture / ruff + shellcheck (+ hadolint) |
| `make smoke` | teste l'image (dépendances, chaîne PDF, contrat CLI Hermes, veillectl, interface) |
| `make integration` | test de bout en bout du conteneur complet (migration, interface, watchdog) |
| `make version` / `make bump` | version courante / nouvelle version + branche `release/vX.Y.Z` |
| `make shell` | ouvre un shell dans le conteneur |
| `make down` | arrête le conteneur (données conservées) |
| `make update` | `git pull`, puis déploiement sûr : sauvegarde, build, démarrage, données vérifiées |
| `make watchdog-check` | exécute le contrôle de surveillance (sans envoyer d'e-mail) |
| `make watchdog-install` | installe la surveillance sur l'hôte et teste le canal d'alerte |

### Disposition des données (`VEILLE_DATA_DIR` = `/opt/data`)

Tout l'état vit dans **un dossier de l'hôte**, `VEILLE_DATA_DIR` (`.env`), monté sur `/opt/data` :
veilles, rapports, jobs (`cron/jobs.json`), historique d'exécution et consommation (`state.db`),
journal d'audit. L'image ne contient aucune donnée : la remplacer ne perd rien.

- **Hors du dépôt git** (recommandé : `/srv/veille-hermes/data`) : ni un `git pull`, ni un
  re-clone, ni un `git clean -x` ne peuvent l'atteindre. Sans `VEILLE_DATA_DIR`, le défaut
  historique `./data` (dans le dépôt) reste utilisé, et `deploy.sh` le signale.
- **Jamais recréé vide en silence** : si le dossier est absent, `docker compose up` échoue
  (`bind source path does not exist`) au lieu de démarrer sur un état vierge.
- Le chien de garde, le serveur MCP (chemins des PDF) et `make backup` lisent le même
  chemin (`scripts/data-dir.sh`).

```
veilles/<slug>/
    veille.json       métadonnées : nom, sujet, fréquence, PDF, lien vers le job Hermes
    config.md         brief de mission (éditable dans l'interface)
    recipients.txt    destinataires, un par ligne
    job-prompt.md     prompt RENDU depuis le gabarit de l'image (ne pas éditer)
    rapports/         AAAA-MM-JJ-HHhMM-veille.{md,pdf}, -charts.json, -mail.json (composants : -pcn.json) ;
                      horodatés : deux exécutions le même jour ne s'écrasent plus (anciens
                      noms AAAA-MM-JJ-veille toujours lus)
    runs/             journaux des lancements manuels (.progress.json : avancement du dernier)
    components.json   veille composants : références, statuts Digi-Key et web, dates de vérification
    notices.json      veille composants : avis PCN / EOL détectés, sources, livraisons
    specs.json        veille composants : fiche Digi-Key de chaque référence (catégorie, caractéristiques)
imports/            nomenclatures déposées pour import (Telegram / CLI), analyses en cours de
                    l'interface (supprimées après 1 h) ; exclu des sauvegardes
veilles/.archive/   veilles archivées, copies des remises à zéro (`<slug>-reinit-…`)
veilles/.audit.log  journal d'audit (interface, CLI, amorçage)
veilles/.settings.json  paramètres de l'interface (adresses d'alerte), lus aussi par le chien de garde
veilles/.web-backend  moteur de recherche web en vigueur, daté à chaque changement (coût de la recherche)
veilles/.veille.db  base SQLite (0600) : index des composants (reconstruit à chaque démarrage depuis les
                    fichiers des veilles) et carnet d'adresses des destinataires
cron/jobs.json      jobs Hermes (état, prochain et dernier run)
state.db            historique des exécutions Hermes (tokens, coûts)
```

Le **job Hermes fait foi** pour l'état (actif / en pause, prochain et dernier run). Le job ne
contient qu'un prompt court qui renvoie vers `job-prompt.md` : une nouvelle image (gabarit
corrigé) ou une modification de la veille prend effet au run suivant, **sans recréer le job**.

## Surveillance (chien de garde)

Le conteneur délivre en `local` : **un échec serait silencieux**, seule l'absence du rapport le
trahirait. Un chien de garde comble ce trou.

Il tourne **sur l'hôte**, pas dans le conteneur : un contrôleur placé dans ce qu'il surveille ne
pourrait signaler ni un conteneur arrêté, ni une clé d'API expirée. Il est **déterministe (aucun
LLM)**, n'utilise que la bibliothèque standard de Python et ne dépend d'aucune installation Hermes
sur l'hôte : il lit le volume et envoie avec le skill `brevo-email` et le `.env` **du dépôt**.

Contrôle quotidien (10h, heure de Paris) :

1. le conteneur tourne ;
2. pour chaque veille **active** (les veilles en pause sont ignorées) : son job existe, son
   dernier run n'est pas en erreur ni « ok » sans avoir produit de rapport, et un rapport récent et
   plausible existe. « Récent » dépend
   de **sa** fréquence : plus grand écart entre deux tirs + 24 h (8 jours pour une veille
   hebdomadaire, 2 jours pour une quotidienne).

Les alertes vont aux adresses saisies dans l'interface, page **Paramètres** (stockées sur le
volume, dans `veilles/.settings.json`, prises en compte au contrôle suivant) ; à défaut à
`ALERT_EMAIL` du `.env`, puis à l'expéditeur Brevo. Un changement de destinataires est signalé à
l'ancienne et à la nouvelle adresse. Alerte immédiate à chaque nouveau problème, rappel tous les
3 jours tant qu'il persiste, puis **e-mail de rétablissement**. L'état est conservé dans
`/var/lib/veille-watchdog/state.json`.

```bash
make watchdog-check      # contrôler maintenant (aucun e-mail envoyé)
make watchdog-install    # (ré)installer la surveillance et tester le canal d'alerte
./scripts/install-watchdog.sh --uninstall
```

## Mise à jour

```bash
make update          # git pull --ff-only, puis ./scripts/deploy.sh
```

`deploy.sh` ne touche jamais aux données, et le vérifie :

1. **refuse de démarrer** si le dossier de données est absent (`--init` uniquement pour une
   première installation) ;
2. **sauvegarde** les données, conteneur arrêté pour une archive cohérente (environ 1 s pour
   40 Mo), à côté du dossier de données ;
3. démarre la nouvelle version et attend la fin de **son** amorçage (réconciliation des jobs) ;
4. vérifie que le conteneur monte bien ce dossier, puis **compare l'inventaire** (veilles et
   rapports PDF) avant et après : s'il manque quoi que ce soit, le déploiement échoue et affiche
   la commande de restauration.

Le conteneur est arrêté pendant la sauvegarde : un rapport en cours de génération est
interrompu (comme lors de tout redémarrage). Éviter de déployer pendant une exécution
planifiée.

Restauration manuelle d'une sauvegarde :

```bash
docker compose stop
tar -xzf /srv/veille-hermes/backups/veille-data-AAAAMMJJ-HHMMSS.tar.gz -C /srv/veille-hermes/data
docker compose start
```

### Sortir les données du dépôt (installation existante)

Une seule fois, sur le serveur :

```bash
make move-data DEST=/srv/veille-hermes/data
```

Le conteneur est arrêté, `./data` est déplacé (renommage ; sur un autre disque : copie, puis
l'original est **renommé**, jamais supprimé), `VEILLE_DATA_DIR` est écrit dans `.env`, puis
`deploy.sh` redéploie et vérifie l'inventaire. Le chien de garde est réinstallé s'il l'était ;
relancer `make mcp-install` si le serveur MCP est branché sur le Hermes de l'hôte.

L'image amont est **épinglée** dans le `Dockerfile`. Dependabot propose ses montées de version
en PR ; la CI les valide (tests, lint, smoke test et test d'intégration sur l'image construite,
dont le contrat avec la CLI `hermes cron`) **avant** toute publication sur `ghcr.io`.

## Versions et CI

Version courante : fichier [`VERSION`](VERSION). Chaque livraison incrémente la version et
passe par une branche `release/vX.Y.Z` mergée dans `main` ; le merge crée l'étiquette et la
release GitHub. Détails, suite de tests et commandes locales : [CONTRIBUTING.md](CONTRIBUTING.md).

### Depuis la v0.2 (veille unique dans `data/veille/`)

Rien à faire : au premier démarrage de la nouvelle image, `data/veille/` est déplacé vers
`data/veilles/industrie-4-0/`, avec l'historique des rapports (anti-répétition) et le **même
job** (édité, pas recréé). Un lien `data/veille → veilles/industrie-4-0` garde l'ancien chemin
lisible. `destinataire.txt` devient `recipients.txt`. Ensuite, sur l'hôte :

1. ajoute le résultat de `make ui-password` dans `.env`, puis renseigne l'adresse d'alerte dans
   l'interface, page **Paramètres** ;
2. réinstalle la surveillance : `make watchdog-install`.

`make update` sauvegarde les données avant toute mise à jour (voir plus haut).

## Arborescence

```
veille-hermes/
├── Dockerfile                    # image dérivée : deps, skills, gabarits, veille_core, interface
├── docker-compose.yml            # service, volume, secrets, port 127.0.0.1:8088, limites
├── Makefile                      # raccourcis d'exploitation
├── requirements-app.txt          # dépendances de veille_core / veille_ui (venv séparé)
├── veille/                       # gabarit du prompt + modèles de brief
├── veille_core/                  # cœur multi-veilles + CLI veillectl
├── veille_ui/                    # interface web (FastAPI + Jinja + HTMX)
├── veille_mcp/                   # serveur MCP (stdio) pour piloter les veilles depuis Hermes
├── skills/                       # brevo-email, styled-pdf-report
├── docker/                       # hook d'amorçage, service s6 de l'interface, veillectl,
│                                 #   plugins Hermes veille-guard (garde-fou de l'agent) et
│                                 #   deepseek-search-fetch (recherche web, vendorisé)
├── deploy/systemd/               # units de la surveillance (sur l'hôte)
├── scripts/                      # déploiement, tests (smoke, intégration), watchdog, MCP, versions
├── tests/                        # pytest (faux binaire hermes pour le cœur et l'interface)
├── VERSION                       # numéro de version (source unique)
├── docs/PLAN-CONTAINERISATION.md # analyse + plan de conteneurisation (archive)
└── .github/                      # workflows ci / image / release, Dependabot, modèle de PR
```

## Secrets

Aucun secret n'est versionné. Voir [`.env.example`](.env.example) : `DEEPSEEK_API_KEY`,
`BREVO_API_KEY`, `BREVO_SENDER_EMAIL`, `BREVO_SENDER_NAME`, `VEILLE_UI_PASSWORD_HASH`,
`VEILLE_UI_SECRET`, et facultativement `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET` (plus simple :
page **Sources** de l'interface) et `ALERT_EMAIL` (repli : l'adresse d'alerte se saisit page
**Paramètres**). Le `.env` est ignoré par git **et** par le build Docker.

## Architecture

Le conteneur n'a besoin que d'accès **sortants** (API DeepSeek : modèle et recherche web, API Brevo, API
Digi-Key ; en secours d'un appel de recherche échoué, les offres gratuites de Hermes). Le
seul port publié est celui de l'interface, lié à `127.0.0.1` ; le serveur MCP passe par
`docker exec -i` (stdio), sans port. La livraison se fait **uniquement
par e-mail** (`deliver: local`) ; le gateway Telegram de l'hôte reste indépendant. L'interface,
`veillectl` et `veille-mcp` ne parlent à Hermes que par sa CLI publique (`hermes cron …`) et la lecture de
`cron/jobs.json`, isolées dans `veille_core/hermes.py`.

## Licence

À définir (les skills et le label de l'image déclarent MIT ; aucun fichier `LICENSE` pour l'instant).
