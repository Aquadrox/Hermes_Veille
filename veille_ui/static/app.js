// Comportements de l'interface — fichier statique (la CSP interdit le JS en ligne).
(function () {
  "use strict";

  // Confirmation des formulaires sensibles : <form data-confirm="…">.
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Affiche seulement les champs utiles au rythme choisi : <label data-show="weekly monthly">.
  function syncFreq() {
    var sel = document.getElementById("freq_kind");
    if (!sel) return;
    document.querySelectorAll("[data-show]").forEach(function (el) {
      var kinds = el.getAttribute("data-show").split(" ");
      el.hidden = kinds.indexOf(sel.value) === -1;
    });
  }
  document.addEventListener("change", function (e) {
    if (e.target && e.target.id === "freq_kind") syncFreq();
  });
  document.addEventListener("DOMContentLoaded", syncFreq);

  // Liste des composants : recherche plein texte + filtre par colonne, en temps réel.
  // Textes normalisés une seule fois (casse et accents ignorés) ; chaque frappe est un simple parcours.
  function norm(s) {
    return s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  }
  function initComponents() {
    var table = document.getElementById("comp-table");
    if (!table) return;
    var search = document.getElementById("comp-search");
    var count = document.getElementById("comp-count");
    var empty = document.getElementById("comp-empty");
    var cols = table.querySelectorAll("thead [data-col]");
    var typeSel = table.querySelector("thead [data-types]");
    var rows = Array.prototype.map.call(table.querySelectorAll("tbody tr.comp"), function (tr) {
      var cells = Array.prototype.map.call(tr.cells, function (td) { return norm(td.textContent); });
      var types = tr.getAttribute("data-types").split(" ").filter(Boolean);
      // codes (EOL, PCN, LTB…) en plus des libellés affichés : on cherche « eol » plus que « fin de vie »
      return {tr: tr, detail: tr.nextElementSibling, cells: cells, crit: tr.classList.contains("crit"), types: types,
              text: cells.join(" ") + " " + norm(types.join(" ") + " " + (tr.getAttribute("data-q") || ""))};
    });
    function apply() {
      var words = norm(search.value).split(/\s+/).filter(Boolean);
      var colFilters = Array.prototype.map.call(cols, function (el) {
        return {i: +el.getAttribute("data-col"), v: norm(el.value.trim())};
      }).filter(function (f) { return f.v; });
      var type = typeSel.value, shown = 0;
      rows.forEach(function (r) {
        var ok = words.every(function (w) { return r.text.indexOf(w) !== -1; }) &&
          colFilters.every(function (f) { return r.cells[f.i].indexOf(f.v) !== -1; }) &&
          (!type || (type === "critical" ? r.crit : type === "notice" ? r.types.length > 0 :
                     type === "none" ? r.types.length === 0 : r.types.indexOf(type) !== -1));
        r.tr.hidden = !ok;
        if (!ok) r.detail.hidden = true;  // un détail ouvert suit sa ligne
        else if (r.tr.querySelector(".comp-toggle").getAttribute("aria-expanded") === "true") r.detail.hidden = false;
        if (ok) shown++;
      });
      count.textContent = shown + " / " + rows.length;
      empty.hidden = shown > 0 || rows.length === 0;
    }
    table.addEventListener("input", apply);
    table.addEventListener("change", apply);
    search.addEventListener("input", apply);
    // Détail : clic sur la ligne (hors lien) = clic sur le bouton du composant (chargé une fois par htmx).
    table.addEventListener("click", function (e) {
      var tr = e.target.closest("tr.comp");
      if (!tr || e.target.closest("a")) return;
      var btn = tr.querySelector(".comp-toggle");
      if (!e.target.closest(".comp-toggle")) { btn.click(); return; }
      var open = btn.getAttribute("aria-expanded") !== "true";
      btn.setAttribute("aria-expanded", String(open));
      tr.nextElementSibling.hidden = !open;
    });
    apply();
  }
  document.addEventListener("DOMContentLoaded", initComponents);
  document.addEventListener("htmx:responseError", function (e) {
    var target = e.detail.target;
    if (target && target.closest && target.closest("tr.detail")) target.textContent = "Détail indisponible : recharge la page.";
  });

  // Session expirée pendant une requête HTMX : retour à la page de connexion.
  document.addEventListener("htmx:responseError", function (e) {
    if (e.detail.xhr.status === 403) window.alert("Session expirée ou jeton invalide : recharge la page.");
  });
})();
