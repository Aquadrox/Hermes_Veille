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
  // Textes normalisés une seule fois (casse et accents ignorés). Seules les lignes qui changent
  // d'état sont touchées et au plus `data-limit` lignes sont affichées : la mise en page reste
  // légère sur mobile, même avec des milliers de références.
  function norm(s) {
    return s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  }
  function setHidden(el, hidden) {
    if (el.hidden !== hidden) el.hidden = hidden;
  }
  function initComponents() {
    var table = document.getElementById("comp-table");
    if (!table) return;
    var limit = +table.getAttribute("data-limit");
    var search = document.getElementById("comp-search");
    var count = document.getElementById("comp-count");
    var empty = document.getElementById("comp-empty");
    var more = document.getElementById("comp-more");
    var cols = table.querySelectorAll("thead [data-col]");
    var typeSel = table.querySelector("thead [data-types]");
    var showAll = false, timer = null;
    var rows = Array.prototype.map.call(table.querySelectorAll("tbody tr.comp"), function (tr) {
      var types = tr.getAttribute("data-types").split(" ").filter(Boolean);
      // codes (EOL, PCN, LTB…) en plus des libellés affichés : on cherche « eol » plus que « fin de vie »
      var r = {tr: tr, types: types, crit: tr.classList.contains("crit"), cells: null,
               detail: null, open: false, loaded: false,
               text: norm(tr.textContent + " " + types.join(" ") + " " + tr.getAttribute("data-q"))};
      tr.compRow = r;
      return r;
    });
    function cells(r) {  // textes par colonne : calculés au premier filtre de colonne seulement
      return r.cells || (r.cells = Array.prototype.map.call(r.tr.cells, function (td) { return norm(td.textContent); }));
    }
    function apply() {
      var words = norm(search.value).split(/\s+/).filter(Boolean);
      var colFilters = Array.prototype.map.call(cols, function (el) {
        return {i: +el.getAttribute("data-col"), v: norm(el.value.trim())};
      }).filter(function (f) { return f.v; });
      var type = typeSel.value, matched = 0;
      rows.forEach(function (r) {
        var ok = words.every(function (w) { return r.text.indexOf(w) !== -1; }) &&
          colFilters.every(function (f) { return cells(r)[f.i].indexOf(f.v) !== -1; }) &&
          (!type || (type === "critical" ? r.crit : type === "notice" ? r.types.length > 0 :
                     type === "none" ? r.types.length === 0 : r.types.indexOf(type) !== -1));
        if (ok) matched++;
        var visible = ok && (showAll || matched <= limit);
        setHidden(r.tr, !visible);
        if (r.detail) setHidden(r.detail, !(visible && r.open));  // un détail ouvert suit sa ligne
      });
      var shown = showAll ? matched : Math.min(matched, limit);
      count.textContent = matched + " / " + rows.length;
      more.firstChild.textContent = (matched - shown) + " autre(s) composant(s) correspondent : affine la recherche ou ";
      setHidden(more, shown === matched);
      setHidden(empty, matched > 0 || rows.length === 0);
    }
    // Saisie : appliquée après une courte pause (pas à chaque touche) ; liste déroulante : immédiat.
    function onInput(e) {
      clearTimeout(timer);
      if (e.target.tagName === "SELECT") apply();
      else timer = setTimeout(apply, 120);
    }
    table.addEventListener("input", onInput);
    search.addEventListener("input", onInput);
    more.querySelector("button").addEventListener("click", function () { showAll = true; apply(); });

    // Détail : ligne créée au premier clic, chargée depuis le serveur ; un échec se réessaie en rouvrant.
    function load(r) {
      var td = r.detail.firstChild;
      r.loaded = true;
      td.textContent = "Chargement…";
      fetch(r.tr.getAttribute("data-detail"), {headers: {"HX-Request": "true"}, credentials: "same-origin"})
        .then(function (res) { if (!res.ok) throw res.status; return res.text(); })
        .then(function (html) { td.innerHTML = html; })  // fragment rendu (et échappé) par le serveur
        .catch(function (status) {
          r.loaded = false;
          td.textContent = status === 401 ? "Session expirée : recharge la page."
            : "Détail indisponible : replie puis rouvre la ligne pour réessayer.";
        });
    }
    function toggle(r) {
      var btn = r.tr.querySelector(".comp-toggle");
      r.open = !r.open;
      btn.setAttribute("aria-expanded", String(r.open));
      if (!r.detail) {
        r.detail = document.createElement("tr");
        r.detail.className = "detail";
        var td = r.detail.appendChild(document.createElement("td"));
        td.id = btn.getAttribute("aria-controls");
        td.colSpan = r.tr.cells.length;
        r.tr.parentNode.insertBefore(r.detail, r.tr.nextSibling);
      }
      setHidden(r.detail, !r.open);
      if (r.open && !r.loaded) load(r);
    }
    table.addEventListener("click", function (e) {
      var tr = e.target.closest("tr.comp");
      if (!tr || e.target.closest("a") || !window.getSelection().isCollapsed) return;  // lien, ou texte sélectionné
      toggle(tr.compRow);
    });
    apply();
  }
  document.addEventListener("DOMContentLoaded", initComponents);

  // Session expirée pendant une requête HTMX : retour à la page de connexion.
  document.addEventListener("htmx:responseError", function (e) {
    if (e.detail.xhr.status === 403) window.alert("Session expirée ou jeton invalide : recharge la page.");
  });
})();
