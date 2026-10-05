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

  // Session expirée pendant une requête HTMX : retour à la page de connexion.
  document.addEventListener("htmx:responseError", function (e) {
    if (e.detail.xhr.status === 403) window.alert("Session expirée ou jeton invalide : recharge la page.");
  });
})();
