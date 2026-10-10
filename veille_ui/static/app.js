// Comportements de l'interface — fichier statique (la CSP interdit le JS en ligne).
(function () {
  "use strict";

  // Confirmation des formulaires sensibles : <form data-confirm="…">.
  // Puis bouton marqué « en cours » : certaines actions passent par la CLI Hermes (~1 s) ; le
  // retour est immédiat et un second clic ne renvoie pas le formulaire.
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    var btn = e.submitter;
    if (!btn || e.defaultPrevented) return;
    btn.classList.add("busy");
    // Désactivé au tour suivant : le formulaire est déjà parti avec la valeur du bouton.
    setTimeout(function () { btn.disabled = true; }, 0);
    setTimeout(function () { btn.disabled = false; btn.classList.remove("busy"); }, 15000);  // filet
  });
  // Retour arrière (page restaurée depuis le cache) : boutons réactivés.
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    document.querySelectorAll("button.busy").forEach(function (b) { b.disabled = false; b.classList.remove("busy"); });
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

  // Destinataires d'une veille : puces, saisie avec autocomplétion sur le carnet d'adresses, ajout d'une
  // adresse du carnet au toucher ou par glisser-déposer (Pointer Events, souris et tactile : le glisser-déposer
  // HTML5 ne fonctionne pas dans les WebView mobiles ; appui long sur écran tactile, pour garder le défilement).
  // La zone de texte reste le champ envoyé (et la seule interface sans JavaScript) : elle est tenue à jour, et
  // le serveur valide les adresses comme avant.
  var EMAIL = /^[^@\s,;<>"]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/;  // même règle que store.EMAIL_RE
  var BOOK_SHOWN = 24;  // suggestions affichées : la recherche affine
  var HOLD_MS = 350;    // appui long avant de saisir une adresse sur écran tactile
  function el(tag, cls) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    return e;
  }
  function initRecipients(root) {
    var form = root.closest("form"), area = root.querySelector("textarea"), book = root.querySelector("[data-book]");
    var emails = [], justDragged = false, drag = null;
    var books = book ? Array.prototype.map.call(book.children, function (li) {
      var b = li.firstElementChild;  // <button data-email> rendu (et échappé) par le serveur
      return {li: li, email: b.getAttribute("data-email"), q: norm(b.getAttribute("data-email"))};
    }) : [];
    var box = el("div", "rcpt-box"), list = box.appendChild(el("ul", "rcpt-list"));
    var input = box.appendChild(el("input", "rcpt-input"));
    var msg = el("p", "small rcpt-msg"), more = book && el("li", "rcpt-more muted small");
    box.setAttribute("data-zone", "to");
    list.setAttribute("aria-label", "Destinataires");
    input.type = "text";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.setAttribute("inputmode", "email");
    input.setAttribute("autocapitalize", "off");
    input.setAttribute("aria-label", "Ajouter un destinataire");
    input.placeholder = book ? "Ajouter ou rechercher une adresse" : "Ajouter une adresse";
    msg.setAttribute("aria-live", "polite");
    area.required = false;  // champ masqué : « au moins un destinataire » est vérifié à l'envoi (ci-dessous)
    area.closest("label").hidden = true;
    root.insertBefore(box, area.closest("label"));
    root.insertBefore(msg, book);
    if (book) {
      var hint = el("p", "small muted rcpt-hint");
      hint.textContent = "Carnet d'adresses : toucher une adresse pour l'ajouter, ou la faire glisser dans les destinataires.";
      root.insertBefore(hint, book);
      book.setAttribute("data-zone", "book");
      book.appendChild(more);
      book.hidden = false;
    }

    function has(a) {
      a = a.toLowerCase();
      return emails.some(function (x) { return x.toLowerCase() === a; });
    }
    function say(text, bad) {
      msg.textContent = text;
      msg.classList.toggle("err", !!bad);
    }
    function render() {  // puces (quelques dizaines au plus) refaites, zone de texte et carnet synchronisés
      list.textContent = "";
      emails.forEach(function (a) {
        var li = list.appendChild(el("li", "rcpt-chip" + (EMAIL.test(a) ? "" : " rcpt-bad")));
        var x = el("button", "rcpt-x");
        li.setAttribute("data-email", a);
        li.appendChild(el("span")).textContent = a;
        x.type = "button";
        x.textContent = "×";
        x.setAttribute("aria-label", "Retirer " + a);
        li.appendChild(x);
      });
      area.value = emails.join("\n");
      input.setCustomValidity("");
      filter();
    }
    function filter() {  // autocomplétion : adresses du carnet pas encore destinataires, qui contiennent la saisie
      if (!book) return;
      var q = norm(input.value.trim()), matched = 0;
      books.forEach(function (b) {
        var ok = !has(b.email) && b.q.indexOf(q) !== -1;
        if (ok) matched++;
        setHidden(b.li, !(ok && matched <= BOOK_SHOWN));
      });
      more.textContent = matched > BOOK_SHOWN ? "+ " + (matched - BOOK_SHOWN) + " autre(s) : affiner la recherche"
        : matched ? "" : q ? "Aucune adresse du carnet ne correspond." : "Toutes les adresses du carnet sont destinataires.";
      setHidden(more, !more.textContent);
    }
    function add(a) {
      a = a.trim();
      if (!a) return true;
      if (!EMAIL.test(a)) { say("Adresse invalide : " + a, true); return false; }
      if (has(a)) { say(a + " est déjà destinataire."); return true; }
      emails.push(a);
      render();
      say("Destinataire ajouté : " + a);
      return true;
    }
    function remove(a) {
      emails = emails.filter(function (x) { return x !== a; });
      render();
      say("Destinataire retiré : " + a);
    }
    function firstSuggestion() {
      var b = books.filter(function (b) { return !b.li.hidden; })[0];
      return b && b.li.firstElementChild;
    }
    // Saisie validée (Entrée, séparateur, collage) : chaque adresse complète devient une puce ; une adresse
    // invalide reste dans le champ, signalée. `partial` : le dernier morceau, en cours de frappe, y reste aussi.
    function commit(partial) {
      var parts = input.value.split(/[\s,;]+/), rest = partial ? parts.pop() : "";
      var bad = parts.filter(function (a) { return !add(a); });
      input.value = bad.concat(rest ? [rest] : []).join(" ");
      filter();
    }

    area.value.split(/[\n,;]+/).forEach(function (a) {  // existants (même invalides : signalés en rouge)
      a = a.trim();
      if (a && a.charAt(0) !== "#" && !has(a)) emails.push(a);
    });
    render();

    input.addEventListener("input", function () {
      input.setCustomValidity("");  // sinon le navigateur bloquerait l'envoi avant même l'événement « submit »
      if (/[\s,;]/.test(input.value)) commit(!/[\s,;]$/.test(input.value));
      else filter();
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {  // jamais l'envoi du formulaire depuis ce champ
        e.preventDefault();
        var v = input.value.trim(), s = !EMAIL.test(v) && v && firstSuggestion();
        if (s) { add(s.getAttribute("data-email")); input.value = ""; filter(); } else commit(false);
      } else if (e.key === "Backspace" && !input.value && emails.length) {
        remove(emails[emails.length - 1]);
      } else if (e.key === "ArrowDown" && firstSuggestion()) {
        e.preventDefault();
        firstSuggestion().focus();
      }
    });
    input.addEventListener("blur", function () { if (EMAIL.test(input.value.trim())) commit(false); });
    box.addEventListener("click", function (e) {
      var x = e.target.closest(".rcpt-x");
      if (x && !justDragged) remove(x.parentNode.getAttribute("data-email"));
      else if (e.target === box || e.target === list) input.focus();
    });
    if (book) book.addEventListener("click", function (e) {
      var b = e.target.closest("[data-email]");
      if (!b || justDragged) return;
      add(b.getAttribute("data-email"));
      input.value = "";  // la recherche qui a servi à la trouver
      filter();
    });
    form.addEventListener("submit", function (e) {
      commit(false);
      area.value = emails.concat(input.value.trim() ? [input.value.trim()] : []).join("\n");  // le serveur juge
      if (!area.value) {
        e.preventDefault();
        input.setCustomValidity("Au moins un destinataire est requis.");
        input.reportValidity();
      }
    });

    // Glisser-déposer : du carnet vers les destinataires (ajout), ou l'inverse (retrait).
    function zoneAt(x, y) {
      var t = document.elementFromPoint(x, y);
      return t && t.closest("[data-zone]");
    }
    function moveTo(x, y) {
      var z = zoneAt(x, y);
      drag.ghost.style.transform = "translate(" + (x + 10) + "px, " + (y + 10) + "px)";  // CSSOM : permis par la CSP
      box.classList.toggle("rcpt-over", z === box && drag.from !== box);
      if (book) book.classList.toggle("rcpt-over", z === book && drag.from !== book);
    }
    function start() {
      clearTimeout(drag.timer);
      drag.on = true;
      drag.ghost = document.body.appendChild(drag.chip.cloneNode(true));
      drag.ghost.classList.add("rcpt-ghost");
      drag.ghost.setAttribute("aria-hidden", "true");
      drag.chip.classList.add("rcpt-held");
      root.classList.add("rcpt-dragging");
      moveTo(drag.x, drag.y);
    }
    function end() {
      if (!drag) return;
      clearTimeout(drag.timer);
      if (drag.on) {
        drag.ghost.remove();
        drag.chip.classList.remove("rcpt-held");
        root.classList.remove("rcpt-dragging");
        box.classList.remove("rcpt-over");
        if (book) book.classList.remove("rcpt-over");
        justDragged = true;  // le « click » qui suit le relâchement n'est pas un toucher
        setTimeout(function () { justDragged = false; }, 0);
      }
      drag = null;
    }
    root.addEventListener("pointerdown", function (e) {
      var chip = e.target.closest("[data-email]");
      if (!chip || e.button > 0 || e.target.closest(".rcpt-x")) return;
      drag = {chip: chip, email: chip.getAttribute("data-email"), from: chip.closest("[data-zone]"),
              x: e.clientX, y: e.clientY, on: false, timer: null};
      if (e.pointerType !== "mouse") drag.timer = setTimeout(start, HOLD_MS);
    });
    document.addEventListener("pointermove", function (e) {
      if (!drag) return;
      if (!drag.on) {
        if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) < 8) return;
        if (e.pointerType !== "mouse") return end();  // le doigt bouge avant l'appui long : c'est un défilement
        start();
      }
      moveTo(e.clientX, e.clientY);
    });
    document.addEventListener("pointerup", function (e) {
      if (drag && drag.on) {
        var z = zoneAt(e.clientX, e.clientY);
        if (z === box && drag.from !== box) add(drag.email);
        else if (z === book && drag.from === box) remove(drag.email);
      }
      end();
    });
    document.addEventListener("pointercancel", end);
    // Pendant un glisser au doigt, la page ne défile pas (sinon le navigateur annulerait le geste).
    document.addEventListener("touchmove", function (e) { if (drag && drag.on) e.preventDefault(); }, {passive: false});
    root.addEventListener("contextmenu", function (e) { if (drag) e.preventDefault(); });  // menu de l'appui long
  }
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-recipients]").forEach(initRecipients);
  });

  // Session expirée pendant une requête HTMX : retour à la page de connexion.
  document.addEventListener("htmx:responseError", function (e) {
    if (e.detail.xhr.status === 403) window.alert("Session expirée ou jeton invalide : recharge la page.");
  });
})();
