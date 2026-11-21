import re


def test_le_menu_lateral_reste_fixe_sur_ordinateur(app, login):
    css = open("app/static/css/app.css", encoding="utf-8").read()
    bloc = re.search(r"@media \(min-width: 768px\) \{(.*?)\n\}", css, re.S).group(1)
    for regle in ("position: sticky", "top: 0", "height: 100vh", "overflow-y: auto", "align-self: flex-start"):
        assert regle in bloc, regle                       # fixe, pleine hauteur, défile seul si l'écran est petit
    assert "main { min-width: 0; }" in css                # le contenu ne déborde pas sous le menu

    page = login("dir").get("/").data.decode()
    assert 'class="sidebar' in page and 'class="mt-auto pt-3"' in page      # compte et déconnexion en bas du menu
    assert page.index("Déconnexion") > page.index("Utilisateurs")


def test_le_menu_n_existe_pas_sur_la_page_de_connexion(app):
    page = app.test_client().get("/connexion").data.decode()
    assert 'class="sidebar' not in page
