/* Cadre de signature : on dessine à la souris, au doigt ou au stylet. Les traits sont mémorisés en coordonnées relatives
   (le dessin survit aux rotations d'écran et aux redimensionnements) puis exportés en PNG transparent. */
(function () {
  var canvas = document.getElementById('cadre-signature');
  if (!canvas) return;
  var champ = document.getElementById('champ-dessin'), formulaire = canvas.closest('form');
  var ENCRE = '#10204f', EPAISSEUR = 0.0052;      // épaisseur relative à la largeur du cadre (≈ 3 px pour 600 px)
  var traits = [], courant = null, ctx = null, dpr = 1;

  function largeurTrait(p, largeur, stylet) { return Math.max(1.2, EPAISSEUR * largeur * (stylet ? 0.6 + 0.9 * (p || 0.5) : 1)); }
  function pt(e, r) { return {x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.width, p: e.pressure, s: e.pointerType === 'pen'}; }

  function dessinerTrait(c, trait, largeur) {          // courbe lissée par les milieux des segments
    var pts = trait.points; if (!pts.length) return;
    c.strokeStyle = c.fillStyle = ENCRE; c.lineCap = c.lineJoin = 'round';
    if (pts.length < 3) {
      var a = pts[0], b = pts[pts.length - 1];
      c.lineWidth = largeurTrait(a.p, largeur, a.s);
      c.beginPath(); c.moveTo(a.x * largeur, a.y * largeur); c.lineTo(b.x * largeur + 0.01, b.y * largeur); c.stroke(); return;
    }
    for (var i = 1; i < pts.length - 1; i++) {
      var p0 = pts[i - 1], p1 = pts[i], p2 = pts[i + 1];
      var mx0 = (p0.x + p1.x) / 2 * largeur, my0 = (p0.y + p1.y) / 2 * largeur, mx1 = (p1.x + p2.x) / 2 * largeur, my1 = (p1.y + p2.y) / 2 * largeur;
      c.lineWidth = largeurTrait(p1.p, largeur, p1.s);
      c.beginPath(); c.moveTo(mx0, my0); c.quadraticCurveTo(p1.x * largeur, p1.y * largeur, mx1, my1); c.stroke();
    }
  }
  function redessiner() {
    var r = canvas.getBoundingClientRect();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, r.width, r.height);
    traits.forEach(function (t) { dessinerTrait(ctx, t, r.width); });
  }
  function dimensionner() {
    var r = canvas.getBoundingClientRect(); if (!r.width) return;
    dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(r.width * dpr); canvas.height = Math.round(r.height * dpr);
    ctx = canvas.getContext('2d'); redessiner();
  }
  function majEtat() {
    var vide = traits.length === 0;
    document.getElementById('btn-effacer').disabled = vide; document.getElementById('btn-annuler').disabled = vide;
    document.getElementById('invite-signature').style.display = vide ? '' : 'none';
  }

  canvas.addEventListener('pointerdown', function (e) {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    e.preventDefault();
    try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* pointeur synthétique ou déjà relâché : sans importance */ }
    courant = {points: [pt(e, canvas.getBoundingClientRect())]}; traits.push(courant); redessiner(); majEtat();
  });
  canvas.addEventListener('pointermove', function (e) {
    if (!courant) return; e.preventDefault();
    var r = canvas.getBoundingClientRect(), evts = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
    (evts.length ? evts : [e]).forEach(function (ev) { courant.points.push(pt(ev, r)); });
    redessiner();
  });
  function fin(e) { if (courant) { courant = null; redessiner(); } }
  canvas.addEventListener('pointerup', fin); canvas.addEventListener('pointercancel', fin); canvas.addEventListener('pointerleave', function (e) { if (courant && !(canvas.hasPointerCapture && canvas.hasPointerCapture(e.pointerId))) courant = null; });
  canvas.addEventListener('contextmenu', function (e) { e.preventDefault(); });

  document.getElementById('btn-effacer').addEventListener('click', function () { traits = []; courant = null; redessiner(); majEtat(); });
  document.getElementById('btn-annuler').addEventListener('click', function () { traits.pop(); redessiner(); majEtat(); });
  window.addEventListener('resize', dimensionner);

  // Export : PNG transparent de largeur fixe (1000 px) : léger à envoyer et net à l'impression.
  function exporter() {
    var L = 1000, r = canvas.getBoundingClientRect(), H = Math.round(L * r.height / r.width);
    var hors = document.createElement('canvas'); hors.width = L; hors.height = H;
    var c = hors.getContext('2d'); traits.forEach(function (t) { dessinerTrait(c, t, L); });
    return hors.toDataURL('image/png');
  }
  window.SignaturePad = {nbTraits: function () { return traits.length; }, exporter: exporter};

  // Deux modes (dessiner / importer) : on n'envoie le dessin que si l'onglet « Dessiner » est actif et non vide.
  formulaire.addEventListener('submit', function () {
    var modeDessin = document.getElementById('onglet-dessin').classList.contains('active');
    champ.value = modeDessin && traits.length ? exporter() : '';
  });
  document.querySelectorAll('[data-mode-signature]').forEach(function (b) {
    b.addEventListener('click', function () {
      var dessin = b.getAttribute('data-mode-signature') === 'dessin';
      document.getElementById('onglet-dessin').classList.toggle('active', dessin);
      document.getElementById('onglet-import').classList.toggle('active', !dessin);
      document.getElementById('volet-dessin').hidden = !dessin; document.getElementById('volet-import').hidden = dessin;
      if (dessin) dimensionner();
    });
  });
  dimensionner(); majEtat();
})();
