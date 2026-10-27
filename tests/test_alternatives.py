"""Fiche composant et alternatives : lecture de la fiche Digi-Key, recherche paramétrique, classement, critères
choisis par l'utilisateur, passage automatique après le scan, recherche à la demande (service, CLI, MCP,
interface). Le réseau (Digi-Key) est simulé."""
import json
import os
import re

import pytest
from test_components import FakeClient, tools  # noqa: F401 - fixtures partagées
from test_digikey import TOKEN, Fake, found, product
from test_progress import manual

from veille_core import catalog, cli, digikey, progress, service, store
from veille_core import components as comp

RES_CAT = [{"id": "2", "name": "Resistors"}, {"id": "52", "name": "Chip Resistor - Surface Mount"}]
IC_CAT = [{"id": "32", "name": "Integrated Circuits (ICs)"}, {"id": "699", "name": "PMIC - Voltage Regulators"}]


def prm(pid, name, value, vid=None):
    return {"id": pid, "name": name, "value": value, "vid": vid if vid is not None else value.lower()}


R10K = [prm(1, "Resistance", "10 kOhms"), prm(2, "Tolerance", "±1%"), prm(3, "Power (Watts)", "0.1W, 1/10W"),
        prm(4, "Composition", "Thick Film"), prm(5, "Package / Case", "0603 (1608 Metric)"),
        prm(6, "Temperature Coefficient", "±100ppm/°C"), prm(7, "Features", "-", "")]


def res(mpn, *changes, status="Active", mfr="YAGEO", url=None):
    """Fiche normalisée d'une résistance (sortie de Client.similar) : R10K avec `changes` (id, valeur)."""
    params = [dict(p) for p in R10K]
    for pid, value in changes:
        for p in params:
            if p["id"] == pid:
                p.update(value=value, vid=value.lower())
    return {"mpn": mpn, "manufacturer": mfr, "status": status, "product_url": url or f"https://www.digikey.com/p/{mpn}",
            "description": f"RES 10K {mpn}", "category": RES_CAT, "params": params}


def spec(params=R10K, category=RES_CAT):
    return {"category": category, "params": params}


def comp_with(mpn="RC0603FR-0710KL", **kw):
    return {"mpn": mpn, "manufacturer": "YAGEO", "dk": {"status": "Last Time Buy"}, **kw}


class AltClient(FakeClient):
    """Faux client Digi-Key : statuts (FakeClient) et recherches paramétriques enregistrées."""

    def __init__(self, answers, similar=None, error=None):
        super().__init__(answers)
        self.similar_answers, self.error, self.searches = similar or {}, error, []

    def similar(self, category_id, filters):
        self.calls += 1
        self.searches.append((category_id, sorted(filters)))
        if self.error:
            raise self.error
        return self.similar_answers.get(category_id, [])


BOM = "RC0603FR-0710KL;YAGEO\nLM317T;Texas Instruments\nGRM188R71H104KA93D;Murata\n"
LTB = {"RC0603FR-0710KL": {"status": "Last Time Buy", "product_url": "https://www.digikey.com/p/r",
                           "category": RES_CAT, "params": R10K},
       "LM317T": {"status": "Obsolete", "product_url": "https://www.digikey.com/p/lm", "category": IC_CAT,
                  "params": [prm(9, "Voltage - Output", "1.2 V ~ 37 V")]}}


@pytest.fixture
def bom(tools):  # noqa: F811
    return service.create_veille(name="Carte R", subject="BOM", schedule_expr=service.COMPONENTS_SCHEDULE,
                                 recipients=["achats@example.org"], brief=store.brief_templates()["composants"],
                                 kind="composants", components=comp.parse_components(BOM), batch_size=2)


def stored(slug, mpn):
    return next(c for c in comp.load_components(slug)["components"] if c["mpn"] == mpn)


# --------------------------------------------------------------------------
# Client Digi-Key : fiche et recherche paramétrique
# --------------------------------------------------------------------------
def dk_product(mpn="RC0603FR-0710KL", status="Last Time Buy", **extra):
    return {**product(mpn=mpn, mfr="YAGEO", status=status), **extra}


def test_status_reads_category_and_parameters_bounded():
    p = dk_product(Category={"CategoryId": 2, "Name": "Resistors", "ChildCategories": [
        {"CategoryId": 52, "Name": " Chip  Resistor - Surface Mount ", "ChildCategories": []}]},
        Parameters=[{"ParameterId": 2085, "ParameterText": "Resistance", "ValueId": "10k", "ValueText": "10 kOhms"},
                    {"ParameterId": True, "ParameterText": "Bool", "ValueText": "x"},       # identifiant non entier
                    {"ParameterId": "7", "ParameterText": "Texte", "ValueText": "x"},
                    {"ParameterId": 3, "ParameterText": "", "ValueText": "vide"},          # sans libellé
                    "pas un objet",
                    {"ParameterId": 4, "ParameterText": "Long", "ValueText": "v" * 500}])
    out = Fake(TOKEN, found(p)).client().status("RC0603FR-0710KL", "YAGEO")
    assert out["category"] == [{"id": "2", "name": "Resistors"}, {"id": "52", "name": "Chip Resistor - Surface Mount"}]
    assert out["params"][0] == {"id": 2085, "name": "Resistance", "value": "10 kOhms", "vid": "10k"}
    assert [p["id"] for p in out["params"]] == [2085, 4] and len(out["params"][1]["value"]) == 120
    many = dk_product(Parameters=[{"ParameterId": i, "ParameterText": f"P{i}", "ValueText": "v"} for i in range(99)])
    assert len(Fake(TOKEN, found(many)).client().status("RC0603FR-0710KL", "YAGEO")["params"]) == digikey.MAX_PARAMS
    # fiche sans catégorie ni caractéristiques : contrat inchangé
    assert Fake(TOKEN, found(dk_product())).client().status("RC0603FR-0710KL", "YAGEO") == {
        "status": "Last Time Buy", "product_url": "https://www.digikey.com/p/1"}


def test_similar_request_and_parsing():
    alt = {**product(mpn="ERJ-3EKF1002V", mfr="Panasonic", status="Active", url="javascript:alert(1)"),
           "Description": {"ProductDescription": "RES  10K\n1% 0603"},
           "Parameters": [{"ParameterId": 1, "ParameterText": "Resistance", "ValueId": "10k", "ValueText": "10 kOhms"}]}
    f = Fake(TOKEN, {"Products": [alt, "x"]})
    out = f.client().similar("52", [(1, "10k"), (5, "0603")])
    body = json.loads(f.requests[1].data)
    assert body["Keywords"] == "" and body["FilterOptionsRequest"]["CategoryFilter"] == [{"Id": "52"}]
    pf = body["FilterOptionsRequest"]["ParameterFilterRequest"]
    assert pf == {"CategoryFilter": {"Id": "52"}, "ParameterFilters": [
        {"ParameterId": 1, "FilterValues": [{"Id": "10k"}]}, {"ParameterId": 5, "FilterValues": [{"Id": "0603"}]}]}
    assert out == [{"mpn": "ERJ-3EKF1002V", "manufacturer": "Panasonic", "status": "Active", "product_url": None,
                    "description": "RES 10K 1% 0603",
                    "params": [{"id": 1, "name": "Resistance", "value": "10 kOhms", "vid": "10k"}]}]
    assert Fake(TOKEN, {"Products": "pas une liste"}).client().similar("52", []) == []


def test_similar_errors_name_the_search():
    from test_digikey import http_error
    with pytest.raises(digikey.TransientError, match=r"HTTP 503 \(alternatives, catégorie 52\)"):
        Fake(TOKEN, http_error(503)).client().similar("52", [])
    with pytest.raises(digikey.DigiKeyError, match="HTTP 400") as e:
        Fake(TOKEN, http_error(400)).client().similar("52", [])
    assert not isinstance(e.value, digikey.TransientError)


# --------------------------------------------------------------------------
# Fonctions pures
# --------------------------------------------------------------------------
def test_family_and_default_criteria():
    sp = spec()
    assert comp.family(sp) == "Résistance" and comp.leaf_category(sp) == "52" and comp.searchable(comp_with(), sp) == ""
    assert [p["name"] for p in comp.criteria(comp_with(), sp)] == ["Resistance", "Package / Case"]  # défauts présents
    ic = spec(category=IC_CAT)
    assert comp.family(ic) is None and "famille non concernée" in comp.searchable(comp_with(), ic)
    assert "pas de fiche" in comp.searchable(comp_with(), None) and "pas de fiche" in comp.searchable(comp_with(), {})
    # aucune critique : toute référence active de la catégorie passerait (une 1 MΩ pour une 10 kΩ), refusé
    assert "aucune caractéristique critique" in comp.searchable(comp_with(critical_params=[]), sp)
    ferrite = spec(category=[{"id": "9", "name": "Filters"}, {"id": "841", "name": "Ferrite Beads and Chips"}])
    assert comp.family(ferrite) == "Ferrite"
    # fiche retouchée à la main : entrées mal formées ignorées, jamais d'exception
    broken = {"category": "x", "params": [{"id": "1", "name": "x", "value": "y"}, "z", prm(5, "Package / Case", "0603")]}
    assert [p["id"] for p in comp.params(broken)] == [5] and comp.family(broken) is None
    assert comp.params({"params": "x"}) == [] and comp.params("x") == []


def test_user_criteria_replace_defaults_and_validation():
    c = comp_with(critical_params=[2, 7, 99])  # 7 sans valeur et 99 absent : ignorés
    assert [p["name"] for p in comp.criteria(c, spec())] == ["Tolerance"]
    assert comp.criteria(comp_with(critical_params=[]), spec()) == []  # choix explicite : aucune critique
    assert comp.critical_ids(c, spec(), ["resistance", 5, "2", " Tolerance "]) == [1, 2, 5]
    with pytest.raises(comp.ComponentsError, match="Features"):
        comp.critical_ids(c, spec(), ["Features"])  # « - » : sans valeur
    with pytest.raises(comp.ComponentsError, match="inconnue"):
        comp.critical_ids(c, spec(), [42, True])
    with pytest.raises(comp.ComponentsError, match="inconnue"):
        comp.critical_ids(c, None, ["Resistance"])  # pas de fiche


def test_rank_alternatives_filters_scores_and_bounds():
    c, sp = comp_with(), spec()
    crit = comp.criteria(c, sp)
    products = [res("RC0603FR-0710KL"),                          # la référence elle-même (autre graphie : même MPN)
                res("RC0603FR-0710KL-OLD", status="Obsolete"),   # pas active
                res("CRCW060310K0FKEA", (5, "0805 (2012 Metric)")),  # critique différente : filtre ignoré par l'API
                res("ERJ-3EKF1002V", (2, "±5%"), mfr="Panasonic"),
                res("ERJ-3EKF1002V", mfr="Panasonic"),           # doublon (même fabricant et MPN)
                res("RMCF0603FT10K0", url="https://www.seielect.com/x"),
                res("AC0603FR-0710KL", (2, "±5%"), (4, "Thin Film"), (6, "±50ppm/°C"))]
    out = comp.rank_alternatives(c, sp, products, crit)
    assert [a["mpn"] for a in out] == ["RMCF0603FT10K0", "ERJ-3EKF1002V", "AC0603FR-0710KL"]
    assert out[0]["score"] == 100 and out[0]["diffs"] == []
    assert out[1]["score"] == 75 and out[1]["diffs"] == [{"name": "Tolerance", "ref": "±1%", "alt": "±5%"}]
    assert out[2]["score"] == 25
    # au plus MAX_ALTERNATIVES, une caractéristique absente de l'alternative est une différence
    many = [res(f"ALT-{i}") for i in range(9)] + [{**res("NOPARAM"), "params": []}]
    assert len(comp.rank_alternatives(c, sp, many, crit)) == comp.MAX_ALTERNATIVES
    assert comp.rank_alternatives(c, sp, [{**res("NOPARAM"), "params": []}], [])[0]["diffs"][0]["alt"] == "—"


def test_alternatives_due_freshness_and_stale_criteria():
    day = "2026-10-26"
    rec = {"searched_at": day, "criteria": [{"id": 1}, {"id": 5}], "items": []}

    def c(mpn, **kw):
        x = comp_with(mpn=mpn)
        x["dk"].update(kw.pop("dk", {}))
        return {**x, **kw}
    notices = [comp.status_notice({"mpn": m, "manufacturer": "YAGEO"}, t, "x", "https://dk/x", "dk", "digikey")
               for m, t in (("NEW", "LTB"), ("FRESH", "OBSOLETE"), ("OLD", "EOL"), ("STALE", "LTB"),
                            ("NRND", "NRND"), ("IC", "EOL"))]
    for n in notices:
        n["found_at"] = day
    comps = [c("ACTIVE"), c("NEW"), c("FRESH", dk={"alternatives": rec}),
             c("OLD", dk={"alternatives": {**rec, "searched_at": "2026-09-01"}}),
             c("STALE", dk={"alternatives": rec}, critical_params=[1]), c("NRND"), c("IC"), c("NOSPEC")]
    notices.append({**notices[0], "key": comp.key_of("NOSPEC", "YAGEO"), "mpn": "NOSPEC"})
    specs = {comp.key_of(x["mpn"], "YAGEO"): spec() for x in comps if x["mpn"] != "NOSPEC"}
    specs[comp.key_of("IC", "YAGEO")] = spec(category=IC_CAT)
    due = comp.alternatives_due(comps, specs, notices, day)
    assert [x["mpn"] for x in due] == ["NEW", "OLD", "STALE"]  # jamais recherchée d'abord, puis la plus ancienne
    assert comp.alternatives_stale(comps[4], spec()) and not comp.alternatives_stale(comps[2], spec())
    assert not comp.alternatives_stale(comps[4], None)  # fiche pas encore relue : rien à comparer
    sheet = comp.sheet(comps[4], spec())
    assert sheet["custom"] and sheet["stale"] and [p["critical"] for p in sheet["params"]][:2] == [True, False]
    assert sheet["category"] == "Resistors › Chip Resistor - Surface Mount" and not sheet["params"][-1]["valued"]


def test_reset_keeps_user_choice_but_forgets_results():
    c = {"mpn": "R1", "manufacturer": "YAGEO", "added_at": "2026-01-01", "dk": {"alternatives": {}},
         "critical_params": [1]}
    assert comp.reset_state([c]) == [{"mpn": "R1", "manufacturer": "YAGEO", "added_at": "2026-01-01",
                                      "web_checked_at": None, "dk": None, "critical_params": [1]}]
    assert "critical_params" not in comp.reset_state([{**c, "critical_params": None}])[0]
    kept = comp.replace_components([c], [{"mpn": "R1", "manufacturer": "Yageo"}])[0]
    assert kept["critical_params"] == [1] and kept["dk"] == {"alternatives": {}}


def test_find_tracked_reference():
    comps = [{"mpn": "R1", "manufacturer": "YAGEO"}, {"mpn": "R2", "manufacturer": "YAGEO"},
             {"mpn": "R2", "manufacturer": "Vishay"}]
    assert comp.find(comps, "r1")["mpn"] == "R1"
    assert comp.find(comps, "R2", "Vishay Dale")["manufacturer"] == "Vishay"
    with pytest.raises(comp.ComponentsError, match="plusieurs fabricants"):
        comp.find(comps, "R2")
    with pytest.raises(comp.ComponentsError, match="non suivie"):
        comp.find(comps, "R3")


# --------------------------------------------------------------------------
# Service : passage automatique après le scan, recherche à la demande, critiques
# --------------------------------------------------------------------------
def test_scan_stores_sheet_and_searches_alternatives_of_end_of_life_passives(bom):
    client = AltClient(LTB, similar={"52": [res("ERJ-3EKF1002V", (2, "±5%"), mfr="Panasonic"),
                                             res("RMCF0603FT10K0")]})
    out = service.components_scan(bom.slug, client=client)
    # seule la résistance en dernière commande est recherchée : le régulateur (CI) obsolète n'est pas concerné,
    # le condensateur est actif ; recherche filtrée sur les critiques par défaut (valeur et boîtier)
    assert client.searches == [("52", [(1, "10 kohms"), (5, "0603 (1608 metric)")])]
    assert out["digikey"]["alternatives"] == {"searched": 1, "found": 1, "pending": 0}
    r = stored(bom.slug, "RC0603FR-0710KL")
    key = comp.key_of("RC0603FR-0710KL", "YAGEO")
    # fiche à part (specs.json) : components.json, relu par la liste et le tableau de bord, reste léger
    assert comp.load_specs(bom.slug)[key] == {"category": RES_CAT, "params": R10K}
    assert "params" not in r["dk"] and '"vid"' not in (bom.dir / "components.json").read_text()
    assert r["dk"]["status"] == "Last Time Buy"
    rec = r["dk"]["alternatives"]
    assert rec["searched_at"] == comp.today() and [x["name"] for x in rec["criteria"]] == ["Resistance", "Package / Case"]
    assert [a["mpn"] for a in rec["items"]] == ["RMCF0603FT10K0", "ERJ-3EKF1002V"]
    assert "alternatives" not in stored(bom.slug, "LM317T")["dk"]
    assert "alternatives : 1 recherchée(s), 1 avec alternatives" in json.dumps(service.read_audit(5), ensure_ascii=False)
    # base des composants : la fiche et les alternatives suivent la référence (reprise par une autre veille)
    other = service.create_veille(name="Carte S", subject="BOM", schedule_expr=service.COMPONENTS_SCHEDULE,
                                  recipients=["a@example.org"], brief="# b\n", kind="composants",
                                  components=comp.parse_components("RC0603FR-0710KL;Yageo"))
    adopted = comp.load_components(other.slug)["components"][0]
    assert adopted["dk"]["alternatives"]["items"][0]["mpn"] == "RMCF0603FT10K0"
    assert catalog.browse(comp.key_of("RC0603FR-0710KL", "YAGEO"))
    # même jour : rien à revérifier, rien à rechercher de nouveau
    again = AltClient({})
    service.components_scan(bom.slug, client=again)
    assert again.calls == 0


def test_scan_alternatives_error_budget_and_deadline(bom, monkeypatch):
    out = service.components_scan(bom.slug, client=AltClient(LTB, error=digikey.DigiKeyError("API Digi-Key : HTTP 400")))
    assert out["complete"] and out["digikey"]["alternatives"]["error"] == "API Digi-Key : HTTP 400"
    assert out["digikey"]["alternatives"]["pending"] == 1 and "alternatives" not in stored(bom.slug, "RC0603FR-0710KL")["dk"]
    assert "HTTP 400" in json.dumps(service.read_audit(3))
    # plafond d'appels par série, et rien ne commence après l'échéance (temps de la tranche)
    targets = [comp_with(mpn=f"R{i}") for i in range(service.ALT_MAX_CALLS + 5)]
    specs = {comp.key_of(c["mpn"], "YAGEO"): spec() for c in targets}
    client = AltClient({})
    info = service._alternatives(bom.slug, client, targets, specs, comp.today())
    assert client.calls == service.ALT_MAX_CALLS and info["pending"] == 5
    client = AltClient({})
    assert service._alternatives(bom.slug, client, targets, specs, comp.today(), deadline=0)["searched"] == 0
    assert client.calls == 0


def test_scan_slice_leaves_alternatives_to_the_final_slice(bom, monkeypatch):
    monkeypatch.setattr(service, "SCAN_SLICE_S", -1)  # tranche écoulée dès la première référence
    client = AltClient(LTB, similar={"52": [res("RMCF0603FT10K0")]})
    out = service.components_scan(bom.slug, client=client)
    assert out["complete"] is False and client.searches == []
    monkeypatch.setattr(service, "SCAN_SLICE_S", 220)
    out = service.components_scan(bom.slug, client=client)
    assert out["complete"] and len(client.searches) == 1


def test_alternatives_apply_by_key_keeps_concurrent_edits(bom):
    service.components_scan(bom.slug, client=AltClient(LTB))

    class Editing(AltClient):
        def similar(self, category_id, filters):
            service.remove_components(bom.slug, ["GRM188R71H104KA93D"])  # l'interface modifie la liste
            return [res("RMCF0603FT10K0")]
    out = service.components_alternatives(bom.slug, ["RC0603FR-0710KL"], client=Editing({}))
    mpns = [c["mpn"] for c in comp.load_components(bom.slug)["components"]]
    assert out["found"] == 1 and mpns == ["RC0603FR-0710KL", "LM317T"]
    assert stored(bom.slug, "RC0603FR-0710KL")["dk"]["alternatives"]["items"][0]["mpn"] == "RMCF0603FT10K0"


def test_on_demand_search_explicit_references_and_reasons(bom, monkeypatch):
    with pytest.raises(service.ServiceError, match="clés Digi-Key absentes"):
        service.components_alternatives(bom.slug, ["RC0603FR-0710KL"])
    service.components_scan(bom.slug, client=AltClient({}))  # tout « Active », sans fiche
    out = service.components_alternatives(bom.slug, ["rc0603fr-0710kl", "LM317T", "XYZ"], client=AltClient({}))
    assert out["searched"] == 0 and out["targets"] == []
    assert {s["mpn"]: s["reason"] for s in out["skipped"]} == {
        "XYZ": "référence non suivie par cette veille",
        "RC0603FR-0710KL": "pas de fiche Digi-Key (statut Digi-Key à vérifier d'abord)",
        "LM317T": "pas de fiche Digi-Key (statut Digi-Key à vérifier d'abord)"}
    monkeypatch.setattr(comp, "today", lambda: "2099-01-01")
    active = {**LTB, "RC0603FR-0710KL": {**LTB["RC0603FR-0710KL"], "status": "Active"}}
    service.components_scan(bom.slug, client=AltClient(active))
    client = AltClient({}, similar={"52": [res("RMCF0603FT10K0")]})
    assert service.components_alternatives(bom.slug, client=client)["targets"] == []  # aucune fin de vie concernée
    out = service.components_alternatives(bom.slug, ["RC0603FR-0710KL", "LM317T"], client=client)  # demandée : même active
    assert out["targets"] == ["RC0603FR-0710KL"] and out["found"] == 1
    assert out["skipped"] == [{"mpn": "LM317T", "reason": "famille non concernée (seuls les passifs et les discrets le sont)"}]


def test_set_critical_params_changes_the_next_search(bom):
    client = AltClient(LTB, similar={"52": [res("RMCF0603FT10K0")]})
    service.components_scan(bom.slug, client=client)
    key = comp.key_of("RC0603FR-0710KL", "YAGEO")
    sheet = service.set_critical_params(bom.slug, key, ["Resistance", "Tolerance", "Power (Watts)"])
    assert sheet["custom"] and sheet["stale"]  # recherche faite avec les défauts : signalée
    assert stored(bom.slug, "RC0603FR-0710KL")["critical_params"] == [1, 2, 3]
    # critères modifiés : la référence attend une nouvelle recherche (passage automatique)
    assert [c["mpn"] for c in comp.alternatives_due(comp.load_components(bom.slug)["components"],
                                                    comp.load_specs(bom.slug), comp.load_notices(bom.slug),
                                                    comp.today())] == ["RC0603FR-0710KL"]
    service.components_alternatives(bom.slug, client=client)
    assert client.searches[-1] == ("52", [(1, "10 kohms"), (2, "±1%"), (3, "0.1w, 1/10w")])
    sp = comp.load_specs(bom.slug)[key]
    assert not comp.sheet(stored(bom.slug, "RC0603FR-0710KL"), sp)["stale"]
    assert service.set_critical_params(bom.slug, key, []) and comp.criteria(stored(bom.slug, "RC0603FR-0710KL"), sp) == []
    assert not service.set_critical_params(bom.slug, key, None)["custom"]
    assert "critical_params" not in stored(bom.slug, "RC0603FR-0710KL")
    with pytest.raises(comp.ComponentsError, match="inconnue"):
        service.set_critical_params(bom.slug, key, ["Couleur"])
    with pytest.raises(service.ServiceError, match="introuvable"):
        service.set_critical_params(bom.slug, "x|y", [])
    assert "components-critical" in json.dumps(service.read_audit(10))


def test_reset_search_keeps_user_criteria(bom):
    service.components_scan(bom.slug, client=AltClient(LTB))
    service.set_critical_params(bom.slug, comp.key_of("RC0603FR-0710KL", "YAGEO"), ["Resistance"])
    backup = service.reset_components(bom.slug)
    r = stored(bom.slug, "RC0603FR-0710KL")
    assert r["dk"] is None and r["critical_params"] == [1] and comp.load_specs(bom.slug) == {}
    assert json.loads((backup / "specs.json").read_text())["specs"]  # copie préalable, fiches comprises


def test_alternatives_now_runs_in_background_with_tracked_mpns_only(bom, monkeypatch):
    launched = []  # PID vivant (ce processus) : le lancement est « en cours »
    monkeypatch.setattr(service.hermes, "spawn_detached", lambda cmd, log, env: launched.append(cmd) or os.getpid())
    with pytest.raises(service.ServiceError, match="clés Digi-Key absentes"):
        service.alternatives_now(bom.slug)
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    with pytest.raises(service.ServiceError, match="aucune de ces références"):
        service.alternatives_now(bom.slug, ["--help", "XYZ"])
    log = service.alternatives_now(bom.slug, ["lm317t", "--help"])
    assert launched[0][-4:] == ["components", "alternatives", bom.slug, "LM317T"]
    assert progress.read(bom.slug)["kind"] == "alternatives" and log.endswith(".log")
    with pytest.raises(service.ServiceError, match="déjà en cours"):
        service.alternatives_now(bom.slug)


def test_progress_of_a_manual_alternatives_search(bom):
    service.components_scan(bom.slug, client=AltClient(LTB))
    manual(bom.slug, "alternatives")
    service.components_alternatives(bom.slug, ["RC0603FR-0710KL"], client=AltClient({}), track=True)
    view = service.run_progress(bom.slug)
    assert view["finished"] and view["summary"] == "Terminé : 1 / 1 référence(s), 0 avec alternatives"
    manual(bom.slug, "alternatives")
    service.components_alternatives(bom.slug, ["RC0603FR-0710KL"],
                                    client=AltClient({}, error=digikey.DigiKeyError("HTTP 403")), track=True)
    assert service.run_progress(bom.slug)["warning"] == "HTTP 403"


def test_cli_alternatives(bom, capsys, monkeypatch):
    service.components_scan(bom.slug, client=AltClient(LTB))
    monkeypatch.setattr(service.digikey, "Client", lambda *a: AltClient({}, similar={"52": [res("RMCF0603FT10K0")]}))
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    assert cli.main(["components", "alternatives", bom.slug, "RC0603FR-0710KL", "LM317T"]) == 0
    out = capsys.readouterr().out
    assert "✓ alternatives : 1 recherchée(s), 1 avec alternatives" in out and "LM317T : famille non concernée" in out


# --------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------
def test_mcp_sheet_alternatives_and_critical(bom, monkeypatch):
    pytest.importorskip("mcp")
    from test_mcp import call, data

    from veille_mcp.server import build_server
    srv = build_server()
    service.components_scan(bom.slug, client=AltClient(LTB, similar={"52": [res("RMCF0603FT10K0", (2, "±5%"))]}))
    sh = data(call(srv, "get_component_sheet", {"slug": bom.slug, "mpn": "rc0603fr-0710kl"}))
    assert sh["family"] == "Résistance" and sh["critical_source"] == "défauts de la famille" and sh["alternatives_possible"]
    assert [p["name"] for p in sh["parameters"] if p["critical"]] == ["Resistance", "Package / Case"]
    assert sh["alternatives"]["criteria"] == ["Resistance = 10 kOhms", "Package / Case = 0603 (1608 Metric)"]
    assert sh["alternatives"]["items"][0]["diffs"] == [{"name": "Tolerance", "ref": "±1%", "alt": "±5%"}]
    ic = data(call(srv, "get_component_sheet", {"slug": bom.slug, "mpn": "LM317T"}))
    assert ic["alternatives_possible"] is False and "famille non concernée" in ic["reason"]
    r = call(srv, "get_component_sheet", {"slug": bom.slug, "mpn": "XYZ"})
    assert r.is_error and "non suivie" in r.content[0].text
    rows = data(call(srv, "list_alternatives", {}))
    assert [(x["veille"], x["mpn"], x["worst_notice"]) for x in rows] == [(bom.slug, "RC0603FR-0710KL", "LTB")]
    assert data(call(srv, "list_alternatives", {"mpn": "LM317T"})) == []
    comps = {c["mpn"]: c for c in data(call(srv, "list_components", {"slug": bom.slug}))}
    assert comps["RC0603FR-0710KL"]["alternatives"] == 1 and comps["LM317T"]["family"] is None
    sh = data(call(srv, "set_critical_parameters", {"slug": bom.slug, "mpn": "RC0603FR-0710KL",
                                                    "parameters": ["Resistance", "Tolerance"]}))
    assert sh["critical_source"] == "utilisateur" and sh["alternatives"]["stale"] is True
    r = call(srv, "set_critical_parameters", {"slug": bom.slug, "mpn": "RC0603FR-0710KL", "parameters": ["Couleur"]})
    assert r.is_error and "inconnue" in r.content[0].text
    # recherche à la demande : pendant l'appel (≤ 10 MPN), sinon en arrière-plan
    monkeypatch.setattr(service.digikey, "Client", lambda *a: AltClient({}, similar={"52": [res("AC0603FR-0710KL")]}))
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    out = data(call(srv, "search_alternatives", {"slug": bom.slug, "mpns": ["RC0603FR-0710KL"]}))
    assert out["searched"] == 1 and out["alternatives"][0]["items"][0]["mpn"] == "AC0603FR-0710KL"
    assert out["alternatives"][0]["stale"] is False
    monkeypatch.setattr(service.hermes, "spawn_detached", lambda cmd, log, env: 4242)
    out = data(call(srv, "search_alternatives", {"slug": bom.slug}))
    assert out["started"] is True and out["log"].endswith(".log")


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
@pytest.fixture
def ui(bom, monkeypatch):
    from test_ui import PASSWORD, login  # noqa: F401

    from veille_ui import auth
    monkeypatch.setenv("VEILLE_UI_PASSWORD_HASH", auth.hash_password(PASSWORD))
    monkeypatch.setenv("VEILLE_UI_SECRET", "s" * 64)
    auth._failures.clear()
    from fastapi.testclient import TestClient

    from veille_ui.app import create_app
    client = TestClient(create_app(), base_url="http://localhost")
    return client, login(client)


def anchor(mpn, mfr="YAGEO"):
    return comp.anchor(comp.key_of(mpn, mfr))


def test_ui_detail_shows_sheet_and_alternatives_escaped(ui, bom):
    client, _ = ui
    evil = {**res("RMCF0603FT10K0", (2, "<img src=x onerror=alert(1)>")), "description": "<script>x</script>"}
    service.components_scan(bom.slug, client=AltClient(LTB, similar={"52": [evil]}))
    page = client.get(f"/veilles/{bom.slug}").text
    assert '<option value="alts">Avec alternatives</option>' in page
    assert re.search(r'<tr class="comp crit"[^>]*data-types="LTB" data-alts', page) and "⇄ 1" in page
    assert f'action="/veilles/{bom.slug}/alternatives"' not in page  # clés Digi-Key absentes : pas de bouton
    d = client.get(f"/veilles/{bom.slug}/components/{anchor('RC0603FR-0710KL')}").text
    assert f'id="cd-{anchor("RC0603FR-0710KL")}"' in d and "Fiche composant · Résistance" in d
    assert re.search(r'name="param" value="1" checked><span class="p-name">Resistance', d)
    assert 'name="param" value="2"><span class="p-name">Tolerance' in d
    assert 'value="7" disabled' in d  # caractéristique sans valeur : jamais critique
    assert "<script>x" not in d and "&lt;script&gt;" in d and "<img" not in d and "&lt;img" in d
    assert 'href="https://www.digikey.com/p/RMCF0603FT10K0" target="_blank" rel="noopener noreferrer"' in d
    assert "critères : Resistance = 10 kOhms ; Package / Case = 0603 (1608 Metric)" in d
    assert "Rechercher des alternatives" not in d and "Relancer la recherche" not in d  # pas de clés Digi-Key
    assert not re.search(r"<[^>]*\s(?:style|on[a-z]+)=", d)  # CSP : ni style ni gestionnaire en ligne
    ic = client.get(f"/veilles/{bom.slug}/components/{anchor('LM317T', 'Texas Instruments')}").text
    assert "Fiche composant" in ic and 'name="param"' not in ic and "Alternatives" not in ic  # CI : lecture seule


def test_ui_set_critical_and_search(ui, bom, monkeypatch):
    client, csrf = ui
    service.components_scan(bom.slug, client=AltClient(LTB, similar={"52": [res("RMCF0603FT10K0")]}))
    url = f"/veilles/{bom.slug}/components/{anchor('RC0603FR-0710KL')}"
    assert client.post(url + "/critical", data={"param": ["1", "2"]}).status_code == 403  # sans jeton CSRF
    hx = {"X-CSRF-Token": csrf, "HX-Request": "true"}
    r = client.post(url + "/critical", data={"param": ["1", "2"], "op": "save"}, headers=hx)
    assert r.status_code == 200 and "critiques : choisies" in r.text and "Critères modifiés depuis" in r.text
    assert '<details class="sheet" open>' in r.text  # la fiche reste ouverte après l'enregistrement (mobile)
    assert stored(bom.slug, "RC0603FR-0710KL")["critical_params"] == [1, 2]
    r = client.post(url + "/critical", data={"param": ["99"], "op": "save"}, headers=hx)
    assert r.status_code == 400 and "inconnue" in r.text and 'role="alert"' in r.text
    r = client.post(url + "/critical", data={"op": "reset"}, headers=hx)
    assert r.status_code == 200 and "défauts de la famille" in r.text
    assert "critical_params" not in stored(bom.slug, "RC0603FR-0710KL")
    # recherche : sans clés, un message ; avec clés, le détail à jour
    r = client.post(url + "/alternatives", headers=hx)
    assert r.status_code == 400 and "clés Digi-Key absentes" in r.text
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    monkeypatch.setattr(service.digikey, "Client", lambda *a: AltClient({}, similar={"52": [res("AC0603FR-0710KL")]}))
    r = client.post(url + "/alternatives", headers=hx)
    assert r.status_code == 200 and "AC0603FR-0710KL" in r.text and "Relancer la recherche" in r.text
    assert 'hx-post="' + url + '/alternatives"' in r.text
    ic = f"/veilles/{bom.slug}/components/{anchor('LM317T', 'Texas Instruments')}/alternatives"
    r = client.post(ic, headers=hx)
    assert r.status_code == 400 and "famille non concernée" in r.text
    assert client.post(url + "/delete", headers=hx).status_code == 404
    assert client.post(f"/veilles/{bom.slug}/components/c-0000000000/critical", headers=hx).status_code == 404
    # bouton de la liste : recherche en arrière-plan des fins de vie, journal suivi
    monkeypatch.setattr(service.hermes, "spawn_detached", lambda cmd, log, env: 4242)
    page = client.get(f"/veilles/{bom.slug}").text
    assert f'action="/veilles/{bom.slug}/alternatives"' in page
    r = client.post(f"/veilles/{bom.slug}/alternatives", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/veilles/{bom.slug}?ok=alternatives"


def test_ui_catalog_detail_is_read_only(ui, bom):
    client, _ = ui
    service.components_scan(bom.slug, client=AltClient(LTB, similar={"52": [res("RMCF0603FT10K0")]}))
    key = comp.key_of("RC0603FR-0710KL", "YAGEO")
    d = client.get("/composants/detail", params={"key": key}).text
    assert "RMCF0603FT10K0" in d and "critères : Resistance = 10 kOhms" in d
    # lecture seule : ni choix des critiques (propres à chaque veille), ni critiques par défaut présentées comme telles
    assert "hx-post" not in d and 'name="param"' not in d and "Critères modifiés" not in d
    assert "★" not in d and "défauts de la famille" not in d


def test_robustness_unreadable_sheet_text_values_progress_and_corrupt_list(ui, bom, monkeypatch):
    client, csrf = ui
    # comparaison sur le texte affiché quand une valeur n'a pas d'identifiant Digi-Key (ValueId)
    no_vid = [{**p, "vid": ""} for p in R10K]
    c = comp_with()
    alt = {**res("RMCF0603FT10K0"), "params": [{**p, "value": p["value"].upper(), "vid": ""} for p in R10K]}
    assert comp.rank_alternatives(c, spec(no_vid), [alt], comp.criteria(c, spec(no_vid)))[0]["score"] == 100
    # scan d'un lancement manuel suivi : la recherche d'alternatives s'affiche dans l'étape Digi-Key
    manual(bom.slug, "statuts")
    writes, orig = [], progress.Tracker.update
    monkeypatch.setattr(progress.Tracker, "update",
                        lambda self, throttle=False, **ch: writes.append(dict(ch)) or orig(self, **ch))
    service.components_scan(bom.slug, client=AltClient(LTB), plan_batch=False)
    assert any(w.get("digikey", {}).get("current") == "RC0603FR-0710KL (alternatives)" for w in writes)
    # fiche illisible : vide (réécrite au passage suivant), jamais une erreur qui bloquerait la page
    (bom.dir / "specs.json").write_text("{pas du json")
    assert comp.load_specs(bom.slug) == {}
    url = f"/veilles/{bom.slug}/components/{anchor('RC0603FR-0710KL')}"
    assert client.get(url).status_code == 200
    # liste illisible : erreur dite (500), pas « introuvable »
    (bom.dir / "components.json").write_text("{pas du json")
    hx = {"X-CSRF-Token": csrf, "HX-Request": "true"}
    assert client.get(url).status_code == 500 and client.post(url + "/critical", headers=hx).status_code == 500


# --------------------------------------------------------------------------
# Retours de revue
# --------------------------------------------------------------------------
def test_refused_request_is_recorded_and_does_not_block_the_others(bom, ui):
    """Une recherche refusée (HTTP 400 : filtres d'une référence) est enregistrée, datée et affichée ; la série
    continue. Elle ne repasse pas en tête au passage suivant (sinon elle bloquerait toutes les autres)."""
    client, _ = ui
    two = {**LTB, "GRM188R71H104KA93D": {**LTB["RC0603FR-0710KL"], "category": [
        {"id": "3", "name": "Capacitors"}, {"id": "60", "name": "Ceramic Capacitors"}],
        "params": [prm(1, "Capacitance", "0.1 µF"), prm(5, "Package / Case", "0603 (1608 Metric)")]}}

    class Refusing(AltClient):
        def similar(self, category_id, filters):
            self.calls += 1
            self.searches.append(category_id)
            if category_id == "52":
                raise digikey.RequestRefused("API Digi-Key : HTTP 400 (alternatives, catégorie 52)")
            return [{**res("CL10B104KB8NNNC"), "category": [], "params": two["GRM188R71H104KA93D"]["params"]}]
    c = Refusing(two)
    out = service.components_scan(bom.slug, client=c)
    assert sorted(c.searches) == ["52", "60"] and out["digikey"]["alternatives"]["searched"] == 2
    rec = stored(bom.slug, "RC0603FR-0710KL")["dk"]["alternatives"]
    assert rec["items"] == [] and "HTTP 400" in rec["error"] and rec["searched_at"] == comp.today()
    assert stored(bom.slug, "GRM188R71H104KA93D")["dk"]["alternatives"]["items"][0]["mpn"] == "CL10B104KB8NNNC"
    assert comp.alternatives_due(comp.load_components(bom.slug)["components"], comp.load_specs(bom.slug),
                                 comp.load_notices(bom.slug), comp.today()) == []
    d = client.get(f"/veilles/{bom.slug}/components/{anchor('RC0603FR-0710KL')}").text
    assert "Recherche refusée par Digi-Key : API Digi-Key : HTTP 400" in d
    # une erreur d'accès (403) arrête la série, rien d'enregistré
    service.set_critical_params(bom.slug, comp.key_of("RC0603FR-0710KL", "YAGEO"), ["Resistance"])
    out = service.components_alternatives(bom.slug, client=AltClient({}, error=digikey.DigiKeyError("HTTP 403")))
    assert out["error"] == "HTTP 403" and out["searched"] == 0 and out["pending"] == 1


def test_http_400_is_a_refused_request_and_keeps_its_nature():
    from test_digikey import http_error
    with pytest.raises(digikey.RequestRefused, match=r"HTTP 400 \(alternatives"):
        Fake(TOKEN, http_error(400)).client().similar("52", [])
    with pytest.raises(digikey.DigiKeyError) as e:
        Fake(TOKEN, http_error(403)).client().similar("52", [])
    assert not isinstance(e.value, digikey.RequestRefused)


def test_stale_ignores_parameter_order_and_scan_budget_counts_alternatives(bom, monkeypatch):
    rec = {"searched_at": "2026-10-26", "criteria": [{"id": 5}, {"id": 1}], "items": []}
    assert not comp.alternatives_stale(comp_with(dk={"alternatives": rec}), spec(list(reversed(R10K))))
    # appels du passage Digi-Key et recherches d'alternatives sous le même plafond (quota journalier)
    monkeypatch.setattr(digikey, "MAX_CALLS", 3)  # 3 statuts lus : plus rien pour les alternatives
    client = AltClient(LTB)
    out = service.components_scan(bom.slug, client=client, plan_batch=False)
    assert client.searches == [] and out["digikey"]["alternatives"]["pending"] == 1


def test_sync_search_does_not_write_the_progress_of_a_background_one(bom):
    service.components_scan(bom.slug, client=AltClient(LTB))
    manual(bom.slug, "alternatives")  # « Alternatives des fins de vie » en cours
    before = progress.read(bom.slug)
    service.components_alternatives(bom.slug, ["RC0603FR-0710KL"], client=AltClient({}))  # bouton d'un composant
    assert progress.read(bom.slug) == before
    service.components_alternatives(bom.slug, client=AltClient({}), track=True)  # le lancement détaché lui-même
    assert service.run_progress(bom.slug)["summary"].startswith("Terminé : ")


def test_mcp_search_returns_its_targets_even_beyond_the_listing_limit(bom, monkeypatch):
    pytest.importorskip("mcp")
    from test_mcp import call, data

    from veille_mcp.server import build_server
    service.components_scan(bom.slug, client=AltClient(LTB))
    rec = {"searched_at": comp.today(), "criteria": [], "items": [{"mpn": "X", "manufacturer": "Y", "score": 1}]}
    filler = [{"mpn": f"EOL{i:03d}", "manufacturer": "YAGEO", "added_at": "2026-10-01", "web_checked_at": None,
               "dk": {"status": "Obsolete", "alternatives": rec}} for i in range(60)]
    data_ = comp.load_components(bom.slug)
    data_["components"] += filler
    comp.save_components(bom.slug, data_)
    notices = [comp.status_notice(c, "OBSOLETE", "x", "https://dk/x", "dk", "digikey") for c in filler]
    for n in notices:
        n["found_at"] = comp.today()
    comp.merge(n_ := comp.load_notices(bom.slug), notices)
    comp.save_notices(bom.slug, n_)
    monkeypatch.setattr(service.digikey, "Client", lambda *a: AltClient({}, similar={"52": [res("AC0603FR-0710KL")]}))
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    srv = build_server()
    assert len(data(call(srv, "list_alternatives", {"slug": bom.slug}))) == 50
    out = data(call(srv, "search_alternatives", {"slug": bom.slug, "mpns": ["RC0603FR-0710KL"]}))
    assert [a["mpn"] for a in out["alternatives"]] == ["RC0603FR-0710KL"]
    sh = data(call(srv, "set_critical_parameters", {"slug": bom.slug, "mpn": "RC0603FR-0710KL", "parameters": [1, "5"]}))
    assert [p["id"] for p in sh["parameters"] if p["critical"]] == [1, 5]  # identifiants entiers acceptés


def test_specs_are_cached_until_the_file_changes(bom):
    service.components_scan(bom.slug, client=AltClient(LTB))
    first = comp.load_specs(bom.slug)
    first["x"] = {}  # copie : le cache n'est pas modifié par l'appelant
    assert "x" not in comp.load_specs(bom.slug)
    comp.save_specs(bom.slug, {"k": spec()})
    assert comp.load_specs(bom.slug) == {"k": spec()}


def test_ui_search_targets_the_opened_reference_only(ui, bom, monkeypatch):
    """Même MPN chez deux fabricants : la recherche du détail ne vise que la référence ouverte."""
    client, csrf = ui
    service.import_components(bom.slug, [{"mpn": "RC0603FR-0710KL", "manufacturer": "Vishay"}])
    service.components_scan(bom.slug, client=AltClient(LTB))
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    fake = AltClient({}, similar={"52": [res("AC0603FR-0710KL")]})
    monkeypatch.setattr(service.digikey, "Client", lambda *a: fake)
    url = f"/veilles/{bom.slug}/components/{anchor('RC0603FR-0710KL')}/alternatives"
    r = client.post(url, headers={"X-CSRF-Token": csrf, "HX-Request": "true"})
    assert r.status_code == 200 and len(fake.searches) == 1 and 'role="alert"' not in r.text


# --------------------------------------------------------------------------
# Rapport, e-mail, Excel : alternatives des composants en fin de vie
# --------------------------------------------------------------------------
def test_report_mail_and_xlsx_show_alternatives_of_end_of_life(bom, tools):  # noqa: F811
    service.components_scan(bom.slug, client=AltClient(LTB, similar={"52": [
        res("ERJ-3EKF1002V", (2, "±5%"), (3, "0.2W"), (4, "Thin Film"), (6, "±50ppm/°C"), mfr="Panasonic"),
        res("RMCF0603FT10K0")]}))
    p = bom.dir / "rapports" / "cand.json"
    p.write_text(json.dumps({"notices": []}))
    service.components_finalize(bom.slug, p)
    md = next((bom.dir / "rapports").glob("*-veille.md")).read_text()
    # tableau : nombre d'alternatives ; détail : critères, alternatives liées, 3 écarts au plus (le reste compté)
    assert f"[**RC0603FR\\-0710KL**](#{comp.anchor(comp.key_of('RC0603FR-0710KL', 'YAGEO'))}) ⇄ 2 |" in md
    assert "**Alternatives actives Digi-Key** (recherche du" in md and "Resistance = 10 kOhms" in md
    assert "- [**RMCF0603FT10K0**](https://www.digikey.com/p/RMCF0603FT10K0) — YAGEO · 100 % identique" in md
    assert "Autres caractéristiques identiques" in md
    assert "Écarts : Tolerance : ±1\\% → ±5\\% ; " in md and "Thin Film ; …" in md and "Temperature Coefficient" not in md
    # pas d'alternative : le motif est dit (circuit intégré)
    assert "*Alternatives : famille non concernée \\(seuls les passifs et les discrets le sont\\).*" in md
    mail = tools()[-1]
    text = mail["args"][mail["args"].index("--text") + 1]
    assert "⇄ 2 alternative(s) dans le PDF" in mail["html"] and "— 2 alternative(s) dans le PDF —" in text
    from test_components import _sheet
    sheet = _sheet(next((bom.dir / "rapports").glob("*.xlsx")).read_bytes())
    head = [c.value for c in sheet[1]]
    rows = {r[0]: dict(zip(head, r, strict=True)) for r in sheet.iter_rows(min_row=2, values_only=True)}
    assert rows["RC0603FR-0710KL"]["Alternatives"] == "RMCF0603FT10K0 (YAGEO) ; ERJ-3EKF1002V (Panasonic)"
    assert rows["LM317T"]["Alternatives"] is None and rows["RC0603FR-0710KL"]["Lien"]  # colonnes suivantes intactes


def test_alternatives_gap_reasons():
    rec = {"searched_at": "2026-10-26", "criteria": [], "items": []}
    assert comp.alternatives_gap(comp_with(dk={"alternatives": {**rec, "items": [{"mpn": "X"}]}}), spec()) == ""
    assert comp.alternatives_gap(comp_with(dk={"alternatives": rec}), spec()) == (
        "aucune référence active équivalente chez Digi-Key (recherche du 2026-10-26)")
    assert comp.alternatives_gap(comp_with(dk={"alternatives": {**rec, "error": "HTTP 400"}}), spec()) == (
        "recherche refusée par Digi-Key le 2026-10-26")
    assert comp.alternatives_gap(comp_with(dk={"error": "introuvable"}), None) == "référence inconnue de Digi-Key"
    assert comp.alternatives_gap(comp_with(), spec(category=IC_CAT)).startswith("famille non concernée")
    assert comp.alternatives_gap(comp_with(), spec()) == "recherche au prochain passage Digi-Key"
    # sans les fiches, la vue ne prétend rien (pas de « pas de fiche » trompeur)
    c = comp_with(dk={"status": "Obsolete"})
    notice = comp.status_notice(c, "OBSOLETE", "Statut Digi-Key : Obsolete", "https://www.digikey.com/p/r",
                                "Digi-Key", "digikey")
    comp.merge(notices := [], [notice], "2026-10-26")
    assert comp.digest([c], notices, [])["alt_gaps"] == {}
    assert "*Alternatives :" not in comp.to_markdown(comp.digest([c], notices, []))
    assert list(comp.digest([c], notices, [], specs={})["alt_gaps"].values()) == [
        "pas de fiche Digi-Key (statut Digi-Key à vérifier d'abord)"]


def test_report_alternatives_escape_untrusted_values_and_skip_odd_entries():
    import shutil
    import subprocess
    evil = "<img src=x onerror=alert(1)> | ::: {.x}"
    rec = {"searched_at": "2026-10-26", "criteria": [{"id": 1, "name": evil, "value": "[a](javascript:x)"}, "?"],
           "items": ["pas un objet", {"mpn": evil, "manufacturer": evil, "score": 50, "url": "javascript:alert(1)",
                                      "description": evil, "diffs": [{"name": evil, "ref": "1", "alt": "2"}, None]}]}
    c = comp_with(dk={"status": "Obsolete", "alternatives": rec})
    notice = comp.status_notice(c, "OBSOLETE", "Statut", "https://www.digikey.com/p/r", "Digi-Key", "digikey")
    comp.merge(notices := [], [notice], "2026-10-26")
    view = comp.digest([c], notices, notices, specs={})
    md = comp.to_markdown(view, heading="#")
    assert "](javascript" not in md and "⇄ 1" in md and "- **\\<img" in md  # lien refusé : MPN sans lien
    assert "<img" not in comp.to_html_email(view, "T")
    if shutil.which("pandoc"):
        out = subprocess.run(["pandoc", "-f", "markdown+fenced_divs+bracketed_spans+pipe_tables", "-t", "html5"],
                             input=md, capture_output=True, text=True, check=True).stdout
        assert "<img" not in out and 'href="javascript' not in out and "Alternatives actives Digi-Key" in out


def test_chapter_of_a_linked_veille_shows_alternatives_and_reasons(bom, tools):  # noqa: F811
    service.components_scan(bom.slug, client=AltClient(LTB, similar={"52": [res("RMCF0603FT10K0")]}))
    v = service.create_veille(name="Veille Indus", subject="s", schedule_expr="30 6 * * 1",
                              recipients=["a@example.org"], brief="# b\n", include_components=bom.slug)
    md = v.dir / "rapports" / "2026-10-12-06h30-veille.md"
    md.write_text("# r\n")
    service.components_chapter(bom.slug, v.slug, md)
    text = md.read_text()
    assert "**Alternatives actives Digi-Key**" in text and "RMCF0603FT10K0" in text and "⇄ 1" in text
    assert "*Alternatives : famille non concernée" in text  # fiches passées au chapitre aussi


def test_report_alternatives_odd_records():
    rec = {"searched_at": "2026-10-26", "criteria": [], "items": [
        {"mpn": "ZERO", "manufacturer": "M", "score": 0, "diffs": [{"name": f"P{i}", "ref": "a", "alt": "b"}
                                                                   for i in range(comp.MAX_DIFFS)]},
        {"mpn": "", "score": 50}, {"manufacturer": "sans MPN"}, None]}
    c = comp_with(dk={"status": "Obsolete", "alternatives": rec})
    notice = comp.status_notice(c, "OBSOLETE", "Statut", "https://www.digikey.com/p/r", "Digi-Key", "digikey")
    comp.merge(notices := [], [notice], "2026-10-26")
    md = comp.to_markdown(comp.digest([c], notices, notices, specs={}))
    # score nul affiché ; écarts stockés tronqués : « … », jamais un compte faux ; entrées sans MPN ignorées
    assert "— M · 0 % identique" in md and "P2 : a → b ; …" in md and "⇄ 1" in md and "sans MPN" not in md
    # que des entrées illisibles : le motif est dit (et non un détail muet)
    c["dk"]["alternatives"] = {**rec, "items": ["x", None, {"mpn": None}]}
    assert comp.alternatives_gap(c, spec()) == "aucune référence active équivalente chez Digi-Key (recherche du 2026-10-26)"
    assert comp.criteria({"critical_params": [[1], "x", 1]}, spec())[0]["name"] == "Resistance"  # retouché : pas d'erreur
