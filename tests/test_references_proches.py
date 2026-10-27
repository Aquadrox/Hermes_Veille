"""Références proches d'une référence que Digi-Key ne connaît pas (constaté le 27/10/2026 sur 32 références
« introuvables ») : variantes au MPN prolongé (« DS28E01P-100+T »), alias d'un revendeur (« APA102-2020 » d'un
produit SparkFun), fabricant écrit autrement (« NXP USA Inc. »), priorité au lot web, interface, Excel, agent."""
import pytest
from test_components import BOM, FakeClient, _sheet, tools, watch  # noqa: F401 - fixtures partagées
from test_digikey import TOKEN, Fake, found, product

from veille_core import components as comp
from veille_core import service

DS = "DS28E01P-100"
NEAR = [{"mpn": "DS28E01P-100+T", "manufacturer": "Analog Devices Inc./Maxim Integrated", "status": "Active",
         "url": "https://www.digikey.com/p/ds-t", "kind": "variante"},
        {"mpn": "14608", "manufacturer": "SparkFun Electronics", "status": "Obsolete",
         "url": "https://www.digikey.com/p/sf", "kind": "alias"}]


def test_manufacturer_names_without_generic_words():
    assert comp.same_manufacturer("NXP Semiconductors", "NXP USA Inc.")
    assert comp.same_manufacturer("Diodes Incorporated", "Diodes Inc")
    assert not comp.same_manufacturer("ON", "Infineon Technologies")  # nom court : identique seulement
    assert not comp.same_manufacturer("Semiconductor Corp", "Toshiba Semiconductor")  # rien de distinctif
    assert not comp.same_manufacturer("NXP Semiconductors", "Nexperia USA Inc.")


def test_status_keeps_variants_then_aliases_never_as_the_reference():
    ad = "Analog Devices Inc./Maxim Integrated"
    products = [product(mpn="DS28E01P-100+T", mfr=ad, status="Active"),
                product(mpn="DS28E01P-100+", mfr=ad, status="Active"),
                product(mpn="DS28E01P-100+T", mfr=ad, status="Active"),            # doublon
                product(mpn="DS28E01P-100X", mfr="Autre Fabricant", status="Active"),  # autre fabricant : pas une variante
                product(mpn="DS28E01", mfr=ad, status="Active"),                   # plus court : pas une variante
                {**product(mpn="14608", mfr="SparkFun Electronics", status="Obsolete"),
                 "OtherNames": ["COM-14608", "ds28e01p100", 7]},                   # alias (graphie libre)
                {**product(mpn="999", mfr="Adafruit"), "OtherNames": "DS28E01P-100"}]  # mal formé : ignoré
    out = Fake(TOKEN, found(*products, key="Products")).client().status(DS, "Analog Devices")
    assert out["error"] == "référence exacte introuvable chez Digi-Key"
    assert [(x["mpn"], x["kind"]) for x in out["near"]] == [
        ("DS28E01P-100+T", "variante"), ("DS28E01P-100+", "variante"), ("14608", "alias")]
    # fabricant écrit autrement chez Digi-Key : la référence elle-même est trouvée
    nxp = Fake(TOKEN, found(product(mpn="PCF8523T", mfr="NXP USA Inc.", status="Obsolete")))
    assert nxp.client().status("PCF8523T", "NXP Semiconductors")["status"] == "Obsolete"
    many = [product(mpn=f"{DS}-{i}", mfr=ad) for i in range(15)]
    assert len(Fake(TOKEN, found(*many, key="Products")).client().status(DS, "Analog Devices")["near"]) == comp.MAX_NEAR


def test_near_accessor_and_summary_ignore_odd_entries():
    c = {"mpn": DS, "manufacturer": "Analog Devices", "dk": {"error": "x", "near": [*NEAR, "x", {"mpn": 1},
                                                                                     {"mpn": "Y", "kind": "?"}]}}
    assert comp.near(c) == NEAR and comp.near_text(c) == "1 variante(s), 1 alias"
    assert comp.near({"dk": {"near": "x"}}) == [] and comp.near({}) == [] and comp.near_text({}) == ""


def test_scan_stores_near_refs_first_in_the_web_lot_and_forgets_them_once_found(watch):  # noqa: F811
    unknown = {"error": "référence exacte introuvable chez Digi-Key", "near": NEAR}
    out = service.components_scan(watch.slug, client=FakeClient({"NE555P": unknown}))
    ne = next(c for c in comp.load_components(watch.slug)["components"] if c["mpn"] == "NE555P")
    assert ne["dk"]["near"] == NEAR
    # inconnue de Digi-Key, jamais recherchée : en tête du lot web, avec ses références proches pour l'agent
    assert out["batch"][0]["mpn"] == "NE555P" and out["batch"][0]["digikey_near"] == NEAR
    prompt = (watch.dir / "job-prompt.md").read_text()
    assert "RÉFÉRENCES PROCHES" in prompt and "Un alias ne fait JAMAIS foi" in prompt
    # référence trouvée au passage suivant (autre jour) : les références proches disparaissent
    data = comp.load_components(watch.slug)
    for c in data["components"]:
        c["dk"]["checked_at"] = "2020-01-01"
    data["scan"] = {}
    comp.save_components(watch.slug, data)
    service.components_scan(watch.slug, client=FakeClient({"NE555P": {"status": "Active", "product_url": None}}))
    ne = next(c for c in comp.load_components(watch.slug)["components"] if c["mpn"] == "NE555P")
    assert "near" not in ne["dk"] and ne["dk"]["status"] == "Active"


def test_batch_puts_unknown_never_searched_first():
    cs = [{"mpn": "A", "dk": {"status": "Active"}}, {"mpn": "B", "dk": {"error": "x"}},
          {"mpn": "C", "web_checked_at": "2026-01-01", "dk": {"error": "x"}}, {"mpn": "D"}]
    assert [c["mpn"] for c in comp.batch(cs, 4)] == ["B", "A", "D", "C"]


def test_xlsx_lists_each_near_reference_on_its_own_sheet():
    evil = {**NEAR[0], "mpn": "=HYPERLINK(\"http://evil/x\",\"clic\")", "url": "javascript:alert(1)"}
    cs = [{"mpn": DS, "manufacturer": "Analog Devices", "dk": {"error": "référence exacte introuvable chez Digi-Key",
                                                               "near": [NEAR[0], NEAR[1], evil]}},
          {"mpn": "LM317T", "manufacturer": "Texas Instruments", "dk": {"status": "Active"}}]
    data = comp.to_xlsx(cs, [])
    main = {r[0]: r for r in _sheet(data).iter_rows(min_row=2, values_only=True)}
    head = [c.value for c in _sheet(data)[1]]
    assert main[DS][head.index("Proches chez Digi-Key")] == "2 variante(s), 1 alias"
    assert main["LM317T"][head.index("Proches chez Digi-Key")] is None
    from io import BytesIO

    from openpyxl import load_workbook
    ws = load_workbook(BytesIO(data))["Références proches"]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0] == ("Composant", "Fabricant", "Référence proche", "Fabricant Digi-Key", "Type", "Statut Digi-Key", "Lien")
    assert rows[1] == (DS, "Analog Devices", "DS28E01P-100+T", "Analog Devices Inc./Maxim Integrated", "variante",
                       "Active", "https://www.digikey.com/p/ds-t")
    assert ws.cell(2, 7).hyperlink.target == "https://www.digikey.com/p/ds-t" and ws.auto_filter.ref
    assert rows[3][2].startswith("=HYPERLINK") and ws.cell(4, 3).data_type == "s"  # texte, jamais formule
    assert rows[3][6] is None and ws.cell(4, 7).hyperlink is None  # lien refusé
    assert len(rows) == 4  # LM317T : aucune ligne


@pytest.fixture
def ui(watch, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient
    from test_ui import PASSWORD, login

    from veille_ui import auth
    from veille_ui.app import create_app
    monkeypatch.setenv("VEILLE_UI_PASSWORD_HASH", auth.hash_password(PASSWORD))
    monkeypatch.setenv("VEILLE_UI_SECRET", "s" * 64)
    auth._failures.clear()
    client = TestClient(create_app(), base_url="http://localhost")
    login(client)
    return client


def test_ui_detail_lists_near_references_escaped(ui, watch):  # noqa: F811
    near = [{**NEAR[0], "mpn": "<img src=x onerror=alert(1)>"}, NEAR[1]]
    service.components_scan(watch.slug, client=FakeClient({"NE555P": {"error": "référence exacte introuvable chez "
                                                                               "Digi-Key", "near": near}}))
    d = ui.get(f"/veilles/{watch.slug}/components/{comp.anchor(comp.key_of('NE555P', 'Texas Instruments'))}").text
    assert "Variante chez Digi-Key" in d and "Alias chez Digi-Key" in d and "&lt;img src=x" in d and "<img src=x" not in d
    assert 'href="https://www.digikey.com/p/sf"' in d and "Indices seulement" in d
    assert "<script" not in d.split('class="comp-detail"')[1]  # rien en ligne (CSP)
