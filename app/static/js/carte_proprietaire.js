/* Carte des biens d'une personne (profil d'un propriétaire). Mêmes fonds, échelle et outils que la carte des parcelles.
   Les textes venant du serveur sont écrits avec textContent (jamais de HTML injecté). */
(function () {
  var donnees = JSON.parse(document.getElementById('donnees-carte-proprio').textContent);
  var carte = L.map('carte-proprio', {scrollWheelZoom: false, maxZoom: 19});
  SIGFCarte.fonds(carte, donnees.tuiles);

  var couches = [], points = L.layerGroup(), ZOOM_POINTS = 15;
  donnees.carte.features.forEach(function (f) {
    var p = f.properties;
    var style = p.ancien
      ? {color: p.couleur, weight: 2, fillColor: p.couleur, fillOpacity: 0.2, dashArray: '6 4'}
      : {color: '#ffffff', weight: 3, fillColor: p.couleur, fillOpacity: 0.55};
    var couche = L.geoJSON(f, {style: function () { return style; }}).addTo(carte);
    var div = document.createElement('div');
    var a = document.createElement('a'); a.href = p.url; a.className = 'fw-semibold'; a.textContent = p.numero; div.appendChild(a);
    [p.statut, p.localisation, 'Part : ' + p.part, p.superficie_m2.toLocaleString('fr-FR') + ' m² (tracé)'].forEach(function (t) {
      if (!t) return; var l = document.createElement('div'); l.className = 'small'; l.textContent = t; div.appendChild(l);
    });
    couche.bindPopup(div);
    couche.on('mouseover', function () { couche.setStyle({weight: 4}); });
    couche.on('mouseout', function () { couche.setStyle(style); });
    couches.push(couche);
    var pt = L.circleMarker(couche.getBounds().getCenter(), {radius: 6, color: '#ffffff', weight: 2, fillColor: p.couleur, fillOpacity: 0.95});
    pt.bindTooltip(p.numero);
    pt.on('click', function () { carte.fitBounds(couche.getBounds(), {padding: [40, 40], maxZoom: 19}); couche.openPopup(); });
    points.addLayer(pt);
  });

  function recadrer() { carte.fitBounds(L.featureGroup(couches).getBounds(), {padding: [30, 30], maxZoom: 19}); }
  SIGFCarte.outils(carte, {zone: document.getElementById('zone-carte-biens'), mesure: document.getElementById('mesure-proprio'), recadrer: recadrer, titreRecadrer: 'Recadrer sur tous les biens'});
  function majPoints() {
    var loin = carte.getZoom() < ZOOM_POINTS;
    if (loin && !carte.hasLayer(points)) carte.addLayer(points);
    if (!loin && carte.hasLayer(points)) carte.removeLayer(points);
  }
  carte.on('zoomend', majPoints);
  recadrer(); majPoints();
  carte.on('click', function () { carte.scrollWheelZoom.enable(); });
})();
