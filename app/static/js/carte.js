/* Carte des parcelles : recherche, filtres, légende, panneau de détail, outils.
   Toutes les valeurs venant du serveur sont écrites avec textContent (jamais de HTML injecté). */
(function () {
  var cfg = JSON.parse(document.getElementById('config-carte').textContent);
  var couleurs = {}, libelles = {};
  cfg.couleurs.forEach(function (l) { couleurs[l.code] = l.couleur; libelles[l.code] = l.label; });
  var $ = function (id) { return document.getElementById(id); };

  // ------------------------------------------------------------ carte et fonds (partagés avec la fiche d'un dossier)
  var carte = L.map('carte-generale', {maxZoom: 19, zoomControl: true}).setView([-11.9, 43.9], 8);
  SIGFCarte.fonds(carte, cfg.tuiles);

  // ------------------------------------------------------------ état
  var entrees = [];                 // {f, couche, visible}
  var groupe = L.layerGroup().addTo(carte);
  var pointsGroupe = L.layerGroup();     // un point par parcelle quand on est loin : les tracés sont alors trop petits pour être vus
  var ZOOM_POINTS = 15;
  var selection = null;
  var barre = null;                 // barre d'outils partagée (mesure, position, plein écran, recadrage)
  function mesureActive() { return barre && barre.mesureActive(); }
  var filtres = {texte: '', origine: '', commune: '', statuts: {}};
  Object.keys(couleurs).forEach(function (k) { filtres.statuts[k] = true; });

  function norme(t) { return (t || '').toString().toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, ''); }
  function styleDe(e, choisi) {
    var c = couleurs[e.f.properties.statut_code] || '#0d6efd';
    return choisi ? {color: '#ffffff', weight: 4, fillColor: c, fillOpacity: 0.65} : {color: c, weight: 2, fillColor: c, fillOpacity: 0.35};
  }
  function texteRecherche(p) { return norme([p.numero, p.proprietaire, p.commune, p.village, p.ile].join(' ')); }
  function passe(e, ignorerStatut) {
    var p = e.f.properties;
    if (filtres.origine && p.origine !== filtres.origine) return false;
    if (filtres.commune && p.commune !== filtres.commune) return false;
    if (filtres.texte && e.texte.indexOf(filtres.texte) === -1) return false;
    return ignorerStatut || filtres.statuts[p.statut_code] !== false;
  }

  // ------------------------------------------------------------ panneau de détail
  function ligne(libelle, valeur) {
    var d = document.createElement('div');
    var l = document.createElement('span'); l.className = 'text-muted'; l.textContent = libelle + ' : ';
    var v = document.createElement('span'); v.textContent = valeur;
    d.appendChild(l); d.appendChild(v); return d;
  }
  function afficherDetail(e) {
    var p = e.f.properties, corps = $('detail-corps');
    corps.textContent = ''; corps.className = 'card-body small';
    var titre = document.createElement('div'); titre.className = 'fs-6 fw-semibold mb-1'; titre.textContent = p.numero; corps.appendChild(titre);
    var st = document.createElement('div'); st.className = 'mb-2';
    var pastille = document.createElement('span'); pastille.className = 'badge'; pastille.style.background = couleurs[p.statut_code] || '#6c757d'; pastille.textContent = p.statut;
    st.appendChild(pastille); corps.appendChild(st);
    if (p.proprietaire) corps.appendChild(ligne('Propriétaire', p.proprietaire));
    corps.appendChild(ligne('Localisation', [p.ile, p.commune, p.village].filter(Boolean).join(' · ')));
    corps.appendChild(ligne('Superficie du tracé', p.superficie_m2.toLocaleString('fr-FR') + ' m²'));
    corps.appendChild(ligne('Dossier', p.origine === 'ancien' ? 'Ancien (numérisé)' : 'Nouveau'));
    var actions = document.createElement('div'); actions.className = 'd-flex flex-wrap gap-2 mt-3';
    var a = document.createElement('a'); a.href = p.url; a.className = 'btn btn-primary btn-sm'; a.textContent = 'Ouvrir le dossier';
    var g = document.createElement('a'); g.href = p.google_earth; g.className = 'btn btn-outline-secondary btn-sm'; g.textContent = 'Google Earth';
    actions.appendChild(a); actions.appendChild(g); corps.appendChild(actions);
  }
  function selectionner(e, cadrer) {
    if (selection) selection.couche.setStyle(styleDe(selection, false));
    selection = e;
    if (!groupe.hasLayer(e.couche)) groupe.addLayer(e.couche);
    e.couche.setStyle(styleDe(e, true)); e.couche.bringToFront();
    afficherDetail(e);
    $('resultats').hidden = true;                                   // la liste se referme : le détail devient visible
    if (typeof $('detail').scrollIntoView === 'function') $('detail').scrollIntoView({block: 'nearest', behavior: 'smooth'});
    if (cadrer) carte.fitBounds(e.couche.getBounds(), {padding: [60, 60], maxZoom: 19});
  }

  // ------------------------------------------------------------ filtres, légende, résultats
  function appliquer() {
    var visibles = 0, parStatut = {};
    entrees.forEach(function (e) {
      var ok = passe(e, false);
      if (passe(e, true)) parStatut[e.f.properties.statut_code] = (parStatut[e.f.properties.statut_code] || 0) + 1;
      if (ok && !groupe.hasLayer(e.couche)) { groupe.addLayer(e.couche); pointsGroupe.addLayer(e.point); }
      if (!ok && groupe.hasLayer(e.couche)) { groupe.removeLayer(e.couche); pointsGroupe.removeLayer(e.point); }
      if (ok) visibles++;
    });
    document.querySelectorAll('[data-nb]').forEach(function (n) { n.textContent = parStatut[n.getAttribute('data-nb')] || 0; });
    $('compte-carte').textContent = visibles + ' parcelle' + (visibles > 1 ? 's' : '') + ' affichée' + (visibles > 1 ? 's' : '') + ' sur ' + entrees.length + ' avec tracé';
    resultats();
  }
  function resultats() {
    var zone = $('resultats'); zone.textContent = '';
    if (!filtres.texte) { zone.hidden = true; return; }
    var trouves = entrees.filter(function (e) { return passe(e, false); });
    zone.hidden = false;
    if (!trouves.length) { var v = document.createElement('div'); v.className = 'text-muted px-2 py-1'; v.textContent = 'Aucune parcelle ne correspond.'; zone.appendChild(v); return; }
    trouves.slice(0, 8).forEach(function (e) {
      var b = document.createElement('button'); b.type = 'button';
      var n = document.createElement('strong'); n.textContent = e.f.properties.numero;
      var s = document.createElement('span'); s.className = 'text-muted'; s.textContent = ' · ' + (e.f.properties.proprietaire || e.f.properties.commune);
      b.appendChild(n); b.appendChild(s);
      b.addEventListener('click', function () { selectionner(e, true); });
      zone.appendChild(b);
    });
    if (trouves.length > 8) {
      var plus = document.createElement('div'); plus.className = 'text-muted px-2 py-1'; plus.textContent = '… et ' + (trouves.length - 8) + ' autres (affinez la recherche)';
      zone.appendChild(plus);
    }
    if (trouves.length > 1) {
      var tout = document.createElement('button'); tout.type = 'button'; tout.className = 'text-primary'; tout.textContent = 'Cadrer les ' + trouves.length + ' résultats';
      tout.addEventListener('click', function () { carte.fitBounds(L.featureGroup(trouves.map(function (e) { return e.couche; })).getBounds(), {padding: [40, 40]}); });
      zone.appendChild(tout);
    }
  }

  $('recherche').addEventListener('focus', function () { if (filtres.texte) resultats(); });
  $('recherche').addEventListener('input', function () { filtres.texte = norme(this.value.trim()); appliquer(); });
  $('recherche').addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter') return;
    ev.preventDefault();
    var trouves = entrees.filter(function (e) { return passe(e, false); });
    if (trouves.length === 1) selectionner(trouves[0], true);
    else if (trouves.length) carte.fitBounds(L.featureGroup(trouves.map(function (e) { return e.couche; })).getBounds(), {padding: [40, 40]});
  });
  $('f-origine').addEventListener('change', function () { filtres.origine = this.value; appliquer(); });
  $('f-commune').addEventListener('change', function () { filtres.commune = this.value; appliquer(); });
  document.querySelectorAll('#legende input').forEach(function (c) {
    c.addEventListener('change', function () { filtres.statuts[c.getAttribute('data-statut')] = c.checked; appliquer(); });
  });
  $('tout-afficher').addEventListener('click', function () {
    document.querySelectorAll('#legende input').forEach(function (c) { c.checked = true; filtres.statuts[c.getAttribute('data-statut')] = true; });
    appliquer();
  });

  // ------------------------------------------------------------ outils (code partagé)
  barre = SIGFCarte.outils(carte, {
    zone: $('zone-carte'), mesure: $('mesure'), titreRecadrer: 'Recadrer sur toutes les parcelles affichées',
    recadrer: function () {
      var vis = entrees.filter(function (e) { return passe(e, false); });
      if (vis.length) carte.fitBounds(L.featureGroup(vis.map(function (e) { return e.couche; })).getBounds(), {padding: [30, 30]});
    }
  });

  function majPoints() {
    var loin = carte.getZoom() < ZOOM_POINTS;
    if (loin && !carte.hasLayer(pointsGroupe)) carte.addLayer(pointsGroupe);
    if (!loin && carte.hasLayer(pointsGroupe)) carte.removeLayer(pointsGroupe);
  }
  carte.on('zoomend', majPoints);

  // ------------------------------------------------------------ chargement des parcelles
  fetch('/carte/donnees.json' + (cfg.ile ? '?ile=' + cfg.ile : ''), {credentials: 'same-origin'})
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var communes = {};
      data.features.forEach(function (f) {
        var e = {f: f, texte: texteRecherche(f.properties)};
        e.couche = L.geoJSON(f, {style: function () { return styleDe(e, false); }});
        e.couche.on('click', function (ev) { if (mesureActive()) return; L.DomEvent.stopPropagation(ev); selectionner(e, false); });
        e.couche.on('mouseover', function () { if (selection !== e) e.couche.setStyle({weight: 3, fillOpacity: 0.5}); });
        e.couche.on('mouseout', function () { if (selection !== e) e.couche.setStyle(styleDe(e, false)); });
        var centre = e.couche.getBounds().getCenter();
        e.point = L.circleMarker(centre, {radius: 6, color: '#ffffff', weight: 2, fillColor: couleurs[f.properties.statut_code] || '#0d6efd', fillOpacity: 0.95});
        e.point.on('click', function () { if (!mesureActive()) selectionner(e, true); });
        e.point.bindTooltip(f.properties.numero);
        entrees.push(e);
        if (f.properties.commune) communes[f.properties.commune] = true;
      });
      Object.keys(communes).sort(function (a, b) { return a.localeCompare(b, 'fr'); }).forEach(function (c) {
        var o = document.createElement('option'); o.value = c; o.textContent = c; $('f-commune').appendChild(o);
      });
      appliquer();
      majPoints();
      if (entrees.length) carte.fitBounds(L.featureGroup(entrees.map(function (e) { return e.couche; })).getBounds(), {padding: [20, 20]});
      if (data.tronque) $('compte-carte').textContent += ' (limité aux ' + data.features.length + ' premières sur ' + data.total + ')';
    })
    .catch(function () { $('compte-carte').textContent = 'Impossible de charger les parcelles.'; });
})();
