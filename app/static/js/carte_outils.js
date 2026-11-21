/* Éléments communs aux cartes (carte générale et fiche d'un dossier) : fonds de carte, échelle, barre d'outils.
   Même aspect et mêmes outils partout. */
window.SIGFCarte = (function () {
  // Fonds Satellite / Plan (et Google si une clé est configurée) + sélecteur + échelle. Zoom limité à 19 (~échelle 20 m)
  // car l'imagerie Esri n'existe pas au-delà sur les Comores ; Google descend jusqu'à 21.
  function fonds(carte, tuiles) {
    var sat = L.tileLayer(tuiles.satellite.url, {maxZoom: 19, maxNativeZoom: tuiles.satellite.zoom_natif, attribution: tuiles.satellite.attribution}).addTo(carte);
    var plan = L.tileLayer(tuiles.plan.url, {maxZoom: 19, maxNativeZoom: tuiles.plan.zoom_natif, attribution: tuiles.plan.attribution});
    var liste = {'Satellite': sat, 'Plan': plan};
    if (window.google && L.gridLayer && L.gridLayer.googleMutant) {
      var gsat = L.gridLayer.googleMutant({type: 'satellite', maxZoom: 21});
      liste = {'Satellite Google': gsat, 'Satellite Esri': sat, 'Plan': plan};
      carte.removeLayer(sat); gsat.addTo(carte); carte.setMaxZoom(21);
    }
    L.control.layers(liste).addTo(carte);
    L.control.scale({imperial: false, position: 'bottomleft'}).addTo(carte);
  }

  function distance(a, b) {   // haversine, en mètres
    var R = 6371008.8, r = Math.PI / 180, dLat = (b.lat - a.lat) * r, dLng = (b.lng - a.lng) * r;
    var h = Math.sin(dLat / 2) * Math.sin(dLat / 2) + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLng / 2) * Math.sin(dLng / 2);
    return 2 * R * Math.asin(Math.sqrt(h));
  }
  function aire(pts) {        // polygone sphérique, en m²
    var R = 6378137, r = Math.PI / 180, s = 0;
    for (var i = 0; i < pts.length; i++) {
      var a = pts[i], b = pts[(i + 1) % pts.length];
      s += (b.lng - a.lng) * r * (2 + Math.sin(a.lat * r) + Math.sin(b.lat * r));
    }
    return Math.abs(s * R * R / 2);
  }
  function fmtM(m) { return m >= 1000 ? (m / 1000).toLocaleString('fr-FR', {maximumFractionDigits: 2}) + ' km' : Math.round(m).toLocaleString('fr-FR') + ' m'; }
  function fmtA(m2) { return Math.round(m2).toLocaleString('fr-FR') + ' m²' + (m2 >= 10000 ? ' (' + (m2 / 10000).toLocaleString('fr-FR', {maximumFractionDigits: 2}) + ' ha)' : ''); }

  // opts : zone (élément qui passe en plein écran et porte l'affichage de mesure), recadrer (fonction), mesure (élément d'affichage)
  // Retourne {mesureActive: function () -> bool}.
  function outils(carte, opts) {
    var etat = {actif: false}, points = [], trace = null, marques = L.layerGroup().addTo(carte), zoneMesure = opts.mesure;
    function maj() {
      if (!etat.actif) { zoneMesure.style.display = 'none'; return; }
      zoneMesure.style.display = 'block';
      if (!points.length) { zoneMesure.textContent = 'Cliquez sur la carte pour tracer. Cliquez de nouveau sur la règle pour terminer.'; return; }
      var total = 0; for (var i = 1; i < points.length; i++) total += distance(points[i - 1], points[i]);
      zoneMesure.textContent = 'Longueur : ' + fmtM(total) + (points.length >= 3 ? ' · Surface : ' + fmtA(aire(points)) : '');
    }
    function effacer() {
      points = []; marques.clearLayers();
      if (trace) { carte.removeLayer(trace); trace = null; }
    }
    function basculer(bouton) {
      etat.actif = !etat.actif;
      bouton.classList.toggle('actif', etat.actif);
      carte.getContainer().style.cursor = etat.actif ? 'crosshair' : '';
      if (!etat.actif) effacer();
      maj();
    }
    carte.on('click', function (ev) {
      if (!etat.actif) return;
      points.push(ev.latlng);
      L.circleMarker(ev.latlng, {radius: 4, color: '#fff', weight: 2, fillColor: '#0d6efd', fillOpacity: 1, interactive: false}).addTo(marques);
      if (trace) carte.removeLayer(trace);
      trace = (points.length >= 3 ? L.polygon : L.polyline)(points, {color: '#0d6efd', weight: 3, fillOpacity: 0.15, interactive: false}).addTo(carte);
      maj();
    });

    var Barre = L.Control.extend({
      options: {position: 'topleft'},
      onAdd: function () {
        var conteneur = L.DomUtil.create('div', 'carte-outils');
        L.DomEvent.disableClickPropagation(conteneur);
        function bouton(icone, titre, action) {
          var b = L.DomUtil.create('button', '', conteneur);
          b.type = 'button'; b.title = titre; b.setAttribute('aria-label', titre);
          var i = document.createElement('i'); i.className = 'bi bi-' + icone; b.appendChild(i);
          b.addEventListener('click', function () { action(b); });
        }
        bouton('rulers', 'Mesurer une distance ou une surface', basculer);
        bouton('geo-alt', 'Ma position', function () { carte.locate({setView: true, maxZoom: 18}); });
        bouton('fullscreen', 'Plein écran', function () {
          if (document.fullscreenElement) document.exitFullscreen(); else if (opts.zone.requestFullscreen) opts.zone.requestFullscreen();
        });
        if (opts.recadrer) bouton('bullseye', opts.titreRecadrer || 'Recadrer', opts.recadrer);
        return conteneur;
      }
    });
    carte.addControl(new Barre());

    var position = null;
    carte.on('locationfound', function (ev) {
      if (position) carte.removeLayer(position);
      position = L.circleMarker(ev.latlng, {radius: 7, color: '#fff', weight: 2, fillColor: '#2a78d6', fillOpacity: 1}).addTo(carte).bindTooltip('Vous êtes ici (± ' + Math.round(ev.accuracy) + ' m)');
    });
    carte.on('locationerror', function () {
      etat.actif = false; zoneMesure.style.display = 'block'; zoneMesure.textContent = 'Position indisponible (autorisation refusée ou appareil sans géolocalisation).';
      setTimeout(function () { if (!etat.actif) zoneMesure.style.display = 'none'; }, 4000);
    });
    document.addEventListener('fullscreenchange', function () { setTimeout(function () { carte.invalidateSize(); }, 150); });
    return {mesureActive: function () { return etat.actif; }};
  }

  return {fonds: fonds, outils: outils};
})();
