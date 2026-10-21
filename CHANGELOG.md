# Journal des modifications

Toutes les modifications notables de ce projet sont documentées dans ce fichier.

Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et le projet adhère au
[versionnage sémantique](https://semver.org/lang/fr/).

## [Non publié]

## [0.7.0] — 2026-10-21

Recherche web des veilles par l'API **DeepSeek Search & Fetch** (ouverte le 17/10/2026), avec la clé déjà
utilisée pour le modèle ; Hermes Agent v0.22.2.

### Ajouté

- **Moteur de recherche web DeepSeek** : `web_search` et `web_extract` passent par
  `api.deepseek.com/v1/tools/search|fetch`. Chaque résultat porte sa date de publication, ses scores de
  pertinence et d'autorité. Plugin Hermes communautaire `deepseek-search-fetch` (Jason Swindle, MIT, non
  affilié à DeepSeek) **vendorisé** au commit relu `b17ae50` (v1.0.8) dans
  `docker/hermes-plugins/deepseek-search-fetch/`, installé en lecture seule dans `/opt/hermes/plugins`
  (jamais téléchargé au démarrage) ; empreintes SHA-256 vérifiées par les tests. Noms d'outils
  inchangés : garde-fou et marquage « non fiable » s'appliquent tels quels.
- Amorçage (à chaque démarrage) : plugin activé et `web.backend: deepseek` (recherche et lecture de pages,
  sans repli silencieux). **`VEILLE_WEB_BACKEND=hermes`** dans `.env` : retour au moteur par défaut de
  Hermes (plugin désactivé, réglage retiré). Sans `DEEPSEEK_API_KEY`, moteur de Hermes.
- **Coût de la recherche web** dans le coût des exécutions (interface : colonne « Web » ; `veillectl
  costs` ; MCP `get_costs` : `web_requests`, `web_usd`) : requêtes lues dans les appels d'outils de
  l'agent (`state.db`, une par recherche, une par page lue), à 0,75 $ les 1 000 (section `web` de
  `pricing.json`, sans heures pleines). Majorant (échecs, résultats vides et cache de Hermes ne sont pas
  facturés) ; rien avant le 2026-10-21 (`since`, moteur précédent gratuit) ni avec
  `VEILLE_WEB_BACKEND=hermes`.
- `scripts/web-probe.py` : contrat du moteur web avec Hermes (smoke test) — faux service DeepSeek sur la
  boucle locale, interrogé par les vrais outils `web_search` / `web_extract`, sans clé ni coût.

### Modifié

- **Image amont : Hermes Agent v0.22.2** (v2026.9.24 = v0.21.5 auparavant), requise par le plugin
  (`requires_hermes >=0.22.0`). Contrat vérifié : CLI `hermes cron` (`--model` épingle toujours le modèle
  d'une veille), noms des outils web / fichiers / terminal, hooks du garde-fou, clés de configuration,
  schéma de `state.db`. Debian 13, Python 3.13 pour l'application.
- Venv de l'application créé par `python3 -m venv` + `pip` (`python3-venv` ajouté aux paquets) : l'image
  amont ne fournit plus `uv` dans le PATH.
- `deploy.sh` (et le test d'intégration) attendent l'amorçage 240 s au lieu de 120 : il prend ≈ 53 s
  avec Hermes v0.22.2 (≈ 28 s avant), la marge se réduisait sur un petit serveur.
- Tableau « Coût des exécutions » en mode mobile (PWA) : chaque valeur porte son libellé dans les cartes
  (`data-label`, CSS seul), « Appels LLM » et « Requêtes web » ne se confondent plus.
- Smoke test : fichiers du plugin, moteur web (sonde), requêtes web lues dans `state.db`. Test
  d'intégration : réglage posé par l'amorçage, puis retour au moteur de Hermes.

### Mesures

- Exécution planifiée réelle (`hermes cron tick`, veille industrie-4-0, faux LLM et faux service DeepSeek
  locaux) : requête initiale 23 757 → 22 470 octets (−5,4 %), schémas des 8 outils 13 131 → 11 936 octets
  (−9,1 %) ; recherche et lecture de page servies par DeepSeek ; résultats marqués « non fiables » (garde-fou,
  plus le nouvel encadrement de Hermes v0.22) ; date, scores de pertinence et d'autorité transmis à l'agent.
- CLI de Hermes plus lente en v0.22.2 : ≈ 2,0 → ≈ 3,2 s par appel (`hermes cron list`, 5 mesures). Les
  actions de l'interface qui passent par `hermes cron` (pause, reprise, lancement, historique) prennent
  ≈ 1,2 s de plus ; le tableau de bord (lecture de `jobs.json`) n'est pas concerné.
- Démarrage complet sur un volume neuf : ≈ 28 → ≈ 53 s (amorçage : une douzaine d'appels à la CLI de
  Hermes), dont 2 appels (≈ 7 s) pour le moteur web.
- Image : 4,70 → 5,36 Go (+14 %, image amont : Debian 13, Chromium) ; plugin : 30 Ko.

### Déploiement

- Rien à faire : `make update`. Vérifier dans les logs `[seed-veille] web.backend = deepseek`.
- Copie de `pricing.json` dans le volume (`data/veilles/pricing.json`) : y ajouter la section `web` de
  `veille/pricing.json`, sinon la recherche web compte pour 0.
- Revenir au moteur de Hermes : `VEILLE_WEB_BACKEND=hermes` dans `.env`, puis `docker compose up -d`.

## [0.6.0] — 2026-10-21

Protection contre l'injection de prompt : l'agent, qui lit le web, ne peut plus que chercher, lire les
fichiers de sa veille, écrire ses fichiers de sortie et lancer les commandes de son job.

### Sécurité

- **Garde-fou `veille-guard`** : plugin Hermes livré dans l'image (`/opt/hermes/plugins`, lecture
  seule), activé à chaque démarrage ; règles dans `veille_core/guard.py`. Avant chaque appel d'outil
  d'une exécution planifiée, par listes blanches (tout le reste est refusé) :
  - outils : `web_search`, `web_extract`, `read_file`, `search_files`, `write_file`, `patch`,
    `terminal`, avec leurs seuls paramètres attendus (ni mode V4A de `patch`, ni `cross_profile`,
    ni arrière-plan, ni durée au-delà de `TERMINAL_MAX_FOREGROUND_TIMEOUT`, 1 200 s dans l'image :
    Hermes passerait la commande en arrière-plan, au résultat illisible) ;
  - fichiers : lecture de `config.md`, `job-prompt.md` et `rapports/` de **sa** veille ; écriture des
    seuls `rapports/<D>-{veille.md, charts.json, mail.json, pcn.json}` ; chemins absolus, liens
    symboliques résolus ; ceux d'un rapport déjà publié ne se modifient plus (historique relu par
    les exécutions suivantes) ;
  - terminal : `date`, `ls` de ses rapports, `veillectl components scan|progress|finalize` et
    `veillectl report publish` pour **son** slug et ses fichiers, sans guillemets ni caractère spécial
    du shell ;
  - exécution identifiée par sa tâche `cron:<job>:…`, stable même quand Hermes renomme la session en
    compressant le contexte ; exécution planifiée non identifiable, ou job qui n'est pas celui d'une
    veille : aucun outil ;
  - une erreur du garde-fou, ou son import impossible, refuse l'appel (Hermes, lui, laisserait passer
    un hook en échec) ; chaque refus est inscrit au journal d'audit (page Santé, origine
    « veille-guard »), sans la liste des commandes permises que reçoit l'agent ;
  - plugin impossible à activer au démarrage : exécutions planifiées réduites à la recherche web.
- **Contenu externe marqué** comme non fiable dans le contexte de l'agent (résultats web, anciens
  rapports, sorties de commande) : encadrement, caractères invisibles retirés, marqueurs neutralisés.
  Règles de sécurité en tête des deux gabarits de prompt.
- **Chaîne PDF fermée** (`build_report.py`) : un Markdown piégé pouvait joindre n'importe quel fichier
  lisible au PDF envoyé (`<a rel="attachment" href="file:///opt/data/.env">`), y glisser un fichier
  encodé (`data:`, ex. un exécutable) et faire appeler des adresses externes. Plus de
  `--embed-resources` (qui lisait lui-même les fichiers désignés) ; WeasyPrint ne charge plus aucune
  ressource, et les pièces jointes sont retirées du document avant l'écriture du PDF (WeasyPrint 61
  les lisait sans passer par le chargeur). Vérifié avec WeasyPrint 61.1 (CI Ubuntu), 62.3 (image,
  Debian 13), 66 et 70.
- **Outils des exécutions planifiées réduits**, réappliqués à chaque démarrage : `web`, `file`,
  `terminal` (ni navigateur, ni exécution de code, ni mémoire, ni délégation, ni gestion des skills) ;
  mémoire persistante coupée ; `approvals.cron_mode: deny` ; pont `tool_call` désactivé.
- **Scripts de l'image** : `veillectl` (et l'interface) exécutent `brevo_send.py` et `build_report.py`
  depuis `/opt/hermes/skills` (lecture seule), plus depuis le volume, où l'agent pouvait les modifier
  et où la synchronisation de Hermes conserve une copie modifiée.
- **MCP** (Telegram) : `read_report` et `get_run_log` encadrent le texte venu du web comme non fiable ;
  les instructions du serveur demandent de n'y appliquer aucune consigne.

### Ajouté

- **`veillectl report publish <slug> <rapports/D-veille.md>`** : étape finale de la veille thématique,
  sans l'agent. Ajoute le chapitre composants s'il y en a un, produit le PDF (titre, en-tête, pied,
  accent de la veille ; sous-titre = période ; temps de lecture calculé ; graphique de
  `D-charts.json`) et l'envoie aux seuls destinataires de la veille, en copie cachée, avec le PDF et la
  liste Excel des composants. Période et messages clés viennent de `D-mail.json` (écrit par l'agent),
  nettoyés et bornés (100 et 1 500 caractères), passés au script d'envoi sous la forme
  `--option=valeur`. Pas deux envois à moins de 30 min pour une même veille (`.dernier-envoi`, posé
  avant l'envoi et retiré si Brevo n'a rien accepté : un envoi interrompu ne se double pas) ; sans
  attente du statut de remise (`--verify`), qui pouvait dépasser le délai de l'envoi.
- `scripts/guard-probe.py` : contrat du garde-fou avec Hermes (smoke test, test d'intégration).

### Modifié

- Prompt de la veille thématique : l'agent n'appelle plus `build_report.py` ni `brevo_send.py`, ni
  `veillectl components chapter` ; il écrit `D-mail.json` puis lance `veillectl report publish`.
- Les jobs n'ont plus de skill attaché ; la réconciliation retire `brevo-email` et
  `styled-pdf-report` des jobs existants (`hermes cron edit --clear-skills`).
- En-tête et pied de page du PDF échappés pour le CSS : un guillemet ne fait plus disparaître l'en-tête.

### Mesures

- Exécution planifiée réelle (`hermes cron run`, Hermes v2026.9.24, faux LLM local) : requête
  initiale 62,4 → 19,6 Ko (−69 %) ; 24 → 8 outils exposés (schémas 42,1 → 13,1 Ko) ; prompt système
  7,6 → 4,8 Ko. `id`, `cat /opt/data/.env` et `/proc/self/environ` refusés, `ls` des rapports permis.
- Rendu PDF identique au pixel près sur un rapport type (4 pages, WeasyPrint 61.1, 62.3, 66 et 70).

### Déploiement

- Rien à faire : le démarrage applique la configuration, active le plugin et retire les skills des
  jobs. Vérifier dans les logs `[seed-veille] plugin veille-guard activé`.
- Recommandé côté hôte (Telegram) : garder l'approbation des commandes du Hermes de l'hôte.

## [0.5.0] — 2026-10-13

Page « Composants » : tout ce qui a déjà été analysé, toutes veilles confondues. Liste de composants au format
`MPN ; Fabricant` (la réf. interne n'est plus utilisée).

### Ajouté

- **Page « Composants »** (menu de l'interface, `/composants`) : la base des composants consultable.
  - Une ligne par référence déjà analysée, toutes veilles composants confondues (actives et archivées) : statut
    le plus récent, avis dédoublonnés (chacun une fois, à sa première date de détection), échéance, veilles qui
    l'ont analysée (une veille archivée est nommée « slug (archivée le AAAA-MM-JJ) », dans l'interface comme
    dans l'export).
  - Même tableau que la liste d'une veille : recherche plein texte (MPN, fabricant, statut, intitulés et
    résumés des avis), filtre par colonne, fins de vie en tête et en rouge, détail au toucher. En plus : filtre
    **par veille**.
  - Détail chargé à la demande pour la seule référence touchée (lecture de la base par sa clé).
  - **Export Excel** de toute la base : le classeur de l'export d'une veille, plus une colonne « Veilles ».
  - Base momentanément indisponible (verrou, reconstruction) : signalé sur la page (HTTP 503, « recharger dans
    quelques instants »), sans inonder le journal d'audit.
  - Un avis de forme inattendue dans la base (fichier retouché à la main, même celui d'une veille archivée) est
    ignoré : il ne bloque ni la page, ni l'export, ni la reprise à l'ajout de références (qui pouvait échouer
    sur un tel avis).
  - Liens vers la page depuis la liste d'une veille et la carte « Base des composants » de la page Santé.
- **Filtres par liste** dans le tableau des composants (valeur exacte, pas une sous-chaîne) : **statut** (page
  d'une veille et page Composants) et **veille** (page Composants). Sur mobile (PWA), ils restent visibles,
  sur deux colonnes lisibles, de 320 à 430 px.
- Colonne **Statut** dans la liste d'une veille, à la place de « Réf. ».
  - Libellé commun aux statuts Digi-Key et web : « Obsolete » (Digi-Key) et « OBSOLETE » (web) donnent
    « Obsolète », un seul choix du filtre. Un statut Digi-Key sans équivalent (« Preliminary »…) reste tel
    quel, et le terme Digi-Key d'origine reste cherchable.
  - Digi-Key fait foi s'il connaît la référence. S'il ne la trouve plus, le statut établi par la recherche web
    l'emporte sur son ancien statut, qui n'est affiché qu'à défaut.
  - « Statut inconnu » : aucun statut connu.

### Modifié

- **Liste de composants : `MPN ; Fabricant`.** La réf. interne n'est plus demandée, enregistrée ni affichée.
  - Rétrocompatible : les anciennes listes `MPN ; Fabricant ; Réf. interne` restent acceptées partout
    (formulaire, `--file`, `components add`, Telegram). Les colonnes suivantes sont ignorées et la liste est
    réaffichée au nouveau format.
  - Les `components.json` existants restent lus tels quels. Aucune migration : une réf. déjà enregistrée
    n'est plus lue, et ré-enregistrer la même liste ne réécrit rien.
  - Import CSV / Excel : seules les colonnes MPN et Fabricant sont proposées. Une colonne de réf. interne
    (« Code article », « Réf. interne »…) reste reconnue pour trouver la ligne d'en-tête et n'est jamais prise
    pour le MPN : la détection est inchangée.
  - Retirée de l'interface (liste, détail, formulaire d'import), du PDF, de l'e-mail et de l'export Excel
    (colonne « Réf. interne »).
  - Le lot de recherche web envoyé à l'agent ne contient plus la réf. : 136 octets par référence au lieu
    de 153 (−11 % sur cette partie du prompt).
- Tableau des composants mutualisé (`partials/comp_table.html`) entre la page d'une veille et la page
  Composants. La lecture de la base est mutualisée aussi (`catalog._gather`, utilisée par la reprise et par
  la page).
  - Elle se fait dans un même instantané (une transaction de lecture) : les statuts et les avis ne peuvent plus
    provenir de deux états différents quand une autre veille écrit entre les deux requêtes.
  - Correspondance statut Digi-Key → type d'avis (`digikey.STATUS_TYPES`) dérivée de la même table que les
    libellés de statut (`components.DK_LIFECYCLE`) : inchangée.

### Supprimé

- MCP : paramètre `ref_column` de `preview_component_import` et `import_components`. Un agent qui
  l'envoie encore n'est pas refusé : le paramètre est ignoré. Champ `ref` de `list_components`.
- CLI : option `--ref` de `veillectl components import`, retirée de l'aide. Un script qui la passe encore
  n'échoue pas : elle est acceptée puis ignorée.

## [0.4.1] — 2026-10-12

Reprises sur incident réseau Digi-Key.

### Corrigé

- **Reprises sur incident réseau Digi-Key.** Un seul `ConnectionResetError` arrêtait la phase Digi-Key
  (veille « composants-nomenclature-2 », 12/10/2026 : 230 références vérifiées sur 386, les 156 suivantes
  jamais interrogées) alors que l'API répondait normalement quelques minutes plus tard (230 appels sur 900
  autorisés : ni quota ni débit en cause).
  - Panne passagère (connexion réinitialisée, interrompue ou refusée, `URLError`, délai dépassé, réponse
    tronquée, HTTP 500 / 502 / 503 / 504, HTTP 429), à l'obtention du jeton comme à la recherche : le
    passage attend puis **reprend à la référence interrompue**. Les références déjà vérifiées ne sont pas
    réinterrogées.
  - Attentes progressives et bornées : 5, 10, 20, 30, 45 s puis 60 s (plafond), ± 20 % ; un peu plus de
    6 min au total au plus. Un 429 respecte toujours le Retry-After (10 s sans en-tête, 60 s au plus) et
    compte parmi les tentatives.
  - Un seul compteur par passage, toutes références confondues (la phase est reprise, pas chaque requête) :
    au 10e échec, la phase est abandonnée (« abandon après 10 tentatives, reprise au prochain passage »).
    Les références restantes ne sont jamais marquées vérifiées : elles partent au lot de recherche web,
    comme avant.
  - Erreurs permanentes jamais réessayées : HTTP 400, 403, 401 après renouvellement du jeton (404 :
    référence introuvable, comme avant).
  - Le budget de temps (15 min) et le plafond de 900 appels gardent la priorité : une attente qui les
    dépasserait n'est pas faite, et l'arrêt est signalé comme avant.
  - Traçabilité : chaque tentative est journalisée sur la sortie d'erreur du scan
    (`Digi-Key — tentative 2/10 : … ; reprise dans 10 s`). Le nombre d'incidents (`digikey.incidents`) et
    l'abandon figurent dans la sortie du scan, dans l'avancement en direct (avertissement « Digi-Key : … »)
    et dans la ligne d'audit `components-scan`, qui nomme désormais l'arrêt. Des incidents surmontés sont
    signalés aussi.
  - Une réponse tronquée (`IncompleteRead`) ou une ligne d'état illisible ne fait plus échouer le scan sur
    une exception non traitée (plus de lot web dans ce cas).

## [0.4.0] — 2026-10-10

Base des composants partagée entre veilles, carnet d'adresses des destinataires, repli alldatasheet pour
les références introuvables.

### Ajouté

- **Base des composants** (`veilles/.veille.db`, SQLite de la bibliothèque standard : ni service ni
  dépendance de plus). Elle partage entre veilles ce qu'on sait de chaque référence : statuts Digi-Key et
  web, date de la dernière recherche web, avis PCN / fins de vie.
  - **Reprise automatique.** Une référence ajoutée à une veille composants (création, ajout, import,
    modification de la liste ; interface, CLI ou Telegram) reprend ce qu'en savent les autres veilles,
    actives ou archivées.
    - Les statuts et la date de recherche web sont repris : la référence passe dans la rotation normale de
      la recherche web au lieu d'être recherchée en priorité, ce qui économise des tokens. Si une autre
      veille l'a vérifiée le jour même, Digi-Key n'est pas réinterrogé.
    - Les avis sont repris avec leur date de détection d'origine et dédoublonnés comme les autres. Ils
      partent avec le premier e-mail de la veille.
    - Message dans l'interface. Comptes `known` / `notices_known` en retour de `import_components` et
      `add_components` (Telegram) et dans la sortie de `veillectl components add|import`.
  - **Index des fichiers, pas une nouvelle source de vérité.** `components.json` et `notices.json` restent
    la référence : le chien de garde et les sauvegardes sont inchangés.
    - Chaque veille y a sa contribution (provenance). Une réinitialisation retire ce qu'elle avait apporté
      (ce que d'autres veilles en ont déjà repris leur reste : les réinitialiser aussi au besoin) ; un
      archivage le conserve.
    - Reconstruite à chaque démarrage (`veillectl catalog rebuild`, hook de seed) depuis les veilles
      actives et archivées, sans les copies de réinitialisation. Une base corrompue, ou d'un schéma plus
      récent, est mise de côté (`.veille.db.corrompue-…`) puis recréée.
  - Un échec de la base n'empêche jamais une analyse, une création ou un ajout : il est tracé dans le
    journal d'audit (`db-error`). Un verrou tenu ou une erreur d'E/S ne fait jamais mettre la base de côté.
  - À la reprise, un statut Digi-Key l'emporte sur un statut web venu d'une autre veille (Digi-Key fait foi).
  - Robustesse multi-processus (interface, CLI du job, MCP) :
    - journal DELETE et non WAL : aucun fichier annexe permanent qu'un `docker exec` en root rendrait
      inaccessible à hermes ;
    - une base créée par root est rendue au propriétaire du dossier ;
    - attente d'un verrou plutôt qu'un échec ;
    - schéma versionné, migrations transactionnelles ;
    - droits 0600.
  - CLI : `veillectl catalog rebuild|stats`. Page Santé : contenu de la base.
- **Carnet d'adresses des destinataires.**
  - Les adresses saisies dans une veille (interface, CLI, Telegram) sont mémorisées. Les adresses des
    veilles existantes l'amorcent à la mise à jour. L'adresse factice d'une veille amorcée sans destinataire
    n'y entre jamais.
  - Formulaire de veille (PWA) :
    - destinataires en puces (× pour retirer) ;
    - un champ unique « Ajouter ou rechercher » qui filtre le carnet (autocomplétion insensible à la casse
      et aux accents). Entrée prend la première suggestion. Virgule, point-virgule, espace et collage de
      plusieurs adresses créent les puces ; Retour arrière retire la dernière ;
    - une adresse du carnet s'ajoute d'un toucher, ou par **glisser-déposer**. Le retrait se fait aussi en
      glissant vers le carnet ;
    - adresse invalide signalée sans être ajoutée ; adresse tapée sans Entrée prise en compte à l'envoi ;
      envoi sans destinataire bloqué.
  - Glisser-déposer en Pointer Events (souris et tactile ; le glisser-déposer HTML5 ne fonctionne pas dans
    les WebView mobiles), avec appui long au doigt pour garder le défilement. Compatible CSP : aucun script
    ni style en ligne.
  - Amélioration progressive : la zone de texte reste le champ envoyé, validé par le serveur comme avant ;
    sans JavaScript, le formulaire est inchangé.
  - Page Paramètres : liste complète du carnet et « Oublier » une adresse (données personnelles, effacée
    aussi des pages libres du fichier). Une adresse oubliée reste destinataire des veilles qui l'utilisent.
    Aucune adresse dans le journal d'audit ; carnet jamais exposé au MCP.
- **Repli alldatasheet** pour une référence inconnue de Digi-Key et introuvable par la recherche web.
  - Le lot du scan fournit à l'agent la recherche alldatasheet de la référence (MPN encodé), son statut web
    précédent et le site du fabricant déjà trouvé. Une référence restée `UNKNOWN` passe directement par ce
    repli.
  - L'agent repère la fiche du bon fabricant et son « Direct Link », puis cherche la fiche produit et les
    avis PCN / EOL sur le site du fabricant. Il rend `datasheet` (page alldatasheet) et `maker` (site du
    fabricant) dans `statuses`.
  - alldatasheet est derrière un challenge Cloudflare et sans API publique : il n'est consulté que par
    l'agent, jamais par un robot de l'application.
  - Garde-fous déterministes : un miroir de fiches techniques ne prouve jamais un statut ni un avis. Un
    statut autre que `UNKNOWN` dont la fiche est sur alldatasheet est rejeté, et une source alldatasheet est
    retirée d'un avis (rejeté s'il n'en a pas d'autre).
  - Affiché dans le détail du composant (liens « alldatasheet » et « site du fabricant »), l'export Excel
    (« Statut web » suffixé « (alldatasheet) »), la recherche de la liste et `list_components`
    (`web_status_datasheet`, `web_status_maker`). Le site du fabricant reste connu pour les passages
    suivants.

## [0.3.8] — 2026-10-10

Références « introuvables chez Digi-Key » retrouvées, ou leur statut établi par l'agent ; recherche
d'une veille composants réinitialisable.

### Ajouté

- **Réinitialiser la recherche** d'une veille composants, pour relancer l'analyse de zéro : avis
  détectés, statuts Digi-Key et web et dates de recherche effacés ; la liste des composants (réf.
  internes, dates d'ajout), les rapports et les destinataires sont conservés. Les avis retrouvés à
  l'analyse suivante sont de nouveau signalés comme nouveaux.
  - Interface (PWA) : carte « Réinitialiser la recherche » de la page de la veille, avec
    confirmation (nombre d'avis effacés) ; bouton désactivé pendant un lancement.
  - CLI : `veillectl components reset <slug>`. Telegram : outil `reset_component_search`, marqué
    destructeur (approbation) et qui exige de répéter le slug, comme `archive_veille`.
  - Copie préalable de `components.json` et `notices.json` dans `veilles/.archive/<slug>-reinit-…/`
    (nom unique : deux remises à zéro de suite n'écrasent jamais la première copie). Refusée pendant
    un lancement manuel, et sans rien modifier si un fichier est illisible. Tracée dans l'audit.
- **Statut web** d'une référence que Digi-Key ne trouve pas : le scan signale `digikey_error` pour
  chaque composant du lot ; l'agent trouve lui-même sa fiche (fabricant, Mouser, Farnell, LCSC…) et
  rend son statut (`ACTIVE`, `NRND`, `LTB`, `OBSOLETE` ou `UNKNOWN`) dans un champ `statuses` du JSON,
  validé comme les avis (référence suivie, statut connu, fiche http(s) obligatoire sauf `UNKNOWN`) ;
  un champ mal formé est rejeté sans bloquer l'enregistrement des avis.
  - Affiché dans le détail du composant (lien vers la fiche), dans la recherche de la liste, dans
    l'export Excel (colonne « Statut web ») et par `list_components` (Telegram). Sans avis ni fiche
    Digi-Key, le lien du composant devient la fiche trouvée sur le web. Effacé dès que Digi-Key
    retrouve la référence (Digi-Key fait foi).
  - `NRND`, `LTB` et `OBSOLETE` créent l'avis correspondant, comme un statut Digi-Key : identifié par
    son intitulé, la même obsolescence citée avec une autre fiche n'est pas un nouvel avis (fiche
    ajoutée aux sources), une aggravation (NRND → OBSOLETE) en est un.
  - Une référence que Digi-Key ne trouve plus passe **une fois en priorité** dans le lot de recherche
    web (comme un statut Digi-Key qui change), puis reprend la rotation normale : le lot ne se fige
    jamais sur elle, même si l'agent ne rend aucun statut valide.

### Corrigé

- **Composants existants signalés « introuvable chez Digi-Key »** (toute la liste, MPN uniques
  compris). La fiche exacte `/products/v4/search/{mpn}/productdetails` :
  - exige l'en-tête `X-DIGIKEY-Account-Id` en OAuth « 2-legged » (documentation v4), jamais envoyé ;
  - ne reconnaît pas une référence vendue par plusieurs fabricants (1N4148, 2N3904, BC547B, LM358DR…) ;
  - ne reconnaît pas une référence saisie sans sa ponctuation (« 2N7002215 », « 74HC14D653 »,
    « BAV99215 » pour les « …,215 » de Nexperia) ;
  - et tout HTTP 400 y était traduit en « introuvable ».
  Chaque référence passe désormais par la **recherche par mot-clé** (`/products/v4/search/keyword`,
  qui n'exige pas d'Account-Id), toujours un seul appel par référence. La fiche retenue est celle du
  fabricant suivi, au MPN identique de préférence, sinon à la ponctuation près ; le « + » d'une version
  sans plomb et le point décimal (« SMBJ6.0A » ≠ « SMBJ60A ») restent significatifs. Un MPN trouvé
  seulement chez d'autres fabricants est signalé « fabricant différent chez Digi-Key (…) ».
- Un refus de Digi-Key (HTTP 400, 500…) est une erreur affichée qui nomme la référence en cause, plus
  jamais un « introuvable » silencieux ; une fiche mal formée ne fait plus échouer le passage.

### Modifié

- Prompt des veilles composants : étape « référence inconnue de Digi-Key » et champ `statuses`.
  Re-rendu au prochain démarrage du conteneur (réconciliation), sans action manuelle.
- Export Excel : colonne « Statut web » après « Vérifié Digi-Key » (les colonnes suivantes se décalent
  d'un cran).
- Test de fumée de l'image : 31 outils MCP attendus.

### Mesures

- Passage Digi-Key de 300 références (200 uniques, 60 multi-fabricants, 40 absentes ; faux Digi-Key
  à 5 ms par appel) : 301 appels avant comme après ; statuts trouvés 200 → 260, références laissées à
  l'agent 100 → 40 (avec la vraie API, sans Account-Id, la fiche exacte n'en trouvait aucune). Durée
  au débit réel inchangée (3,0 min) ; calcul hors latence ≈ 0,55 → 0,62 ms par appel (réponses de
  recherche plus longues).
- Mêmes données avant/après : rendu de la liste de 300 composants 7,9 → 8,4 ms (même processus) ;
  page de la veille ≈ 28–32 ms et export Excel ≈ 85–110 ms, sans écart mesurable (bruit de mesure) ;
  +0,8 Ko de HTML (carte de réinitialisation).

## [0.3.7] — 2026-10-10

Avancement d'une analyse composants visible en direct, dans l'interface et sur Telegram.

### Ajouté

- **Avancement d'un lancement manuel de veille composants** (« Lancer l'analyse » et « Vérifier les
  statuts Digi-Key », depuis l'interface ou Telegram) : jusqu'ici, rien ne s'affichait avant la fin.
  - Étapes 1/3 « Statut Digi-Key » (référence en cours, `124 / 310`), 2/3 « Recherche web » (composants
    du lot faits, en cours, à rechercher), 3/3 « Rapport et envoi » (enregistrement des avis, PDF,
    e-mail) ; à la fin, nombre de nouveaux avis par composant, ou étape où le lancement s'est arrêté.
  - **Page de la veille** : panneau au-dessus du journal, rafraîchi avec lui (toutes les 5 s) : étapes,
    barre de progression, une pastille par composant du lot (○ à rechercher, … en cours, ✓ fait).
  - **Tableau de bord** : résumé et barre sur la ligne de la veille en cours ; seule cette ligne se
    ré-interroge (toutes les 10 s), et plus du tout une fois le lancement fini (état final sans
    recharger, pour toute veille lancée).
  - **Telegram** : `get_run_log` renvoie `progress` (synthèse, étape, composant en cours, MPN du lot par
    état) ; `list_veilles` et `get_veille` donnent la synthèse des veilles en cours. Aucun nouvel outil.
  - Étape 2 : l'agent signale chaque groupe de recherches (`veillectl components progress <slug> MPN…`)
    **uniquement lors d'un lancement manuel** (`track_progress` dans la sortie du scan). Une exécution
    planifiée n'écrit rien et ne coûte aucun appel de plus. Le prompt des veilles composants est re-rendu
    au prochain démarrage du conteneur (réconciliation), sans action manuelle.
  - Passage Digi-Key arrêté (erreur de l'API, budget de temps atteint) : signalé en orange, jamais
    affiché comme un succès. Relance de `finalize` après un échec (PDF, e-mail) : les avis trouvés au
    premier passage restent comptés.
  - État dans `runs/.progress.json` (écriture atomique, une par seconde au plus pendant le passage
    Digi-Key, format versionné). Confort d'affichage : une écriture qui échoue, ou un fichier mal formé
    ou d'une autre version, est ignoré ; l'analyse continue.

### Corrigé

- Après un redémarrage du conteneur, un lancement manuel interrompu n'est plus considéré « en cours »
  si son numéro de processus a été réattribué : l'amorçage oublie les `runs/.running` du démarrage
  précédent.

### Mesures

- Passage Digi-Key de 300 références : 2 à 3 écritures de l'état (≈ 0,4 ms chacune), durée inchangée
  (1,57 s avec un faux Digi-Key à 5 ms par appel, avant comme après).
- Suivi du journal toutes les 5 s : ≈ +0,8 ms côté serveur (4,2–4,6 → 4,7–5,9 ms), 230 → 649 octets
  compressés (lot de 40) ; page de la veille et tableau de bord inchangés (≈ 20 ms et ≈ 7 ms).
- Ligne du tableau de bord : ≈ 7 ms, 1,5 Ko, toutes les 10 s pendant un lancement seulement.
- Telegram : réponse de `get_run_log` compacte (1,1 Ko pour un lot de 40, au lieu de 3,5 Ko en détail).
- Signal de l'agent : commande de ≈ 0,15 s, un appel par groupe de recherches (fabricant).

## [0.3.6] — 2026-10-10

Interface plus réactive, notamment par tunnel SSH.

### Modifié

- **Interface plus réactive, notamment par tunnel SSH.** Chaque appel à la CLI Hermes
  (`hermes cron …`) relance tout Hermes Agent (≈ 0,7 s d'imports mesurés sur la v2026.9.24) :
  - page d'une veille : l'historique d'exécution Hermes (`hermes cron runs`) est chargé après la
    page (`/veilles/<slug>/history`) ; la page s'affiche sans l'attendre (0,77 s → 0,03 s
    côté serveur) et « Pause » / « Reprendre » depuis cette page ne paient plus qu'un appel à la
    CLI au lieu de deux (1,5 s → 0,75 s) ;
  - boutons d'action : marqués « en cours » dès le clic (curseur d'attente, bouton désactivé),
    ce qui évite aussi les doubles envois ;
  - fichiers statiques (`htmx.min.js`, CSS, JS) : URL versionnées par empreinte du contenu et mis
    en cache par le navigateur, au lieu d'être retéléchargés à chaque page ; réponses compressées
    (gzip, sauf PDF et Excel) ;
  - formulaire d'une veille : aperçu de la fréquence rendu avec la page (une requête de moins au
    chargement) ;
  - coûts : lecture de `state.db` en une requête indexée au lieu d'une par exécution ;
  - page « Santé » : comptes Brevo et DeepSeek interrogés en parallèle.

## [0.3.5] — 2026-10-09

Interface pleine largeur sur grand écran.

### Modifié

- **Pleine largeur sur PC** : le contenu n'est plus limité à une colonne de 1 200 px centrée ;
  cartes, tableaux et grilles occupent toute la fenêtre (96 % de la largeur à 1 920 et 2 560 px,
  contre 61 % et 46 %). Marge latérale proportionnelle (2 % de la largeur, 16 px au minimum),
  partagée par l'en-tête, le contenu et le pied de page, alignés sur grand écran.
- Le texte courant (paragraphes d'aide, aperçu du brief, avis d'un composant) reste plafonné à
  ~110 caractères par ligne pour rester lisible ; les encadrés (`.flash`) suivent leur bloc.
- Mobile (PWA) inchangé : mêmes marges de 16 px, aucun défilement horizontal.

## [0.3.4] — 2026-10-09

Liste des composants exportable en Excel et jointe aux e-mails ; état d'une analyse à jour sans
recharger la page.

### Ajouté

- **Export Excel de la liste des composants** : bouton « Exporter (Excel) » de la liste
  (`/veilles/<slug>/components.xlsx`). Une ligne par référence, comme dans l'interface : fins de
  vie et dernières commandes en tête et en rouge, filtre automatique, en-tête figé, dates au format
  date, lien cliquable de l'avis le plus critique, statut et dates de vérification Digi-Key / web.
- **Excel joint à l'e-mail périodique**, dans les deux modes de livraison :
  - e-mail dédié de la veille composants : joint à côté du PDF (`veillectl components finalize`) ;
  - chapitre d'une veille thématique : `veillectl components chapter` écrit l'Excel au même
    horodatage que le rapport et affiche `PIECE_JOINTE=<chemin>` ; l'étape « 6 bis » du prompt
    demande à l'agent de l'ajouter (`--attach`) à l'envoi. Le prompt des veilles rattachées est
    re-rendu au prochain démarrage (réconciliation), sans action manuelle.
  - Fichier écrit dans `rapports/` (`<horodatage>-<slug>.xlsx`), hors de la liste des rapports ;
    seul le dernier est gardé (volume des sauvegardes borné).
  - Pièce jointe de confort : si l'export échoue, l'échec est tracé dans le journal d'audit et
    l'e-mail (avis, PDF, chapitre) part quand même, sans l'Excel ni sa mention.

### Corrigé

- **Fin d'une analyse sans rechargement** : le badge « en cours » à côté du titre passe tout seul à
  « actif » (ou « en échec ») et les boutons « Lancer l'analyse » / « Vérifier les statuts » se
  réactivent. Le suivi du journal (toutes les 5 s) renvoie l'en-tête à jour une seule fois, en
  échange hors bande htmx : aucun minuteur ni JS supplémentaire (CSP inchangée). Un lien
  « recharger » signale qu'un nouveau rapport est disponible.
- Suivi du journal d'une veille archivée pendant un lancement : arrêté proprement (réponse 286)
  au lieu d'interroger le serveur indéfiniment.

### Sécurité

- Excel : les textes venus du web (titres d'avis, statuts) sont forcés en texte — une valeur
  « =… » n'est jamais une formule — et purgés des caractères interdits en XML (contrôles,
  U+FFFE/U+FFFF, qui corrompraient le fichier envoyé) ; seuls les liens http(s) validés sont
  cliquables.

## [0.3.3] — 2026-10-09

Adresses d'alerte du chien de garde modifiables dans l'interface, sans toucher au `.env`.

### Ajouté

- **Interface — page « Paramètres »** : adresse(s) des alertes du chien de garde (une à dix, une
  par ligne), avec l'adresse **effectivement utilisée** et son origine (interface, `ALERT_EMAIL`
  du `.env` ou expéditeur Brevo) et un bouton « E-mail de test ». Stockage
  `veilles/.settings.json` (écriture atomique, inclus dans les sauvegardes : pas un secret),
  modifications tracées dans le journal d'audit. Modifiable depuis l'interface **uniquement** :
  ni Telegram ni MCP ne peuvent détourner les alertes de panne.

### Modifié

- **Chien de garde** : destinataires lus sur le volume — interface, sinon `ALERT_EMAIL` du `.env`
  (repli des installations existantes), sinon `BREVO_SENDER_EMAIL`. Chaque contrôle affiche le
  destinataire retenu et son origine. Pris en compte au contrôle suivant, sans réinstallation.
  **Tout changement de destinataires est signalé à l'ancienne ET à la nouvelle adresse** : le
  fichier vit sur le volume, accessible au conteneur, et ne peut donc pas détourner les alertes
  en silence.
- `ALERT_EMAIL` devient facultatif dans le `.env`. L'expéditeur (`BREVO_SENDER_*`) et
  `VEILLE_RECIPIENTS` (amorçage de la première veille) y restent ; les destinataires des rapports
  se règlent toujours dans chaque veille.

### Corrigé

- Lecture des fichiers de réglages JSON (clés d'API, paramètres) factorisée (`store.read_json_dict`).
- `make watchdog-install` : l'e-mail de test du canal d'alerte lit désormais le dossier de données
  réel (`VEILLE_DATA_DIR`) et non `./data` du dépôt.

## [0.3.2] — 2026-10-09

Veille composants lisible avec beaucoup de références, sur PC comme sur mobile ; rapports horodatés.

### Modifié

- **Interface — liste des composants** : les blocs « Fins de vie », « Avis détectés » et
  « Composants suivis » sont remplacés par **une liste unique de tous les composants**, une ligne
  chacun : types d'avis en badges, fins de vie / dernières commandes en rouge et en tête, échéance,
  date de détection, lien vers la source de l'avis le plus critique.
  - **Recherche en temps réel** (mots-clés, sans casse ni accents) sur le MPN, le fabricant, la
    réf. interne, les intitulés, numéros et résumés des avis et le statut Digi-Key.
  - **Filtre par colonne** (composant, fabricant, réf., type d'avis — dont « fin de vie / dernière
    cde », « avec avis », « sans avis » —, échéance, date de détection) et compteur « N / M ».
  - **Clic sur un composant** : le détail se déplie sous la ligne (avis complets, sources, statut et
    fiche Digi-Key, dates de vérification), chargé à la demande ; un échec réseau se réessaie en
    rouvrant la ligne.
  - La recherche porte sur **toute** la liste (plus de plafond à 100 avis) ; 200 lignes au plus sont
    affichées à la fois (« tout afficher » sur demande) pour rester fluide sur mobile avec des
    milliers de références.
  - Mobile (PWA) : plus de « tuile » par avis ; lignes compactes sur deux niveaux (MPN · lien /
    badges · échéance), sans défilement horizontal, colonnes secondaires dans le détail.
  - Un composant retiré de la liste garde ses avis visibles (« retiré ») mais n'est plus compté ni
    affiché comme alerte de fin de vie (comme dans le tableau de bord et le rappel).
  - Filtrage dans `static/app.js` : aucune règle CSP assouplie, aucun script ni style en ligne.
- **PDF et chapitre composants** : un tableau compact **par composant** (types en badges colorés,
  échéance, lien), puis une section « Détail par composant » ; le MPN du tableau est un lien vers
  son détail.
- **E-mail dédié** : même tableau compact (une ligne par composant, intitulé de l'avis le plus
  critique) ; le détail est dans le PDF joint.
- **Rapports horodatés** `AAAA-MM-JJ-HHhMM-veille.{md,pdf}` : relancer une veille le même jour ne
  remplace plus le rapport précédent. Les anciens noms restent lus partout (interface, MCP, CLI,
  chien de garde). Veilles thématiques : le prompt fait calculer l'horodatage une seule fois par
  l'agent (`date +%Y-%m-%d-%Hh%M`) puis le réutiliser.
- MCP : `list_reports` et `read_report` renvoient aussi l'heure (`time`) et l'horodatage (`stamp`) ;
  `read_report(date)` accepte un jour (le plus récent de ce jour) ou un horodatage exact ;
  `get_veille` liste les horodatages des derniers rapports.
- Chien de garde : l'heure du nom départage les rapports d'un même jour.

### Corrigé

- PDF composants : une longue liste n'est plus reportée en bloc à la page suivante (page 1 à moitié
  vide, cadre vide en page 2) ; le tableau se coupe entre deux lignes, en-tête répété.
- Chapitre composants : le jour de livraison est celui du **nom du rapport** et non l'heure de la
  commande ; une exécution commencée avant minuit n'est plus prise pour un échec (avis renvoyés en
  double la fois suivante). Le message d'erreur indique le nom de rapport attendu.

## [0.3.1] — 2026-10-09

Import d'une nomenclature CSV ou Excel dans une veille composants.

### Ajouté

- **Import CSV / Excel** (`.csv`, `.txt`, `.xlsx`, `.xlsm`) dans une veille composants, en création
  comme en édition. Les colonnes MPN, Fabricant et Réf. interne sont **reconnues par règles**
  (dictionnaire de libellés FR / EN, forme des valeurs, sans LLM : rien ne sort du serveur).
  La correspondance est **proposée et modifiable** (feuille, ligne d'en-tête, colonnes, fabricant
  par défaut), avec un aperçu, le décompte des composants valides et des lignes ignorées
  (numérotées). Les lignes de titre au-dessus du tableau sont ignorées. Une colonne de référence
  distributeur (Digi-Key, Mouser…) n'est jamais prise pour le MPN.
- Choix **« Ajouter à la liste »** (défaut, doublons ignorés) ou **« Remplacer la liste »**
  (l'historique des avis est conservé), avec le décompte ajoutés / déjà présents / retirés.
- Interface : carte « Importer un fichier » au-dessus de la liste des composants. Le fichier
  reste sur le serveur (jeton lié à la session, 1 h, 0600) : le navigateur n'échange que la
  correspondance.
- Telegram / MCP : `preview_component_import` (lecture) et `import_components` (modification),
  limités aux fichiers déposés dans `<données>/imports/`. 30 outils (14 en lecture).
- CLI : `veillectl components import <slug> <fichier> [--dry-run] [--replace] [--sheet] [--header-row]
  [--mpn] [--manufacturer] [--ref] [--default-manufacturer]` ; `--components-file` accepte aussi
  un fichier Excel.
- Dépendances : `openpyxl` (lecture Excel) et `defusedxml` (XML sûr).
- Tests : `tests/test_bom_import.py` ; smoke test : import d'un vrai `.xlsx` dans l'image.

### Corrigé

- Modification de la liste des composants (`_edit_components`) : un remplacement à nombre de
  composants égal n'était pas enregistré (seule la taille était comparée).

### Sécurité

- Fichiers importés traités comme non fiables : 5 Mo, 10 000 lignes, 100 colonnes et 200
  caractères par cellule au plus ; archive xlsx contrôlée avant ouverture (bombe zip) ; XML lu
  via defusedxml (entités refusées, testé) ; formules jamais évaluées, macros jamais exécutées.
- MCP : seul un nom de fichier du dossier d'import est accepté (aucun chemin, lien symbolique
  vers l'extérieur refusé). Dossier d'import exclu de `make backup`.

## [0.3.0] — 2026-10-09

Veille composants : détection des avis PCN et des fins de vie (EOL) d'une nomenclature.

### Ajouté

- **Veille composants** (`kind: composants`) : une liste de références « MPN ; Fabricant ; Réf.
  interne » (collage tableur accepté, plus de 300 références prévues), surveillée chaque semaine
  (dimanche 23h par défaut, en heures creuses DeepSeek). Elle réutilise tout le cycle de vie d'une
  veille : job Hermes, pause, lancement, archivage, coûts, rapports PDF, chien de garde.
  - **Statut Digi-Key** (API Product Information v4, OAuth 2-legged, `DIGIKEY_CLIENT_ID` /
    `DIGIKEY_CLIENT_SECRET`) vérifié pour TOUS les composants à chaque exécution : un passage en
    Obsolete, Last Time Buy ou Not For New Designs devient un avis. Débit limité, budget de 900
    appels et 15 minutes par passage ; facultatif.
  - **Recherche web** par l'agent, en **lot tournant** (`batch_size`, 40 par défaut) : jamais
    vérifiés d'abord, puis les références dont le statut Digi-Key a changé, puis les plus anciennes.
  - **Incrémental** : chaque référence garde ses dates de vérification ; un avis déjà connu (même
    référence et même numéro d'avis ou même lien) n'est jamais signalé deux fois, ses nouvelles
    sources sont fusionnées.
  - Le LLM ne fait que chercher et écrire un JSON ; validation, dédoublonnage, persistance
    (`components.json`, `notices.json`), rapport PDF et e-mail sont déterministes
    (`veillectl components scan|finalize`).
- **Chapitre « Composants — PCN & fins de vie »** dans l'e-mail d'une veille thématique
  (`include_components`) : nouveaux avis depuis le précédent envoi, fins de vie en tête dans un
  encadré rouge, rappel de toutes les fins de vie actives, objet préfixé « [EOL ×N] ». Sans
  rattachement, la veille composants envoie son propre e-mail (HTML + PDF).
- Interface : bouton « + Veille composants », formulaire avec la liste des références, page de
  détail avec bandeau **Fins de vie et dernières commandes**, avis filtrables par type (sans
  JavaScript), liens sources, état de chaque référence ; badge et compteur EOL sur le tableau de
  bord.
- MCP : `list_components`, `list_notices` (lecture), `add_components`, `remove_components`,
  `check_component_statuses` (modification) ; `create_veille(kind=…, components_list=…,
  include_components=…)`, `update_veille(include_components=…, batch_size=…)`. 28 outils (13 en
  lecture).
- **Lancement à la demande** : « Lancer l'analyse » d'une veille composants (interface,
  `run_veille_now`) et **« Vérifier les statuts Digi-Key »** (interface, `check_component_statuses`,
  `veillectl components scan --status-only`) : statut de toutes les références en arrière-plan, sans
  LLM ni e-mail, sans toucher au lot de la prochaine exécution planifiée. Les confirmations disent
  ce qui sera réellement envoyé (aucun e-mail pour une veille composants rattachée).
- CLI : `veillectl components list|notices|add|remove|scan [--status-only]|finalize|chapter`,
  `create --kind composants --components-file`, `edit --include-components / --batch-size`.
- **Page « Sources »** de l'interface : saisie des clés d'API Digi-Key avec un test de connexion.
  Les valeurs ne sont jamais réaffichées (aperçu masqué). Stockage dans `veilles/.credentials.json`
  (0600, écriture atomique, exclu de `make backup`), audit sans les valeurs. Les clés du `.env`
  restent prioritaires (lecture seule dans l'interface). Aucune saisie possible par
  Telegram / MCP : `health` indique seulement si une source est configurée.
- Skill `brevo-email` : `--html-file` (corps HTML sans limite de taille de ligne de commande).
- Tests : `tests/test_components.py`, `tests/test_digikey.py` et `tests/test_credentials.py` ; smoke test de la veille
  composants sur l'image (PDF réel, chapitre).

### Modifié

- `veille_core/envfile.py` : lecture unique des secrets (environnement, puis `.env` du volume),
  partagée par DeepSeek, Brevo et Digi-Key (trois copies auparavant).

### Sécurité

- Les avis trouvés sur le web sont des données non fiables : schéma strict, liens `http(s)`
  uniquement (ni `javascript:`, ni identifiants dans l'URL), tailles bornées, échappement
  systématique dans le Markdown du PDF (pas de HTML brut transmis à pandoc / WeasyPrint), dans
  l'e-mail HTML et dans l'interface. Les secrets Digi-Key ne figurent jamais dans les journaux.
- Sauvegardes : les clés saisies dans l'interface en sont exclues, comme le `.env`.

## [0.2.7] — 2026-10-09

Données persistantes à travers les mises à jour, garanties et vérifiées.

### Ajouté

- **`VEILLE_DATA_DIR`** : dossier des données sur l'hôte, monté sur `/opt/data`. Il est
  recommandé hors du dépôt git (`/srv/veille-hermes/data`), pour qu'aucune mise à jour (re-clone,
  `git clean -x`) ne puisse l'atteindre. Le défaut `./data` est conservé, donc rien ne change
  pour une installation existante.
- **`make move-data DEST=…`** (`scripts/move-data.sh`) sort les données du dépôt sans rien
  perdre : renommage, ou copie puis original renommé (jamais supprimé), réglage dans `.env`,
  redéploiement vérifié, réinstallation du chien de garde.
- `scripts/data-dir.sh` : résolution unique du dossier de données côté hôte, partagée par le
  déploiement, la sauvegarde, le chien de garde et le serveur MCP.
- `scripts/backup.sh` (`make backup`) : archives rangées à côté des données, réservées au
  propriétaire (`umask 077`).
- Interface : nombre de rapports sur le tableau de bord, avec un lien vers l'historique.
- Tests : `tests/test_persistence.py` (32 tests : scripts de l'hôte exécutés avec un faux
  `docker`), réconciliation sur un `jobs.json` corrompu, historique des rapports dans
  l'interface. Le test d'intégration simule une **mise à jour** (conteneur supprimé puis recréé :
  veilles, jobs, rapports et audit identiques) et vérifie le refus de démarrer sans dossier de
  données.

### Modifié

- **`deploy.sh` / `make update`**, déploiement sûr :
  - refus de démarrer si le dossier de données est absent (`--init` pour une première
    installation) ;
  - **sauvegarde automatique** conteneur arrêté ;
  - attente de l'amorçage de *ce* démarrage (et non plus du marqueur du premier) ;
  - vérification que le conteneur monte bien le dossier attendu (`docker inspect`) ;
  - **inventaire avant et après** : le déploiement échoue s'il manque une veille ou un rapport ;
  - code de sortie non nul si l'amorçage échoue, si la réconciliation signale une erreur, si
    `veillectl` ou l'interface ne répondent pas (auparavant : message, mais succès).
- `docker-compose.yml` : volume en syntaxe longue avec `create_host_path: false`. Un dossier
  absent fait échouer le démarrage au lieu de repartir d'un état vide en silence.
- Page d'une veille : l'historique des rapports passe **en tête**, avant le coût des exécutions,
  avec le nombre de rapports et une présentation en cartes sur mobile.

### Corrigé

- Réconciliation au démarrage et modification d'une veille : un `cron/jobs.json` **illisible**
  (droits, contenu corrompu) n'est plus pris pour une absence de jobs. Auparavant, chaque job
  était recréé : nouveaux identifiants, historique d'exécution et coûts orphelins, envois en
  double. Désormais, rien n'est modifié et l'erreur est signalée. Même correctif pour
  l'archivage (le job n'était pas supprimé et continuait d'envoyer des e-mails) et la migration
  de l'ancienne disposition.

## [0.2.6] — 2026-10-05

Consommation de tokens et coût réel de chaque rapport.

### Ajouté

- **Coût des exécutions**, calculé à partir des tokens que Hermes enregistre par exécution
  (`state.db`, une session `cron_<job>_<date>` par run, détail par modèle, tâches auxiliaires
  comprises). La lecture se fait en lecture seule, dans `veille_core/hermes.py` (`run_usage`).
  - Tarifs DeepSeek dans `veille/pricing.json` (surchargeable dans le volume), **heures pleines
    comprises** : lun–ven 01h–04h et 06h–10h UTC, tarif ×2. L'estimation de Hermes n'applique que
    le tarif creux et sous-estimait donc de moitié le rapport du lundi 8h.
  - Interface : section « Coût des exécutions » par veille (durée, tokens, heure pleine ou creuse,
    coût, totaux du mois et des 30 jours) ; page Santé : **solde DeepSeek** (`GET /user/balance`)
    et coût du mois par veille.
  - Telegram : outil MCP **`get_costs`** (lecture seule, 23 outils au total). `health` ajoute le
    solde et le coût du mois, et `preview_schedule` signale les heures pleines.
  - CLI : `veillectl costs [slug] [--days N]`.
- Aperçu de fréquence (interface et MCP) : avertissement « heure pleine DeepSeek (tarif ×2) »,
  avec les créneaux creux conseillés.
- Smoke test : lecture d'une session créée par Hermes lui-même, pour vérifier le contrat avec son
  schéma de base.

### Modifié

- Veille par défaut d'une installation neuve : **lundi 6h30** (heures creuses), au lieu de 8h.
- Calculs de planification dans un fuseau explicite (`Europe/Paris`, variable `VEILLE_TZ`), au lieu
  du fuseau du processus.

## [0.2.5] — 2026-10-05

Outils de développement à jour (reprend la PR Dependabot groupée #15).

### Modifié

- `requirements-dev.txt` : `pytest-cov` 7.0.0 → 7.1.0, `ruff` 0.15.10 → 0.16.10,
  `pip-audit` 2.10.0 → 2.10.1 ; même version de `pip-audit` dans le job d'audit de la CI.
  Aucun effet sur l'image ni sur le serveur.

## [0.2.4] — 2026-10-05

Mise à jour des actions GitHub et Dependabot groupé.

### Modifié

- Actions GitHub en dernière version majeure, toutes sur Node 24 (fin des avertissements de
  dépréciation de Node 20) : `actions/checkout` 4 → 7, `actions/setup-python` 5 → 7,
  `actions/upload-artifact` 4 → 7, `docker/setup-buildx-action` 3 → 4,
  `docker/metadata-action` 5 → 6, `docker/build-push-action` 6 → 7, `docker/login-action`
  3 → 4, `hadolint/hadolint-action` 3.1 → 3.5. Reprend les PR Dependabot #2, #3, #4, #7 et #9.
  hadolint 2.15 (embarqué par l'action) ajoute la règle DL3066 : exception justifiée pour le
  `USER root` voulu (même raison que DL3002).
- Dependabot : **une PR groupée par écosystème et par mois** (au lieu d'une PR par paquet).
  Ces PR servent de signal : leur contenu est repris dans la prochaine branche
  `release/vX.Y.Z`, car chaque merge sur `main` doit incrémenter la version.

## [0.2.3] — 2026-10-05

Piloter les veilles depuis Hermes (Telegram) : serveur MCP.

### Ajouté

- **Serveur MCP `veille-mcp`** (`veille_mcp/`, SDK `mcp` 2.3, transport stdio) : 22 outils.
  - Lecture (annotés `readOnlyHint`) : `list_veilles`, `get_veille`, `get_brief`,
    `list_brief_templates`, `list_reports`, `read_report` (Markdown d'un rapport, pour le
    résumer), `get_run_log`, `run_history`, `preview_schedule`, `health`.
  - Modification : `create_veille`, `update_veille`, `set_recipients`, `add_recipients`,
    `remove_recipients`, `update_brief`, `edit_brief` (remplacement ciblé), `pause_veille`,
    `resume_veille`, `run_veille_now`, `send_test_email`, `archive_veille` (`destructiveHint`,
    exige de répéter le slug).
  - Fréquences en langage clair (`frequency`, `time`, `day_of_week` « lundi »…, `day_of_month`
    ou `cron`) ; erreurs métier renvoyées proprement à l'agent ; actions tracées dans le journal
    d'audit avec leur origine (`VEILLE_MCP_ACTOR`, ex. « telegram ») ; chemins hôte des PDF
    (`VEILLE_HOST_DATA`) pour les envoyer en pièce jointe ; mode `--readonly`.
- `scripts/install-mcp-host.sh` (`make mcp-install`) : déclare le serveur dans le Hermes de
  l'hôte (`docker exec -i`, **aucun port ouvert**, `trust: untrusted` : toute modification
  demande une approbation dans Telegram), puis teste la connexion avec `hermes mcp test`.
- Tests : 14 tests à travers le vrai protocole MCP (client en mémoire et sous-processus stdio) ;
  smoke test avec le **client MCP de Hermes lui-même** (`hermes mcp test` : 22 outils) ;
  test d'intégration via `docker exec -i` (lecture, pause/reprise effectives, confirmation
  d'archivage, audit « telegram »).

## [0.2.2] — 2026-10-05

Suite de tests GitHub et gestion professionnelle des versions.

### Ajouté

- Workflow **`ci`** (push `main` / `release/**`, PR) : cohérence de la version, ruff,
  shellcheck, hadolint, `docker compose config`, contrôle « ports sur 127.0.0.1 », pytest sur
  Python 3.11 / 3.12 / 3.13 avec couverture minimale de 75 % (résumé et rapports JUnit/XML en
  artefacts), audit des dépendances (`pip-audit`).
- Workflow **`release`** : à chaque merge sur `main`, étiquette `vX.Y.Z` et release GitHub
  avec la section du CHANGELOG, posées sur le commit où la version est arrivée sur `main` ;
  rattrape les versions encore sans étiquette (dont la 0.2.1).
- `scripts/integration-test.sh` (`make integration`) : conteneur complet (s6, gateway, seed,
  interface) sur un volume v0.2 — migration (même job, historique, lien de compatibilité),
  interface (session, CSRF, Host, pause/reprise effectives côté Hermes, version affichée),
  redémarrage sans doublon, chien de garde.
- `scripts/check-version.sh`, `scripts/bump-version.sh` (`make bump`, `make version`),
  `requirements-dev.txt`, `CONTRIBUTING.md` (versions, branches `release/vX.Y.Z`, tests),
  modèle de PR, configuration hadolint justifiée.

### Modifié

- Workflow `build` renommé **`image`** : build → smoke test → test d'intégration → push ; les
  images sont étiquetées avec leur version (`X.Y.Z`) et portent le label OCI `version`.
- `make test` mesure la couverture ; `make lint` inclut hadolint s'il est installé.

### Corrigé

- CI : le nom d'image est mis en minuscules (le propriétaire du dépôt, « Aquadrox », produisait
  une étiquette Docker invalide).

### Sécurité

- Dépendances de l'application mises à jour : **Starlette 0.48 → 1.7** (5 avis de sécurité dont
  CVE-2026-48710, signalés par `pip-audit`), FastAPI 0.142, Uvicorn 0.54, python-multipart 0.0.32.

## [0.2.1] — 2026-10-05

Multi-veilles et interface web de gestion. Migration automatique depuis la v0.2.

### Ajouté

- **Plusieurs veilles** : chacune dans `data/veilles/<slug>/` (`veille.json`, brief `config.md`,
  `recipients.txt`, rapports), avec sa fréquence, ses destinataires et sa mise en forme PDF.
- `veille_core/` — cœur partagé : stockage, fréquences (préréglages ↔ cron, aperçu, seuil de
  fraîcheur), rendu du prompt, **adaptateur unique** vers `hermes cron`, opérations métier
  (création, modification, pause, reprise, lancement, archivage, réconciliation) et journal d'audit.
- `veillectl` — CLI de gestion des veilles dans le conteneur.
- Fichier `VERSION` (source unique du numéro de version) : affiché par `veillectl --version`
  et en pied de page de l'interface, copié dans l'image.
- **Interface web** `veille_ui/` (FastAPI + Jinja + HTMX) : tableau de bord, création et édition
  (fréquence avec aperçu, destinataires, brief avec aperçu, PDF), lancement immédiat avec journal
  en direct, rapports, historique, e-mail de test, archivage, page santé (crédits Brevo, audit).
  Publiée **sur 127.0.0.1 uniquement** (accès par tunnel SSH), mot de passe argon2, session
  signée, CSRF, anti-force brute, contrôle de l'en-tête `Host`, CSP stricte. Service s6 dédié :
  une panne de l'interface n'affecte pas le gateway.
- Gabarit générique du prompt (`veille/job-prompt.tmpl.md`) et modèles de brief
  (`veille/templates/industrie-4-0.md`, `veille/templates/vierge.md`).
- `brevo_send.py` : plusieurs destinataires par fichier, `--bcc` / `--bcc-file` (les
  destinataires ne se voient pas), `--env-file`.
- Tests unitaires (pytest, faux binaire `hermes`) : cœur, migration, watchdog, skills, interface.
- CI : lint (ruff, shellcheck), tests, contrôle « port publié sur 127.0.0.1 uniquement », puis
  build → **smoke test** → push (plus de publication d'une image non testée). Dependabot.
- Smoke test étendu : contrat de la CLI `hermes cron`, cycle de vie `veillectl` contre le vrai
  Hermes, démarrage de l'interface.
- Cibles `make veilles`, `make run-now VEILLE=<slug>`, `make ui-password`, `make tunnel`,
  `make backup`, `make test`, `make lint`.

### Modifié

- **Image amont épinglée** (`nousresearch/hermes-agent:v2026.9.24`) au lieu de `latest`.
- Hook `50-seed-veille` : s'exécute à **chaque** démarrage (migration, amorçage, réconciliation).
  Le job ne contient plus qu'un prompt court renvoyant vers `job-prompt.md`, re-rendu au boot :
  une nouvelle image propage ses corrections de prompt sans recréer les jobs. Auparavant, le
  texte du prompt était figé dans le job à sa création.
- Modèle et fournisseur configurables (`VEILLE_MODEL`, `VEILLE_PROVIDER`).
- **Chien de garde multi-veilles** : seuil de fraîcheur propre à chaque veille, veilles en pause
  ignorées, lecture directe de `cron/jobs.json`, alertes à `ALERT_EMAIL` (et non plus au
  destinataire du rapport), envoi avec le skill et le `.env` **du dépôt** : plus aucune
  dépendance à une installation Hermes sur l'hôte.
- `build_report.py` : répertoire temporaire supprimé après usage, fichiers correctement fermés.

### Corrigé

- Surveillance : `install-watchdog.sh` ne réécrivait que le chemin du script, pas celui du
  volume. Installé ailleurs que dans `/root/veille-hermes`, le chien de garde surveillait un
  volume inexistant.
- `brevo_send.py --to-file` ignorait toutes les adresses après la première.
- `make run-now` prenait le premier identifiant de job trouvé (faux dès deux jobs).

### Supprimé

- `veille/destinataire.txt` (adresse personnelle versionnée) : les destinataires vivent dans le
  volume, et la veille par défaut lit `VEILLE_RECIPIENTS`.

### Migration depuis la v0.2

Automatique au premier démarrage : `data/veille/` → `data/veilles/industrie-4-0/`, historique et
job conservés (édité, pas recréé), lien de compatibilité `data/veille`. Annulée proprement si
l'édition du job échoue. Côté hôte : ajouter `ALERT_EMAIL` au `.env`, puis `make watchdog-install`.

### Bascule (avant 0.2.1)

#### Modifié

- **Bascule effectuée** : le job de veille de l'hôte est mis en pause ; le conteneur est désormais
  la seule source de production. L'historique des rapports (3 semaines) a été migré dans le volume
  (`/opt/data/veille/rapports/`) afin de préserver la contrainte anti-répétition.

#### Ajouté

- Cible `make update` — `git pull` puis reconstruction et redéploiement : le cycle de mise à jour.
- **Surveillance (chien de garde)** — `scripts/watchdog.py`, `scripts/install-watchdog.sh` et un
  timer systemd quotidien (10h) **sur l'hôte** : détecte les pannes silencieuses (conteneur
  arrêté, rapport trop ancien ou tronqué, dernier run en échec) et prévient par e-mail, avec
  e-mail de rétablissement et rappel tous les 3 jours. Déterministe (aucun LLM) et volontairement
  placé **hors** du conteneur : un contrôleur interne ne pourrait signaler ni un conteneur arrêté
  ni une clé d'API expirée.
- Cibles `make watchdog-install` et `make watchdog-check`.

## [0.2.0] — 2026-10-05

Conteneurisation : la veille devient une image Docker dérivée, déployable et mettable à jour par
`pull` + recréation.

### Ajouté

- `Dockerfile` — image dérivée de `nousresearch/hermes-agent` : dépendances système
  (`pandoc`, `weasyprint`, `poppler-utils`, polices), bakage des skills et des assets par défaut.
- `docker-compose.yml` — service `veille`, volume `./data:/opt/data`, secrets par `env_file`,
  `TZ=Europe/Paris`, limites mémoire/CPU, rotation des logs.
- `docker/cont-init.d/50-seed-veille` — amorçage du **premier démarrage** : création des
  répertoires, seed des assets (sans écrasement), réglage du modèle et du fuseau, création
  **idempotente** du job cron.
- `veille/job-prompt.md` — prompt du job adapté aux chemins du conteneur (`/opt/data/...`).
- `scripts/deploy.sh` — déploiement en une commande : pré-vol, build, démarrage, attente de
  l'amorçage, vérifications.
- `scripts/smoke-test.sh` — contrôle dans l'image des dépendances, des skills et de la chaîne
  PDF complète (Markdown → PDF).
- `Makefile` — raccourcis d'exploitation (`build`, `up`, `logs`, `run-now`, `verify`, `smoke`…).
- `.github/workflows/build.yml` — construction et publication de l'image sur `ghcr.io`
  (à chaque push sur `main`, à chaque étiquette `v*`, et chaque lundi).

### Modifié

- `README.md` — reflète l'état implémenté et la procédure de déploiement.

### Corrigé

- Image dérivée : conserver `root` comme utilisateur final (et non `hermes`), sinon `/init`
  (s6-overlay) ne peut pas prendre possession du volume `/opt/data` et le conteneur boucle au
  démarrage (`chown /opt/data failed`, puis `Permission denied`).
- Hook de seed : créer les répertoires **en tant que `hermes`** — sinon ils appartiennent à root
  et les copies d'assets échouent silencieusement en permission refusée.

## [0.1.0] — 2026-10-05

Première version : import dans un dépôt versionné du système de veille technologique
jusqu'ici hébergé « à plat » sur le VPS (Hermes installé en git, gateway sous systemd).

### Ajouté

- `veille/config.md` — brief de mission éditable : rôle, cadre temporel, anti-répétition,
  règles de fiabilité, structure du rapport, conventions de mise en forme, couverture, ton.
- `veille/destinataire.txt` — destinataire e-mail par défaut.
- Skill `skills/email/brevo-email/` — envoi d'e-mail via l'API transactionnelle Brevo
  (pièces jointes, vérification de livraison via l'API d'événements).
- Skill `skills/productivity/styled-pdf-report/` — génération de rapports PDF stylés à partir de
  Markdown enrichi (pandoc + WeasyPrint + graphiques SVG, sans dépendance Python).

### Historique (avant versionnement)

Fonctionnalités déjà opérationnelles sur le VPS et importées telles quelles :

- **2026-09-18** — Mise en place du job cron hebdomadaire « Veille technologique »
  (lundi 8h, heure de Paris) et de son archivage hebdomadaire des rapports.
- **2026-09-18** — Chaîne PDF (`pandoc` + `weasyprint`) et envoi e-mail (API Brevo,
  expéditeur validé) rendues opérationnelles de bout en bout.
- **2026-09-18** — Refonte du format du rapport : badges de maturité / confiance / type de
  source, fiches item standardisées (Fait · Pourquoi ça compte · Maturité · Confiance · Source),
  encadrés « À retenir » / « Point de vigilance », tableaux comparatifs, frise de la semaine,
  glossaire, sources numérotées en hyperliens, bandeau de chiffres clés et graphiques.
- **2026-09-21** — Mesure comparative des architectures de génération : modèle « pro » seul
  contre architecture hybride « flash → revue pro ».

### Notes

- Les secrets (`BREVO_API_KEY`, `DEEPSEEK_API_KEY`, …) ne sont **jamais** versionnés —
  voir `.env.example`.
- L'image Docker n'existe pas encore à ce stade ; la démarche est décrite dans
  `docs/PLAN-CONTAINERISATION.md`.
