/* Graphiques du tableau de bord : HTML/SVG sans bibliothèque.
   Règles : texte inséré avec textContent (jamais en HTML : les libellés sont des données),
   survol ET clavier, vue en tableau pour chaque graphique, aucune valeur réservée à l'infobulle. */
(function () {
  'use strict';
  var SVG = 'http://www.w3.org/2000/svg';
  var COULEURS = ['var(--viz-s1)', 'var(--viz-s2)', 'var(--viz-s3)'];

  // ---------- petits utilitaires DOM ----------
  function appliquer(e, attrs) {
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined) return;
      if (k === 'texte') e.textContent = v; else e.setAttribute(k, v);
    });
  }
  function ajouter(e, enfants) {
    (enfants || []).forEach(function (c) { if (c) e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
  }
  function h(tag, attrs, enfants) { var e = document.createElement(tag); appliquer(e, attrs); ajouter(e, enfants); return e; }
  function s(tag, attrs, enfants) { var e = document.createElementNS(SVG, tag); appliquer(e, attrs); ajouter(e, enfants); return e; }
  function nombre(v, dec) {
    return Number(v).toLocaleString('fr-FR', { maximumFractionDigits: dec === undefined ? 1 : dec });
  }

  // ---------- infobulle partagée ----------
  var tip = null;
  function montrer(contenu, x, y) {
    if (!tip) { tip = h('div', { 'class': 'viz-tip', role: 'tooltip', hidden: '' }); document.body.appendChild(tip); }
    tip.textContent = '';
    ajouter(tip, contenu);
    tip.hidden = false;
    var r = tip.getBoundingClientRect();
    var gauche = x + 14, haut = y + 14;
    if (gauche + r.width > window.innerWidth - 8) gauche = x - r.width - 14;
    if (haut + r.height > window.innerHeight - 8) haut = y - r.height - 14;
    tip.style.left = Math.max(8, gauche) + 'px';
    tip.style.top = Math.max(8, haut) + 'px';
  }
  function cacher() { if (tip) tip.hidden = true; }

  // ---------- barres horizontales ----------
  function texteValeur(it, opt) { return nombre(it.valeur, opt.decimales) + (opt.unite || ''); }

  function barres(zone, items, opt) {
    zone.textContent = '';
    var max = Math.max.apply(null, items.map(function (i) { return i.valeur; }).concat([0]));
    var total = items.reduce(function (a, i) { return a + i.valeur; }, 0);
    if (!items.length || max === 0) {
      zone.appendChild(h('p', { 'class': 'viz-vide', texte: opt.vide || 'Aucune donnée sur cette période.' }));
      return;
    }
    items.forEach(function (it) {
      var libelle = texteValeur(it, opt);
      var ligne = h(it.url ? 'a' : 'div', { 'class': 'viz-ligne', tabindex: '0', href: it.url || null, 'aria-label': it.nom + ' : ' + libelle });
      var barre = h('span', { 'class': 'viz-barre' + (it.actif === false ? ' inactif' : '') });
      barre.style.width = (it.valeur / max * 82) + '%';   // le reste de la piste héberge la valeur, jamais rognée
      ligne.appendChild(h('span', { 'class': 'viz-etiquette', texte: it.nom }));
      ligne.appendChild(h('span', { 'class': 'viz-piste' }, [it.valeur > 0 ? barre : null, h('span', { 'class': 'viz-valeur', texte: libelle })]));

      function contenu() {
        var c = [h('div', { 'class': 'viz-tip-val', texte: libelle }), h('div', { 'class': 'viz-tip-nom', texte: it.nom })];
        if (opt.part && total > 0) c.push(h('div', { 'class': 'viz-tip-nom', texte: nombre(it.valeur / total * 100, 0) + ' % du total' }));
        if (opt.detail && it.n !== undefined) c.push(h('div', { 'class': 'viz-tip-nom', texte: 'sur ' + it.n + ' dossier(s)' }));
        return c;
      }
      ligne.addEventListener('pointermove', function (e) { montrer(contenu(), e.clientX, e.clientY); });
      ligne.addEventListener('pointerleave', cacher);
      ligne.addEventListener('focus', function () { var r = ligne.getBoundingClientRect(); montrer(contenu(), r.left + r.width / 2, r.top); });
      ligne.addEventListener('blur', cacher);
      zone.appendChild(ligne);
    });
  }

  // ---------- courbes ----------
  function graduations(max) {
    var pas = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000];
    var p = pas.filter(function (x) { return max / x <= 4; })[0] || 20000;
    var haut = Math.max(p, Math.ceil(max / p) * p), t = [];
    for (var v = 0; v <= haut; v += p) t.push(v);
    return t;
  }

  function courbes(zone, data) {
    zone.textContent = '';
    var series = data.series, n = data.mois.length;
    var toutes = [];
    series.forEach(function (x) { toutes = toutes.concat(x.valeurs); });
    if (!toutes.some(function (v) { return v > 0; })) {
      zone.appendChild(h('p', { 'class': 'viz-vide', texte: 'Aucune activité sur cette période.' }));
      return;
    }
    var legende = h('div', { 'class': 'viz-legende' });
    series.forEach(function (x, i) {
      legende.appendChild(h('span', { 'class': 'viz-leg' }, [h('span', { 'class': 'viz-cle', style: 'background:' + COULEURS[i] }), x.nom]));
    });
    zone.appendChild(legende);

    var L = Math.max(zone.clientWidth, 300), H = 240, mg = { g: 34, d: 44, h: 10, b: 26 };
    var graduation = graduations(Math.max.apply(null, toutes));
    var ymax = graduation[graduation.length - 1];
    var pasX = n > 1 ? (L - mg.g - mg.d) / (n - 1) : 0;
    function X(i) { return n > 1 ? mg.g + i * pasX : (L + mg.g - mg.d) / 2; }
    function Y(v) { return mg.h + (1 - v / ymax) * (H - mg.h - mg.b); }

    var svg = s('svg', { 'class': 'viz-svg', viewBox: '0 0 ' + L + ' ' + H, role: 'img', tabindex: '0',
      'aria-label': 'Courbes mensuelles : ' + series.map(function (x) { return x.nom; }).join(', ') + '. Les valeurs sont aussi disponibles en tableau.' });
    svg.style.height = H + 'px';

    var axes = s('g', { 'class': 'viz-axe' });
    graduation.forEach(function (v) {   // repères : fines lignes pleines, la base un cran plus marquée
      axes.appendChild(s('line', { x1: mg.g, x2: L - mg.d, y1: Y(v), y2: Y(v), stroke: v === 0 ? 'var(--viz-base)' : 'var(--viz-grid)', 'stroke-width': 1 }));
      axes.appendChild(s('text', { x: mg.g - 7, y: Y(v) + 4, 'text-anchor': 'end', texte: nombre(v, 0) }));
    });
    var saut = Math.max(1, Math.ceil(46 / (pasX || 46)));
    data.mois.forEach(function (m, i) {
      if (i % saut === 0 || i === n - 1) axes.appendChild(s('text', { x: X(i), y: H - 7, 'text-anchor': 'middle', texte: m }));
    });
    svg.appendChild(axes);

    series.forEach(function (x, i) {
      var d = x.valeurs.map(function (v, j) { return (j ? 'L' : 'M') + X(j).toFixed(1) + ' ' + Y(v).toFixed(1); }).join(' ');
      svg.appendChild(s('path', { d: d, fill: 'none', stroke: COULEURS[i], 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
    });
    // points d'arrivée, bague de 2 px couleur du fond ; valeur écrite seulement si les étiquettes ne se touchent pas
    var fins = series.map(function (x) { return Y(x.valeurs[n - 1]); });
    var ordre = fins.slice().sort(function (a, b) { return a - b; });
    var libres = ordre.every(function (y, k) { return k === 0 || y - ordre[k - 1] >= 14; });
    series.forEach(function (x, i) {
      svg.appendChild(s('circle', { cx: X(n - 1), cy: fins[i], r: 4, fill: COULEURS[i], stroke: 'var(--viz-surface)', 'stroke-width': 2 }));
      if (libres) svg.appendChild(s('text', { 'class': 'viz-fin-valeur', x: X(n - 1) + 10, y: fins[i] + 4, texte: nombre(x.valeurs[n - 1], 0) }));
    });

    // couche de survol : le réticule trouve le mois, une seule infobulle liste toutes les séries
    var reticule = s('line', { y1: mg.h, y2: H - mg.b, stroke: 'var(--viz-base)', 'stroke-width': 1, visibility: 'hidden' });
    svg.appendChild(reticule);
    var points = series.map(function (x, i) {
      var c = s('circle', { r: 4, fill: COULEURS[i], stroke: 'var(--viz-surface)', 'stroke-width': 2, visibility: 'hidden' });
      svg.appendChild(c); return c;
    });
    var courant = n - 1;
    function afficher(i, cx, cy) {
      courant = i;
      reticule.setAttribute('x1', X(i)); reticule.setAttribute('x2', X(i)); reticule.setAttribute('visibility', 'visible');
      points.forEach(function (p, k) { p.setAttribute('cx', X(i)); p.setAttribute('cy', Y(series[k].valeurs[i])); p.setAttribute('visibility', 'visible'); });
      var lignes = series.map(function (x, k) {
        return h('div', { 'class': 'viz-tip-ligne' }, [
          h('span', { 'class': 'gauche' }, [h('span', { 'class': 'viz-tip-cle', style: 'background:' + COULEURS[k] }), x.nom]),
          h('span', { 'class': 'viz-tip-val', texte: nombre(x.valeurs[i], 0) })]);
      });
      montrer([h('div', { 'class': 'viz-tip-tete', texte: data.mois[i] })].concat(lignes), cx, cy);
    }
    function masquer() {
      reticule.setAttribute('visibility', 'hidden');
      points.forEach(function (p) { p.setAttribute('visibility', 'hidden'); });
      cacher();
    }
    var capteur = s('rect', { x: mg.g, y: 0, width: L - mg.g - mg.d + 12, height: H - mg.b, fill: 'transparent' });
    capteur.addEventListener('pointermove', function (e) {
      var r = svg.getBoundingClientRect();
      var px = (e.clientX - r.left) * (L / r.width);
      var i = n > 1 ? Math.max(0, Math.min(n - 1, Math.round((px - mg.g) / pasX))) : 0;
      afficher(i, e.clientX, e.clientY);
    });
    capteur.addEventListener('pointerleave', masquer);
    svg.appendChild(capteur);
    svg.addEventListener('keydown', function (e) {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      var i = Math.max(0, Math.min(n - 1, courant + (e.key === 'ArrowRight' ? 1 : -1)));
      var r = svg.getBoundingClientRect();
      afficher(i, r.left + X(i) * (r.width / L), r.top + 20);
    });
    svg.addEventListener('focus', function () { var r = svg.getBoundingClientRect(); afficher(courant, r.left + X(courant) * (r.width / L), r.top + 20); });
    svg.addEventListener('blur', masquer);
    zone.appendChild(svg);
  }

  // ---------- mini-courbe d'une tuile ----------
  function minicourbe(zone) {
    var v = zone.getAttribute('data-valeurs').split(',').map(Number), n = v.length;
    var L = 96, H = 32, max = Math.max.apply(null, v.concat([1]));
    function X(i) { return 5 + i * (L - 10) / (n - 1); }
    function Y(x) { return H - 6 - x / max * (H - 12); }
    var svg = s('svg', { viewBox: '0 0 ' + L + ' ' + H, width: L, height: H, role: 'img', 'aria-label': 'Tendance sur ' + n + ' mois : ' + v.join(', ') });
    var d = v.map(function (x, i) { return (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(x).toFixed(1); }).join(' ');
    svg.appendChild(s('path', { d: d, fill: 'none', stroke: 'var(--viz-gris)', 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
    svg.appendChild(s('path', { d: 'M' + X(n - 2).toFixed(1) + ' ' + Y(v[n - 2]).toFixed(1) + ' L' + X(n - 1).toFixed(1) + ' ' + Y(v[n - 1]).toFixed(1),
      fill: 'none', stroke: 'var(--viz-s1)', 'stroke-width': 2, 'stroke-linecap': 'round' }));
    svg.appendChild(s('circle', { cx: X(n - 1), cy: Y(v[n - 1]), r: 4, fill: 'var(--viz-s1)', stroke: 'var(--viz-surface)', 'stroke-width': 2 }));
    zone.textContent = '';
    zone.appendChild(svg);
  }

  // ---------- vue en tableau (équivalent accessible de chaque graphique) ----------
  function tableau(cible, colonnes, lignes) {
    cible.textContent = '';
    var thead = h('thead', {}, [h('tr', {}, colonnes.map(function (c) { return h('th', { scope: 'col', texte: c }); }))]);
    var tbody = h('tbody', {}, lignes.map(function (l) { return h('tr', {}, l.map(function (c, i) { return h(i ? 'td' : 'th', i ? { texte: c } : { scope: 'row', texte: c }); })); }));
    cible.appendChild(h('table', { 'class': 'table table-sm table-borderless' }, [thead, tbody]));
  }

  // ---------- montage ----------
  function monter() {
    var source = document.getElementById('donnees-dashboard');
    if (!source) return;
    var donnees = JSON.parse(source.textContent);

    document.querySelectorAll('[data-trace]').forEach(minicourbe);

    document.querySelectorAll('.viz-carte[data-cle]').forEach(function (carte) {
      var cle = carte.getAttribute('data-cle'), type = carte.getAttribute('data-type');
      var zone = carte.querySelector('.viz-trace-zone'), cibleTableau = carte.querySelector('.viz-tableau');
      var opt = { unite: carte.getAttribute('data-unite') || '', decimales: carte.hasAttribute('data-decimales') ? Number(carte.getAttribute('data-decimales')) : 0,
        part: carte.hasAttribute('data-part'), detail: carte.hasAttribute('data-detail'), vide: carte.getAttribute('data-vide') };
      var d = donnees[cle];
      function dessiner() { if (type === 'courbes') courbes(zone, d); else barres(zone, d, opt); }
      dessiner();

      if (type === 'courbes') {
        tableau(cibleTableau, ['Mois'].concat(d.series.map(function (x) { return x.nom; })),
          d.mois.map(function (m, i) { return [m].concat(d.series.map(function (x) { return nombre(x.valeurs[i], 0); })); }));
      } else {
        var total = d.reduce(function (a, i) { return a + i.valeur; }, 0);
        var colonnes = ['', carte.getAttribute('data-colonne') || 'Nombre'];
        if (opt.part) colonnes.push('Part');
        tableau(cibleTableau, colonnes, d.map(function (i) {
          var l = [i.nom, nombre(i.valeur, opt.decimales) + opt.unite];
          if (opt.part) l.push(total ? nombre(i.valeur / total * 100, 0) + ' %' : '—');
          return l;
        }));
      }

      var bouton = carte.querySelector('.viz-bascule');
      bouton.addEventListener('click', function () {
        var enTableau = carte.getAttribute('data-vue') === 'tableau';
        carte.setAttribute('data-vue', enTableau ? 'graphique' : 'tableau');
        bouton.textContent = enTableau ? 'Voir en tableau' : 'Voir le graphique';
        bouton.setAttribute('aria-pressed', enTableau ? 'false' : 'true');
        if (enTableau) dessiner();   // un SVG caché n'a pas de largeur : on le redessine au retour
      });

      if (type === 'courbes' && 'ResizeObserver' in window) {
        var derniere = zone.clientWidth, minuteur = null;
        new ResizeObserver(function () {
          clearTimeout(minuteur);
          minuteur = setTimeout(function () {
            if (carte.getAttribute('data-vue') !== 'tableau' && Math.abs(zone.clientWidth - derniere) > 4) { derniere = zone.clientWidth; dessiner(); }
          }, 120);
        }).observe(zone);
      }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', monter); else monter();
})();
