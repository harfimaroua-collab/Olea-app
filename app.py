import math
import json
import os
from datetime import date, timedelta
from pathlib import Path
from io import StringIO
import pandas as pd
import streamlit as st
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from bs4 import BeautifulSoup




import sys
import socket
import subprocess
if "--ouvrir" in sys.argv:
    import time
    import webbrowser
    import urllib.request
    connexion = socket.socket()
    connexion.bind(("127.0.0.1", 0))
    port = str(connexion.getsockname()[1])
    connexion.close()
    print("Ouverture OLEA : http://localhost:" + port, flush=True)
    commande = [sys.executable, "-m", "streamlit", "run", __file__,
        "--server.address", "127.0.0.1", "--server.port", port,
        "--server.headless", "true", "--browser.gatherUsageStats", "false",
        "--theme.base", "light", "--theme.primaryColor", "#47745a"]
    serveur = subprocess.Popen(commande)
    try:
        for attente in range(80):
            if serveur.poll() is not None:
                break
            try:
                with urllib.request.urlopen("http://localhost:"+port+"/_stcore/health", timeout=1):
                    webbrowser.open("http://localhost:"+port)
                    break
            except OSError:
                time.sleep(0.25)
        serveur.wait()
    except KeyboardInterrupt:
        serveur.terminate()
        serveur.wait()
    sys.exit(serveur.returncode or 0)


def pression_vapeur(temperature):
    return 0.6108 * math.exp(17.27 * temperature / (temperature + 237.3))


def eto_fao56(tmin, tmax, hr, vent, rn, altitude):
   
    valeurs = [tmin, tmax, hr, vent, rn, altitude]
    if not all(math.isfinite(float(v)) for v in valeurs):
        raise ValueError("Météo ou altitude manquante.")
    if not (-40 <= tmin <= tmax <= 60 and 0 <= hr <= 100 and 0 <= vent <= 50
            and -10 <= rn <= 50 and -400 <= altitude <= 5000):
        raise ValueError("Météo hors limites : vérifier les unités.")
    temperature = (tmin + tmax) / 2
    es = (pression_vapeur(tmin) + pression_vapeur(tmax)) / 2
    ea = es * hr / 100
    delta = 4098 * pression_vapeur(temperature) / (temperature + 237.3)**2
    pression = 101.3 * ((293 - 0.0065 * altitude) / 293)**5.26
    gamma = 0.000665 * pression
    numerateur = 0.408 * delta * rn + gamma * 900 / (temperature + 273) * vent * (es - ea)
    denominateur = delta + gamma * (1 + 0.34 * vent)
    return max(0.0, numerateur / denominateur)


def bilan_irrigation(mesures, kc, kr, ru, p, efficience, dr_initial, intensite, heures, profil):
   
    valeurs = [kc, kr, ru, p, efficience, dr_initial, intensite]
    if any(v is None for v in valeurs) or not all(math.isfinite(float(v)) for v in valeurs):
        raise ValueError("Consignes hydriques manquantes.")
    if not (0 < kc <= 2 and 0 < kr <= 1 and ru > 0 and 0 < p < 1
            and 0 < efficience <= 1 and 0 <= dr_initial <= ru and intensite > 0):
        raise ValueError("Consignes hydriques incohérentes.")
    if heures is not None and not (math.isfinite(heures) and 0 <= heures <= 24):
        raise ValueError("Heures disponibles : de 0 à 24, ou inconnues.")
    tableau = mesures.copy()
    colonnes = ["date", "eto", "pe", "irrigation_gross"]
    if any(c not in tableau for c in colonnes):
        raise ValueError("CSV requis : date, eto, pe, irrigation_gross.")
    tableau["date"] = pd.to_datetime(tableau["date"], format="%Y-%m-%d")
    tableau = tableau.sort_values("date")
    if tableau.empty or tableau.date.isna().any() or tableau.date.duplicated().any():
        raise ValueError("Série vide, date absente ou dupliquée.")
    if not tableau.date.eq(tableau.date.dt.normalize()).all() or tableau.date.dt.tz is not None:
        raise ValueError("Une journée locale sans heure par ligne est requise.")
    if not tableau.date.diff().dt.total_seconds().dropna().eq(86400).all():
        raise ValueError("Journée manquante : compléter le bilan sans inventer les apports.")
    dr = dr_initial
    rfu = p * ru
    resultats = []
    for _, jour in tableau.iterrows():
        et0, pluie, apport = float(jour.eto), float(jour.pe), float(jour.irrigation_gross)
        if not all(math.isfinite(v) and v >= 0 for v in [et0, pluie, apport]):
            raise ValueError("ET0, pluie efficace ou apport réalisé manquant/négatif.")
        etc = et0 * kc * kr
        dr_calcule = dr + etc - pluie - apport * efficience
        drainage = max(0.0, -dr_calcule)
        stress = dr_calcule > ru
        dr = max(0.0, min(ru, dr_calcule))
        if profil and pd.notna(jour.get("dr_observed_mm")):
            dr = float(jour.dr_observed_mm)
            if not math.isfinite(dr) or not 0 <= dr <= ru:
                raise ValueError("Déplétion du profil calibré hors de RU.")
        nette = dr if dr >= 0.5 * rfu else 0.0
        brute = nette / efficience
        possible = None if heures is None else min(brute, intensite * heures)
        if brute == 0:
            possible = 0.0
        duree = None if possible is None else possible / intensite
        reste = None if possible is None else max(0.0, brute - possible)
        resultats.append(dict(date=jour.date,ET0=et0,ETc=etc,Pluie=pluie,Apport=apport,Dr=dr,
            RFU=rfu,Seuil=0.5*rfu,Nette=nette,Brute=brute,Possible=possible,
            Heures=duree,Reste=reste,Duree_totale=brute/intensite,Drainage=drainage,Stress=stress))
    return pd.DataFrame(resultats)


def analyse_en_mg_kg(valeur, unite, element):
    
    if valeur is None or not math.isfinite(float(valeur)) or valeur < 0:
        raise ValueError("Analyse du sol manquante ou invalide : " + element)
    facteurs = {
        "N": {"mg N/kg": 1.0, "mg NO3/kg": 14.007 / 62.005},
        "P": {"mg P/kg": 1.0, "mg P2O5/kg": 1 / 2.2919},
        "K": {"mg K/kg": 1.0, "mg K2O/kg": 1 / 1.2046,
              "cmol(+)/kg": 390.983, "meq/100 g": 390.983}}
    if unite not in facteurs[element]:
        raise ValueError("Unité incompatible avec l’analyse : " + element)
    return valeur * facteurs[element][unite]


def stock_analytique(concentration, densite, profondeur, cailloux, surface_sol):
    
    valeurs = [concentration, densite, profondeur, cailloux, surface_sol]
    if any(v is None for v in valeurs) or not all(math.isfinite(float(v)) for v in valeurs):
        raise ValueError("Analyse, densité ou volume de sol à compléter.")
    if not (concentration >= 0 and 0.5 <= densite <= 2 and 0 < profondeur <= 2
            and 0 <= cailloux < 100 and 0 < surface_sol <= 100):
        raise ValueError("Densité, profondeur, cailloux ou surface de sol incohérents.")
    return concentration * densite * profondeur * 10 * (1-cailloux/100) * surface_sol/100


def bilan_fertilisation(parametres, realises):
  
    p = parametres
    limites = {"rendement": (0, 50), "taille": (0, 50), "ms_taille": (0, 100),
        "export_taille": (0, 100), "semaines": (1, 52), "densite": (0.5, 2),
        "z_analyse": (0.01, 2), "z_racines": (0.01, 2),
        "cailloux": (0, 99), "surface_sol": (0.01, 100)}
    for cle_parametre, (minimum, maximum) in limites.items():
        valeur = p.get(cle_parametre)
        if valeur is None or not math.isfinite(float(valeur)) or not minimum <= valeur <= maximum:
            raise ValueError("Donnée à compléter / vérifier : " + cle_parametre)
    if not float(p["semaines"]).is_integer():
        raise ValueError("Le nombre de semaines doit être entier.")
    if p["z_analyse"] > p["z_racines"]:
        raise ValueError("Demander une analyse du profil racinaire retenu, sans utiliser une couche plus profonde.")
    lignes = []
    for i, e in enumerate(ELEMENTS):
        nom_element = ["N", "P", "K"][i]
        facteur_oxyde = [1.0, 2.2919, 1.2046][i]
        export = p["exports"][i]
        sol = p["sol"][i]
        mineral = p["mineral"][i]
        champs = [export["Fruit kg/t fraîche"], export["Taille kg/t MS"],
            sol["Part mobilisable (%)"], mineral["Ajustement (kg/ha)"],
            mineral["Efficience (%)"], mineral["Avant registre (kg/ha)"], realises[e]]
        if any(v is None for v in champs) or not all(math.isfinite(float(v)) for v in champs):
            raise ValueError("Coefficients, crédits ou historique à compléter : " + e)
        if not (export["Fruit kg/t fraîche"] >= 0 and export["Taille kg/t MS"] >= 0
                and 0 <= sol["Part mobilisable (%)"] <= 100
                and 0 < mineral["Efficience (%)"] <= 100
                and mineral["Avant registre (kg/ha)"] >= 0 and realises[e] >= 0):
            raise ValueError("Pourcentage ou quantité incohérent : " + e)
        fruits = p["rendement"] * export["Fruit kg/t fraîche"] * facteur_oxyde
        taille = p["taille"] * p["ms_taille"]/100 * p["export_taille"]/100 * export["Taille kg/t MS"] * facteur_oxyde
        concentration = analyse_en_mg_kg(sol["Analyse"], p["unites_sol"][i], nom_element)
        stock_sol = stock_analytique(concentration, p["densite"], p["z_analyse"], p["cailloux"], p["surface_sol"]) * facteur_oxyde
        credit_sol = stock_sol * sol["Part mobilisable (%)"]/100
        credit_eau = 0.0
        if not p["eau_nulle"]:
            eau = p["eau"][i]
            volume, teneur, disponible = p["volume_eau"], eau["Analyse (mg/L)"], eau["Valorisable (%)"]
            if any(v is None for v in [volume, teneur, disponible]) or not all(math.isfinite(float(v)) for v in [volume, teneur, disponible]):
                raise ValueError("Analyse / volume d’eau à compléter : " + e)
            if not (volume >= 0 and teneur >= 0 and 0 <= disponible <= 100):
                raise ValueError("Contribution de l’eau invalide : " + e)
            if e == "N" and p["forme_eau_N"] == "NO3":
                teneur = teneur * 14.007 / 62.005
            credit_eau = teneur * volume / 1000 * disponible/100 * facteur_oxyde
        credit_organique = 0.0
        if not p["organique_nul"]:
            organique = p["organique"][i]
            masse, teneur, disponible = p["masse_organique"], organique["Teneur (%)"], organique["Disponible campagne (%)"]
            if any(v is None for v in [masse, teneur, disponible]) or not all(math.isfinite(float(v)) for v in [masse, teneur, disponible]):
                raise ValueError("Apport organique / composition à compléter : " + e)
            if not (masse >= 0 and 0 <= teneur <= 100 and 0 <= disponible <= 100):
                raise ValueError("Contribution organique invalide : " + e)
            # Composition déjà exprimée en N-P2O5-K2O sur produit tel qu’apporté.
            credit_organique = masse * 1000 * teneur/100 * disponible/100
        demande = max(0.0, fruits + taille + mineral["Ajustement (kg/ha)"])
        deficit = max(0.0, demande - credit_sol - credit_eau - credit_organique)
        programme = deficit / (mineral["Efficience (%)"]/100)
        solde = max(0.0, programme - mineral["Avant registre (kg/ha)"] - realises[e])
        lignes.append({"Élément":e, "Fruits":fruits, "Taille exportée":taille,
            "Stock analytique sol":stock_sol, "Crédit sol":credit_sol,
            "Crédit eau":credit_eau, "Crédit organique":credit_organique,
            "Programme minéral":programme, "Avant registre":mineral["Avant registre (kg/ha)"],
            "Réalisé (kg/ha)":realises[e], "Solde (kg/ha)":solde, "Cette semaine (kg/ha)":solde/p["semaines"]})
    return pd.DataFrame(lignes)


def diagnostic_foliaire(teneur, element, mois):
   
    if teneur is None or not math.isfinite(float(teneur)) or not 0 <= teneur <= 100:
        return "Analyse à compléter"
    if mois != 7:
        return "Interprétation selon le protocole local"
    if element == "N":
        if teneur < 1.4:
            return "Sous le repère de carence"
        if teneur < 1.5:
            return "Proche de la limite basse"
        if teneur <= 2:
            return "Dans la plage de référence"
        return "Au-dessus de la plage : revoir l’apport N"
    if element == "P":
        return "Sous le repère de suffisance" if teneur < 0.1 else "Dans la plage de référence" if teneur <= 0.3 else "Au-dessus de la plage de référence"
    return "Sous le repère de carence" if teneur < 0.4 else "À surveiller" if teneur < 0.8 else "Au-dessus du repère de suffisance"


def parametres_fertilisation(demo, texture):
    
    densite = 1.50 if "SABLEUX" in texture else 1.30 if texture == "ARGILEUX" else 1.35

    exports = []
    for e, fruit, taille in [("N",5.72,4.01),("P",1.44467,0.30463),("K",7.48,2.37)]:
        exports.append({"Élément":e, "Fruit kg/t fraîche":fruit/2.681,
                        "Taille kg/t MS":taille/(2.77*204/1000)})
    sol, eau, organique, mineral, foliaire = [], [], [], [], []
    for i, e in enumerate(ELEMENTS):
        sol.append({"Élément":["N minéral","P extrait","K échangeable"][i],
            "Analyse":[3.0,8.0,60.0][i] if demo else None,
            "Part mobilisable (%)":[50.0,3.0,3.0][i] if demo else None})
        eau.append({"Élément":["N","P","K"][i], "Analyse (mg/L)":[8.0,0.0,0.0][i] if demo else None,
            "Valorisable (%)":80.0 if demo else None})
        organique.append({"Élément":e, "Teneur (%)":[1.0,0.3,0.7][i] if demo else None,
            "Disponible campagne (%)":[20.0,50.0,80.0][i] if demo else None})
        mineral.append({"Élément":e, "Ajustement (kg/ha)":0.0 if demo else None,
            "Efficience (%)":[70.0,50.0,70.0][i] if demo else None,
            "Avant registre (kg/ha)":0.0 if demo else None})
        foliaire.append({"Élément":["N","P","K"][i],"Teneur (%)":[1.45,0.09,0.7][i] if demo else None})
    return {"rendement":12.0 if demo else None, "taille":3.0 if demo else None,
        "ms_taille":50.0 if demo else None, "export_taille":50.0 if demo else None,
        "semaines":10.0 if demo else None, "densite":densite if demo else None,
        "z_analyse":0.6 if demo else None, "z_racines":0.6 if demo else None,
        "cailloux":0.0 if demo else None, "surface_sol":100.0 if demo else None,
        "exports":exports, "sol":sol, "eau":eau, "organique":organique, "mineral":mineral,
        "foliaire":foliaire, "date_foliaire":date(aujourdhui().year if aujourdhui().month >= 7 else aujourdhui().year-1,7,15).isoformat() if demo else None,
        "unites_sol":["mg N/kg","mg P/kg","mg K/kg"], "methode_P":"Olsen",
        "forme_eau_N":"NO3", "volume_eau":3000.0 if demo else None,
        "eau_nulle":False, "masse_organique":2.0 if demo else None, "organique_nul":False,
        "date_analyse":min(date(aujourdhui().year,1,15),aujourdhui()).isoformat() if demo else None,
        "pH_sol":7.5 if demo else None, "source":"Hypothèses de simulation" if demo else "",
        "valide":demo, "surface":None, "campagne":aujourdhui().year}


def editer_nutrition(lignes, cle, minimum=0.0, maximum=None):
    
    colonnes = {}
    tableau = pd.DataFrame(lignes)
    for champ in lignes[0]:
        if champ != "Élément":
            tableau[champ] = pd.to_numeric(tableau[champ],errors="coerce").astype(float)
            colonnes[champ] = st.column_config.NumberColumn(min_value=minimum, max_value=maximum, format="%.3f")
    edition = st.data_editor(tableau, disabled=["Élément"],
        hide_index=True, column_config=colonnes, key=cle)
    return edition.astype(object).where(pd.notna(edition), None).to_dict("records")


def plan_engrais(besoins, stock, surface):
    
    reste = besoins.copy()
    produits = []
    # Les produits de stock positif sont examinés avant les autres.
    for disponible in [True, False]:
        for produit in stock:
            positif = produit["Quantité"] is not None and produit["Quantité"] > 0
            if positif == disponible:
                produits.append(produit)
    lignes = []
    for element in ["P2O5", "K2O", "N"]:
        if reste[element] <= 0.000001:
            continue
        candidat = None
        dose_choisie = 0.0
        for produit in produits:
            if produit["Catégorie"] != "Engrais minéral" or produit["Unité"] != "kg" or not produit["Vérifié"]:
                continue
            if any(produit[e] is None or not 0 <= produit[e] <= 100 for e in ELEMENTS):
                continue
            if produit[element] <= 0:
                continue
            dose = reste[element] / (produit[element] / 100)
            if any(dose * produit[e] / 100 > reste[e] + 0.000001 for e in ELEMENTS):
                continue
            if candidat is None:
                candidat, dose_choisie = produit, dose
            # Un stock suffisant évite un achat si un autre grade est disponible.
            if produit["Quantité"] is not None and produit["Quantité"] >= dose * surface:
                candidat, dose_choisie = produit, dose
                break
        if candidat is not None:
            produit, dose = candidat, dose_choisie
            total = dose * surface
            disponible = produit["Quantité"]
            utilise = None if disponible is None else min(total, disponible)
            achat = None if utilise is None else max(0.0, total - utilise)
            lignes.append({"Produit":produit["Produit"],"kg/ha/semaine":dose,
                "kg du secteur":total,"Stock utilisé (kg)":utilise,"À acheter (kg)":achat})
            for e in ELEMENTS:
                reste[e] = max(0.0, reste[e] - dose * produit[e] / 100)
    return pd.DataFrame(lignes), reste


# 2. LES DONNÉES : catalogue inclus ici, sans fichier CSV supplémentaire.
ELEMENTS = ["N", "P2O5", "K2O"]
STADES = {"Repos végétatif":0.45,"Reprise végétative":0.50,"Inflorescences":0.60,
    "Floraison":0.65,"Nouaison":0.65,"Croissance du fruit":0.70,
    "Sclérification":0.60,"Accumulation d’huile":0.70,"Maturation":0.65}
# Les Kc ci-dessus servent UNIQUEMENT à la simulation.
PARCELLES_CSV = """id,domaine,zone,parcelle,variete,surface_ha,sol,plantation,interligne_m,sur_rang_m,goutteurs_arbre,debit_goutteur_lh
PARC-O23-S1,Ouled Hatten,O23,P1/ O23-S1,Menara,102.0,LIMONEUX ARGILEUX,07/2013,7.0,5.0,5,3.0
PARC-O23-S2,Ouled Hatten,O23,P2/ O23-S2,Haouzia,104.0,LIMONEUX ARGILEUX,07/2013,7.0,5.0,5,3.0
PARC-O23-S3,Ouled Hatten,O23,P3/ O23-S3,Menara,101.0,ARGILEUX,07/2013,7.0,5.0,5,3.0
PARC-OHA-SA1,Ouled Hatten,OHA,P1/ OHA-SA1,Arbosana,23.5,ARGILEUX,10/2008,4.0,1.36,2,1.67
PARC-OHA-SA4,Ouled Hatten,OHA,P1/ OHA-SA4,Arbosana,20.15,LIMONEUX ARGILEUX,10/2008,4.0,1.36,2,1.67
PARC-OHA-SA6,Ouled Hatten,OHA,P1/ OHA-SA6,Arbequine,20.15,LIMONEUX ARGILEUX,10/2008,4.0,1.36,2,1.67
PARC-OHA-SB2,Ouled Hatten,OHA,P2/ OHA-SB2,Arbequine,20.65,ARGILEUX,10/2008,4.0,1.36,2,1.67
PARC-OHA-SB5,Ouled Hatten,OHA,P2/ OHA-SB5,Arbequine,20.65,ARGILEUX LIMONEUX,10/2008,4.0,1.36,2,1.67
PARC-OHA-SC3,Ouled Hatten,OHA,P3/ OHA-SC3,Arbequine,23.2,LIMONEUX ARGILEUX,10/2008,4.0,1.36,2,1.67
PARC-OHA-SC4,Ouled Hatten,OHA,P3/ OHA-SC4,Arbequine,21.0,ARGILEUX LIMONEUX,10/2008,4.0,1.36,2,1.67
PARC-OHA-SD1,Ouled Hatten,OHA,P4/ OHA-SD1,Arbequine,27.4,LIMONEUX ARGILEUX,10/2008,4.0,1.36,2,1.67
PARC-OHA-SD6,Ouled Hatten,OHA,P4/ OHA-SD6,Arbequine,23.5,LIMONEUX ARGILEUX,10/2008,4.0,1.36,2,1.67
PARC-OHE-B2,Ouled Nejaa,OHE,OHE B2,Picual,72.0,LIMONEUX ARGILEUX,01/01/2015,7.0,5.0,10,1.0
PARC-OHE-B1,Ouled Nejaa,OHE,OHE B1,Picual,150.0,LIMONEUX ARGILEUX,01/01/2015,7.0,5.0,10,1.0
PARC-ONE-B2,Ouled Nejaa,ONE,ONE B2,Picual,68.0,LIMONEUX ARGILEUX,01/01/2015,7.0,5.0,10,1.0
PARC-ONE-B3,Ouled Nejaa,ONE,ONE B3,MENARAHOUZIA · libellé du tableau,172.0,SABLEUX LIMONEUX AR… · texte tronqué,01/01/2015,7.0,5.0,10,1.0
PARC-PEN-S1,Pénitencier,PEN,PEN S1,Picual,115.0,LIMONEUX ARGILEUX,01/01/2016,7.0,5.0,10,2.0
PARC-PEN-S2,Pénitencier,PEN,PEN S2,Picual,121.0,ARGILEUX,01/01/2016,7.0,5.0,10,2.0
PARC-PEN-S3,Pénitencier,PEN,PEN S3,Picual,115.0,ARGILEUX LIMONEUSE,01/01/2016,7.0,5.0,10,2.0"""
parcelles = pd.read_csv(StringIO(PARCELLES_CSV), keep_default_na=False)
CATALOGUE = [
    ["MAP Fertika 12-61-0", 12.0, 61.0, 0.0],
    ["SOLUCROS sulfate de potasse 0-0-50", 0.0, 0.0, 50.0],
    ["Nitrate de potasse Fertika 13-0-46", 13.0, 0.0, 46.0],
    ["Urée 46 %", 46.0, 0.0, 0.0]]
CATEGORIES = ["Engrais minéral", "Engrais organique", "Biostimulant / humiques", "Phyto", "Autre"]
INTERVENTIONS = ["Irrigation", "Fertilisation", "Apport organique", "Taille", "Récolte", "Travail du sol", "Désherbage", "Traitement phytosanitaire", "Entretien", "Autre"]


# MÉTÉO EN LIGNE — les fonctions de calcul agronomique existantes sont conservées.
URL_MAROC = "https://www.marocagriculture.com/meteo/prevision/fquih-ben-salah"
LATITUDE, LONGITUDE = 32.5008, -6.6939


def aujourdhui():
    return datetime.now(ZoneInfo("Africa/Casablanca")).date()


def lire_url(url):
    requete = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; OLEA-PFE/1.0)"})
    with urlopen(requete, timeout=15) as reponse:
        return reponse.read().decode("utf-8")


def nombre_meteo(noeud):
    if noeud is None:
        return float("nan")
    trouve = re.search(r"-?\d+(?:[.,]\d+)?", noeud.get_text(" ", strip=True))
    return float(trouve.group().replace(",", ".")) if trouve else float("nan")


def analyser_maroc(html, jour_local):
    """Lire uniquement les valeurs publiées, sans inventer de pluie en mm."""
    page = BeautifulSoup(html, "html.parser")
    debut = None
    for script in page.find_all("script", type="application/ld+json"):
        try:
            description = json.loads(script.string or script.get_text())
            if isinstance(description, dict) and description.get("@type") == "WeatherForecast":
                debut = date.fromisoformat(description["validFrom"][:10])
                break
        except (ValueError, KeyError, TypeError):
            continue
    if debut is None or not -1 <= (jour_local - debut).days <= 1:
        raise ValueError("Date des prévisions Maroc Agriculture absente ou ancienne.")
    lignes = []
    for i, carte in enumerate(page.select(".o_meteo_day_v2")):
        jour = debut + timedelta(days=i)
        etiquette = carte.select_one(".o_meteo_day_date")
        if etiquette is None or etiquette.get_text(strip=True) != jour.strftime("%d/%m"):
            raise ValueError("Dates météo non consécutives : vérifier la page source.")
        tmin = nombre_meteo(carte.select_one(".o_meteo_lo"))
        tmax = nombre_meteo(carte.select_one(".o_meteo_hi"))
        eto = nombre_meteo(carte.select_one('.o_meteo_day_agro [title*="ET"]'))
        probabilite = nombre_meteo(carte.select_one(".o_meteo_day_pop"))
        if not (-40 <= tmin <= tmax <= 60 and math.isfinite(eto) and 0 <= eto <= 30):
            raise ValueError("Température ou ET0 Maroc Agriculture invalide/manquante.")
        if math.isfinite(probabilite) and not 0 <= probabilite <= 100:
            raise ValueError("Probabilité de pluie invalide.")
        lignes.append({"date":pd.Timestamp(jour), "tmin":tmin, "tmax":tmax,
            "eto":eto, "prob_rain":probabilite, "source_eto":"Maroc Agriculture",
            "source_prob":"Maroc Agriculture" if math.isfinite(probabilite) else None})
    if not lignes:
        raise ValueError("Prévisions non trouvées : la structure de la page a peut-être changé.")
    actualisation = page.select_one(".o_meteo_updated")
    return pd.DataFrame(lignes), actualisation.get("data-iso", "non indiquée") if actualisation else "non indiquée"


def analyser_openmeteo(donnees):
    """Les journées passées sont des archives de modèles, pas une station OLEA."""
    if donnees.get("timezone") != "Africa/Casablanca":
        raise ValueError("Fuseau météo inattendu.")
    unites = donnees.get("daily_units", {})
    if any(unites.get(c) != "mm" for c in ["et0_fao_evapotranspiration", "precipitation_sum"]):
        raise ValueError("Unités météo inattendues : mm requis.")
    unites_horaires = donnees.get("hourly_units", {})
    if unites_horaires.get("vapour_pressure_deficit") != "kPa" or unites_horaires.get("relative_humidity_2m") != "%":
        raise ValueError("Unités VPD/humidité inattendues.")
    tableau = pd.DataFrame(donnees["daily"]).rename(columns={"time":"date",
        "temperature_2m_min":"tmin", "temperature_2m_max":"tmax",
        "et0_fao_evapotranspiration":"eto", "precipitation_sum":"rain_mm",
        "precipitation_probability_max":"prob_rain"})
    tableau["date"] = pd.to_datetime(tableau["date"], format="%Y-%m-%d")
    if tableau.empty or tableau.date.isna().any() or tableau.date.duplicated().any():
        raise ValueError("Dates Open-Meteo invalides.")
    for champ in ["tmin", "tmax", "eto", "rain_mm", "prob_rain"]:
        tableau[champ] = pd.to_numeric(tableau[champ], errors="coerce")
        invalide = tableau[champ].notna() & ~tableau[champ].map(math.isfinite)
        if champ in ["eto", "rain_mm"]:
            invalide |= tableau[champ] < 0
        if champ == "prob_rain":
            invalide |= (tableau[champ] < 0) | (tableau[champ] > 100)
        if champ in ["tmin", "tmax"]:
            invalide |= (tableau[champ] < -40) | (tableau[champ] > 60)
        if invalide.any():
            raise ValueError("Valeur météo Open-Meteo invalide : " + champ)
    if (tableau.tmin > tableau.tmax).any():
        raise ValueError("Températures Open-Meteo incohérentes.")
    heures = pd.DataFrame(donnees["hourly"])
    heures["date"] = pd.to_datetime(heures["time"]).dt.normalize()
    champs = {"relative_humidity_2m":"rh", "vapour_pressure_deficit":"VPD", "wind_speed_10m":"vent10"}
    for champ in champs:
        heures[champ] = pd.to_numeric(heures[champ], errors="coerce")
        valeurs = heures[champ].dropna()
        if not valeurs.map(math.isfinite).all() or (valeurs < 0).any():
            raise ValueError("Indicateur horaire invalide : " + champ)
        if champ == "relative_humidity_2m" and (valeurs > 100).any():
            raise ValueError("Humidité relative hors de 0 à 100 %.")
    moyennes = heures.groupby("date")[list(champs)].mean().rename(columns=champs)
    tableau = tableau.merge(moyennes, on="date", how="left", validate="one_to_one")
    tableau["source_eto"] = "Open-Meteo"
    tableau["source_pluie"] = "Open-Meteo"
    tableau["source_prob"] = "Open-Meteo"
    return tableau.sort_values("date").reset_index(drop=True)


@st.cache_data(ttl=3600, show_spinner=False)
def charger_maroc(jour_iso):
    tableau, actualisation = analyser_maroc(lire_url(URL_MAROC), date.fromisoformat(jour_iso))
    return tableau, actualisation, datetime.now(timezone.utc).isoformat(timespec="seconds")


@st.cache_data(ttl=3600, show_spinner=False)
def charger_openmeteo(jour_iso):
    parametres = {"latitude":LATITUDE, "longitude":LONGITUDE,
        "daily":"temperature_2m_min,temperature_2m_max,et0_fao_evapotranspiration,precipitation_sum,precipitation_probability_max",
        "hourly":"temperature_2m,relative_humidity_2m,vapour_pressure_deficit,wind_speed_10m",
        "timezone":"Africa/Casablanca", "past_days":21, "forecast_days":16, "wind_speed_unit":"kmh"}
    url = "https://api.open-meteo.com/v1/forecast?" + urlencode(parametres)
    tableau = analyser_openmeteo(json.loads(lire_url(url)))
    return tableau, datetime.now(timezone.utc).isoformat(timespec="seconds")


def assembler_meteo(maroc, modele, jour_local):
    """Priorité au site demandé pour ET0/T ; pluie en mm issue d'Open-Meteo."""
    historique = modele[modele.date.dt.date < jour_local].copy() if not modele.empty else pd.DataFrame()
    futur = modele[modele.date.dt.date >= jour_local].copy() if not modele.empty else pd.DataFrame(columns=["date"])
    if not maroc.empty:
        futur = futur.set_index("date").combine_first(maroc.set_index("date"))
        for champ in ["tmin", "tmax", "eto", "prob_rain", "source_eto", "source_prob"]:
            futur.loc[maroc.date, champ] = maroc.set_index("date")[champ].combine_first(futur[champ])
        futur = futur.reset_index()
    if not futur.empty:
        futur = futur[futur.date.dt.date >= jour_local].sort_values("date").head(15).reset_index(drop=True)
        for champ in ["rain_mm", "prob_rain", "rh", "VPD", "vent10", "source_pluie"]:
            if champ not in futur:
                futur[champ] = float("nan") if champ != "source_pluie" else "Non disponible"
        futur["source_pluie"] = futur["source_pluie"].fillna("Non disponible")
        futur["source_prob"] = futur["source_prob"].fillna("Non disponible")
    return historique, futur


def completer_meteo_csv(tableau, historique):
    """Compléter seulement la météo. Jamais les irrigations, le MDS ou le VWC."""
    tableau = tableau.copy()
    tableau["date"] = pd.to_datetime(tableau["date"], format="%Y-%m-%d")
    if historique.empty:
        return tableau
    modele = historique.set_index("date")
    for champ in ["eto", "tmin", "tmax", "rh"]:
        if champ not in tableau:
            tableau[champ] = float("nan")
        remplacement = tableau.date.map(modele[champ])
        if champ == "eto":
            # Une station complète conserve la priorité pour calculer sa propre ET0.
            station_complete = pd.Series(False, index=tableau.index)
            if all(c in tableau for c in ["tmin", "tmax", "rh", "wind", "rn"]):
                station_complete = tableau[["tmin", "tmax", "rh", "wind", "rn"]].notna().all(axis=1)
            remplacement = remplacement.mask(station_complete)
        tableau[champ] = tableau[champ].fillna(remplacement)
    return tableau


def prevoir_irrigation(meteo, kc, kr, ru, p, efficience, dr_reference, intensite, heures,
                       pluie_deduite=False, coefficient_pluie=None,
                       apport_deja_recu=0.0, pluie_deja_recue=0.0):
    """Plan conditionnel. Les apports proposés ne sont jamais enregistrés comme réalisés."""
    valeurs = [kc, kr, ru, p, efficience, dr_reference, intensite]
    if any(v is None for v in valeurs) or not all(math.isfinite(float(v)) for v in valeurs):
        raise ValueError("Compléter Kc, Kr, RU, p, efficience, Dr et le réseau.")
    if not (0 < kc <= 2 and 0 < kr <= 1 and ru > 0 and 0 < p < 1
            and 0 < efficience <= 1 and 0 <= dr_reference <= ru and intensite > 0):
        raise ValueError("Paramètres du plan hydrique incohérents.")
    if heures is not None and not (math.isfinite(heures) and 0 <= heures <= 24):
        raise ValueError("Disponibilité du réseau invalide.")
    if any(v is None or not math.isfinite(float(v)) or v < 0 for v in [apport_deja_recu, pluie_deja_recue]):
        raise ValueError("Renseigner les apports déjà reçus aujourd’hui, y compris un zéro confirmé.")
    if pluie_deduite and (coefficient_pluie is None or not math.isfinite(coefficient_pluie) or not 0 <= coefficient_pluie <= 1):
        raise ValueError("Confirmer un coefficient de pluie efficace entre 0 et 1.")
    tableau = meteo.sort_values("date")
    if tableau.empty or tableau.date.duplicated().any() or not tableau.date.diff().dt.days.dropna().eq(1).all():
        raise ValueError("Prévisions vides ou journées manquantes.")
    dr = dr_reference
    lignes = []
    for _, jour in tableau.iterrows():
        eto = float(jour.eto)
        if not math.isfinite(eto) or eto < 0:
            raise ValueError("ET0 prévue absente ou invalide.")
        pluie = 0.0  # hypothèse explicite « sans crédit de pluie », pas une observation
        if pluie_deduite:
            quantite = float(jour.get("rain_mm", float("nan")))
            if not math.isfinite(quantite) or quantite < 0:
                raise ValueError("Pluie prévue en mm indisponible : aucun crédit possible.")
            pluie = quantite * coefficient_pluie
        apport_recu = apport_deja_recu if not lignes else 0.0
        pluie_observee = pluie_deja_recue if not lignes else 0.0
        # La prévision journalière couvre toute la journée : ne pas ajouter deux fois la pluie observée.
        pluie = max(pluie, pluie_observee)
        etc = eto * kc * kr
        dr_potentiel = dr + etc - pluie - apport_recu * efficience
        dr_avant = max(0.0, min(ru, dr_potentiel))
        nette = dr_avant if dr_avant >= 0.5 * p * ru else 0.0
        brute = nette / efficience
        possible = min(brute, intensite * heures) if heures is not None else float("nan")
        if brute == 0:
            possible = 0.0
        duree = possible / intensite
        # Cette mise à jour concerne le plan uniquement, jamais le registre réel.
        dr = max(0.0, dr_avant - possible * efficience) if math.isfinite(possible) else dr_avant
        lignes.append({"date":jour.date, "ET0":eto, "ETc":etc, "Pluie":pluie,
            "Apport_deja_recu":apport_recu, "Pluie_deja_recue":pluie_observee,
            "Dr":dr_avant, "Dr_apres_apport":dr, "RFU":p*ru, "Seuil":0.5*p*ru,
            "Nette":nette, "Brute":brute, "Possible":possible, "Heures":duree,
            "Reste":max(0.0, brute-possible) if math.isfinite(possible) else float("nan"),
            "Duree_totale":brute/intensite, "Stress":dr_potentiel > ru})
    return pd.DataFrame(lignes)




FICHIER = Path(__file__).with_name("suivi.json")


def enregistrer(base):
    texte = json.dumps(base, ensure_ascii=False, indent=2, allow_nan=False)
    temporaire = FICHIER.with_suffix(".tmp")
    temporaire.write_text(texte, encoding="utf-8")
    temporaire.replace(FICHIER)


def realiser_travail(registre, numero, quantite, jour):
    travail = registre["travaux"][numero]
    if travail["État"] != "Prévu" or jour > aujourdhui().isoformat():
        raise ValueError("Tâche déjà clôturée ou date future.")
    apports = dict.fromkeys(ELEMENTS, 0.0)
    if travail["Produit"]:
        produit = None
        for ligne in registre["stock"]:
            if ligne["Produit"] == travail["Produit"]:
                produit = ligne
                break
        if produit is None:
            raise ValueError("Produit absent du stock.")
        if quantite is None or not math.isfinite(quantite) or quantite <= 0 or produit["Quantité"] is None or quantite > produit["Quantité"]:
            raise ValueError("Consommation réelle manquante ou stock insuffisant/inconnu.")
        if travail["Type"] == "Fertilisation":
            if produit["Catégorie"] != "Engrais minéral" or produit["Unité"] != "kg" or not produit["Vérifié"] or any(produit[e] is None for e in ELEMENTS):
                raise ValueError("Engrais minéral en kg, composition vérifiée requis.")
            for e in ELEMENTS:
                apports[e] = quantite * produit[e] / 100 / travail["Surface"]
        produit["Quantité"] = max(0.0, produit["Quantité"] - quantite)
    elif travail["Type"] == "Fertilisation":
        raise ValueError("Associer un produit à une fertilisation pour la comptabiliser.")
    travail.update({"État":"Réalisé","Réalisé le":jour,"Consommé":quantite,"Apports":apports})



def saisie(titre, valeur, cle, minimum=0.0, maximum=5000.0):
    return st.number_input(titre, value=valeur, min_value=float(minimum), max_value=float(maximum), key=cle)




def interpreter_mds(mds, reference):
    if mds is None or pd.isna(mds) or not math.isfinite(float(mds)) or mds < 0:
        return "Mesure à compléter.", None
    if reference is None or pd.isna(reference) or not math.isfinite(float(reference)) or reference <= 0:
        return "Amplitude mesurée ; référence locale nécessaire pour comparer.", None
    rapport = mds / reference
    if rapport > 1:
        texte = "Amplitude au-dessus de la référence ; confronter au climat et au bilan du sol."
    else:
        texte = "Amplitude proche ou sous la référence ; cela ne garantit pas l’absence de stress."
    return texte, rapport


def afficher(valeur):
    if valeur is None or pd.isna(valeur):
        return "—"
    return f"{valeur:.2f}".replace(".", ",")


def courbe(tableau, variables, titre, unite):
    presentes = []
    for variable in variables:
        if variable in tableau and tableau[variable].notna().any():
            presentes.append(variable)
    if not presentes:
        return
    with st.container(border=True):
        st.markdown("**" + titre + "**")
        couleurs = ["#47745a", "#c89845", "#839aab"][:len(presentes)]
        noms = {"theta20":"20 cm","theta40":"40 cm","mds_mm":"MDS","mds_ref_mm":"Référence",
            "tmin":"Minimale","tmax":"Maximale","rh":"HR","Dr":"Épuisement"}
        graphique = tableau.set_index("date")[presentes].rename(columns=noms)
        st.line_chart(graphique,height=220,y_label=unite,x_label="",color=couleurs)

#
st.set_page_config(page_title="OLEA · Pilotage de l’olivier", page_icon="🫒", layout="wide")
st.markdown("""<style>
.stApp{background:#f7faf6} h1,h2,h3{color:#214c35}
[data-testid="stMetricValue"]{color:#214c35;font-size:1.8rem}
.block-container{padding-top:2rem;max-width:1450px}
</style>""", unsafe_allow_html=True)
st.title("OLEA App")
st.caption("Climat, plante et sol · Un bilan explicable pour décider")
lecture_seule = os.environ.get("OLEA_LECTURE_SEULE") == "1"
try:
    lecture_seule = lecture_seule or st.secrets.get("lecture_seule", False)
except FileNotFoundError:
    pass
base = {}
if FICHIER.exists():
    try:
        base = json.loads(FICHIER.read_text(encoding="utf-8"))
        if not isinstance(base, dict):
            raise ValueError("Format de sauvegarde inconnu.")
        for ancien in base.values():
            if not isinstance(ancien, dict) or not isinstance(ancien.get("stock"), list) or not isinstance(ancien.get("travaux"), list):
                raise ValueError("Sauvegarde d’une autre version.")
    except (ValueError, OSError):
        st.error("Sauvegarde illisible : conserver suivi.json et vérifier son contenu.")
        st.stop()

with st.sidebar:
    st.header("Mon secteur")
    mode = st.radio("Origine des données", ["Météo en ligne", "Mes données"], key="mode")
    demo = False  # aucune analyse, aucun stock et aucun capteur générés
    st.caption("Point météo : Fquih Ben Salah, 32,5008° N / −6,6939°. À confirmer pour le secteur.")
    actualiser = st.button("Actualiser la météo")
    domaine = st.selectbox("Domaine", parcelles.domaine.unique())
    choix = parcelles[parcelles.domaine == domaine]
    nom = st.selectbox("Parcelle", choix.parcelle.tolist())
    parcelle = choix[choix.parcelle == nom].iloc[0]
    stade = st.selectbox("Stade observé", list(STADES), index=7, key=mode+":"+parcelle.id+":stade")
    configuration = st.expander("Données et consignes", expanded=False)

# La météo publique est seule mise en cache. Les données de la parcelle restent séparées.
maroc, modele = pd.DataFrame(), pd.DataFrame()
informations_meteo = []
if actualiser:
    charger_maroc.clear()
    charger_openmeteo.clear()
with st.spinner("Lecture de la météo en ligne…"):
    try:
        maroc, date_site, date_lecture = charger_maroc(aujourdhui().isoformat())
        informations_meteo.append("Maroc Agriculture : ET0 et températures prévues ; actualisation indiquée " + date_site)
    except Exception as erreur:
        st.warning("Maroc Agriculture indisponible ou page modifiée : " + str(erreur))
    try:
        modele, date_lecture_api = charger_openmeteo(aujourdhui().isoformat())
        informations_meteo.append("Open-Meteo : pluie prévue en mm, humidité, VPD et archives de modèles. Consultation " + date_lecture_api)
    except Exception as erreur:
        st.warning("Complément Open-Meteo indisponible : " + str(erreur))
meteo_historique, previsions_meteo = assembler_meteo(maroc, modele, aujourdhui())
st.caption("Météo en ligne — prévisions et archives modélisées ; données distinctes des mesures de la station OLEA.")
problemes = []
with configuration:
    secteur = st.text_input("Identifiant du secteur", "secteur-1", key=mode+":"+parcelle.id+":secteur")
    cle = mode + ":" + parcelle.id + ":" + secteur + ":"
    surface_enregistree = base.get(mode, {}).get("nutrition", {}).get(cle, {}).get("surface")
    surface = saisie("Surface suivie (ha)", 1.0 if demo else surface_enregistree, cle+"surface", 0.001, parcelle.surface_ha)
    kc = saisie("Kc du stade", STADES[stade] if demo else None, cle+"kc:"+stade, 0.01, 2.0)
    kr = saisie("Kr du couvert", 1.0 if demo else None, cle+"kr", 0.01, 1.0)
    ruf = saisie("RU du profil (mm)", 120.0 if demo else None, cle+"ru", 1.0, 1000.0)
    p = saisie("Fraction p : RFU = p × RU", 0.5 if demo else None, cle+"p", 0.01, 0.99)
    eff = saisie("Efficience (0–1)", 0.9 if demo else None, cle+"eff", 0.01, 1.0)
    titre_dr = "Déficit constaté au soir d’hier (mm)" if mode == "Météo en ligne" else "Déficit avant la première journée du CSV (mm)"
    dr0 = saisie(titre_dr, None, cle+"dr0", 0.0, 1000.0)
    interligne = saisie("Interligne (m)", float(parcelle.interligne_m), cle+"rang", 0.1, 30.0)
    espacement = saisie("Espacement sur rang (m)", float(parcelle.sur_rang_m), cle+"arbre", 0.1, 30.0)
    goutteurs = saisie("Total de goutteurs/arbre", float(parcelle.goutteurs_arbre), cle+"goutteurs", 1.0, 100.0)
    debit = saisie("Débit par goutteur (L/h)", float(parcelle.debit_goutteur_lh), cle+"debit", 0.01, 200.0)
    altitude = saisie("Altitude (m) si ET0 à calculer", 450.0 if demo else None, cle+"altitude", -400.0, 5000.0)
    heures = saisie("Heures disponibles pour cet apport", 6.0 if demo else None, cle+"heures", 0.0, 24.0)
    confirme = st.checkbox("Déficit, consignes, réseau et localisation météo confirmés", value=False, key=cle+"confirme")
    horizon = st.selectbox("Plan prévisionnel (jours)", [1, 2, 3, 4, 5, 6, 7], index=2, key=cle+"horizon")
    irrigation_aujourdhui = saisie("Irrigation brute déjà reçue aujourd’hui (mm)", None, cle+"irrigation_aujourdhui", 0.0, 1000.0)
    pluie_aujourdhui = saisie("Pluie efficace déjà reçue aujourd’hui (mm)", None, cle+"pluie_aujourdhui", 0.0, 1000.0)
    st.caption("Saisir 0 si l’absence d’apport est confirmée. Ces apports sont déduits avant de proposer un complément pour aujourd’hui.")
    deduire_pluie = st.checkbox("Déduire la pluie prévue dans le plan", key=cle+"pluie_prevue")
    coefficient_pluie = saisie("Part de pluie prévue retenue efficace (0–1)", None, cle+"coefficient_pluie", 0.0, 1.0)
    st.caption("Le crédit de pluie est conditionnel : la pluie prévue n’est pas une pluie reçue. Le coefficient est à justifier selon le sol et les pertes ; aucun pourcentage n’est imposé.")
    profil = st.checkbox("dr_observed_mm : profil calibré en FIN de journée", key=cle+"profil") if mode == "Mes données" else False
    reference_mds = st.checkbox("Référence MDS locale et climat comparable validés", value=demo, key=cle+"reference_mds")
    fichier = st.file_uploader("CSV journalier de ce secteur", type=["csv"], key=cle+"csv") if mode == "Mes données" else None
    st.caption("En ligne : Dr est une estimation du profil au soir d’hier, à fournir. Avec CSV : Dr précède la première ligne ; pe et irrigation_gross sont des apports réellement reçus, même si leur valeur est zéro.")
    st.caption("Kc de simulation illustratifs. Si Kc inclut déjà le couvert, utiliser Kr = 1. RU à confirmer, pas déduite de la texture.")
    st.caption("MDS en mm, VWC en %, VPD en kPa. Pe est une pluie EFFICACE, l’irrigation un apport effectivement reçu.")
    modele = "date,plot_id,sector,eto,pe,irrigation_gross,tmin,tmax,rh,theta20,theta40,mds_mm,mds_ref_mm\n"
    st.download_button("Modèle CSV", modele, "modele.csv", "text/csv")
    for titre, valeur in [("Surface",surface),("Kc",kc),("Kr",kr),("RU",ruf),("p",p),("Efficience",eff),("Épuisement initial",dr0),("Disponibilité du réseau",heures)]:
        if valeur is None:
            problemes.append(titre + " à renseigner.")
    if not secteur.strip():
        problemes.append("Identifiant du secteur à renseigner.")

if mode not in base:
    stock_initial = []
    for produit, n, phosphore, potassium in CATALOGUE:
        stock_initial.append({"Produit":produit,"Catégorie":"Engrais minéral","Unité":"kg",
            "Quantité":25.0 if demo else None,"N":n,"P2O5":phosphore,"K2O":potassium,"Vérifié":demo})
    base[mode] = {"stock":stock_initial,"travaux":[]}
registre = base[mode]

# Lire les données avant de calculer. Les mesures absentes restent absentes.
mesures, eau = pd.DataFrame(), None
try:
    intensite = goutteurs * debit / (interligne * espacement)
    if not float(goutteurs).is_integer():
        raise ValueError("Nombre entier de goutteurs requis.")
    if mode == "Météo en ligne":
        mesures = meteo_historique.copy()
        if mesures.empty:
            raise ValueError("Historique météo indisponible ; les prévisions restent consultables plus bas.")
    elif fichier is not None:
        mesures = pd.read_csv(fichier, sep=None, engine="python")
        for colonne, attendu in [("plot_id",parcelle.id),("sector",secteur)]:
            if colonne not in mesures or not mesures[colonne].eq(attendu).all():
                raise ValueError("CSV : " + colonne + " différent du secteur choisi.")
        mesures = completer_meteo_csv(mesures, meteo_historique)
    else:
        raise ValueError("Aucun fichier de mesures reçu.")
    mesures["date"] = pd.to_datetime(mesures["date"], format="%Y-%m-%d")
    if mesures.empty or mesures.date.isna().any() or mesures.date.duplicated().any():
        raise ValueError("Dates absentes ou dupliquées.")
    mesures = mesures.sort_values("date")
    if mode == "Mes données" and mesures.date.max().date() >= aujourdhui():
        raise ValueError("Utiliser des journées locales terminées, avant aujourd’hui.")
    if not mesures.date.eq(mesures.date.dt.normalize()).all() or mesures.date.dt.tz is not None:
        raise ValueError("Une date locale sans heure par ligne est requise.")
except (ValueError, TypeError, KeyError, ZeroDivisionError) as erreur:
    mesures = pd.DataFrame()
    problemes.append(str(erreur))

if not mesures.empty:
    try:
        if "eto" not in mesures:
            mesures["eto"] = None
        et0_calculees = []
        for _, jour in mesures.iterrows():
            if pd.notna(jour.eto):
                et0_calculees.append(float(jour.eto))
            else:
                et0_calculees.append(eto_fao56(jour.tmin,jour.tmax,jour.rh,jour.wind,jour.rn,altitude))
        mesures["eto"] = et0_calculees
        if mode == "Mes données":
            if not confirme or not secteur.strip() or surface is None:
                raise ValueError("Surface, consignes et réseau local à confirmer.")
            eau = bilan_irrigation(mesures,kc,kr,ruf,p,eff,dr0,intensite,heures,profil)
    except (ValueError, TypeError, KeyError, AttributeError) as erreur:
        problemes.append(str(erreur))
    for variable in ["tmin","tmax","rh","theta20","theta40","mds_mm","mds_ref_mm","pe","irrigation_gross"]:
        if variable in mesures:
            valeurs = pd.to_numeric(mesures[variable], errors="coerce")
            invalide = valeurs.notna() & ~valeurs.map(math.isfinite)
            if variable in ["rh","theta20","theta40"]:
                invalide = invalide | (valeurs < 0) | (valeurs > 100)
            elif variable in ["tmin","tmax"]:
                invalide = invalide | (valeurs < -40) | (valeurs > 60)
            else:
                invalide = invalide | (valeurs < 0)
            if variable == "mds_ref_mm":
                invalide = invalide | (valeurs <= 0)
            if invalide.any() or (mesures[variable].notna() & valeurs.isna()).any():
                problemes.append(variable + " : valeurs invalides écartées du graphique.")
            mesures[variable] = valeurs.mask(invalide)
    if "VPD" not in mesures and all(c in mesures for c in ["tmin","tmax","rh"]):
        vpd = []
        for _, jour in mesures.iterrows():
            if pd.notna(jour.tmin) and pd.notna(jour.tmax) and pd.notna(jour.rh) and jour.tmax >= jour.tmin:
                vpd.append(pression_vapeur((jour.tmin+jour.tmax)/2)*(1-jour.rh/100))
            else:
                vpd.append(float("nan"))
        mesures["VPD"] = vpd
    if "mds_mm" not in mesures or mesures.mds_mm.isna().all():
        problemes.append("MDS du dendromètre non reçu.")
    if "mds_ref_mm" not in mesures or not reference_mds:
        problemes.append("Référence MDS locale à fournir et à valider.")
    if mesures.date.max().date() < aujourdhui()-timedelta(days=1):
        problemes.append("Historique ancien : actualiser avant une décision d’application.")

# Le plan part d’un déficit récent ; les prévisions ne réécrivent jamais le passé.
plan_eau, plan_sans_pluie = pd.DataFrame(), pd.DataFrame()
erreur_prevision = ""
try:
    if previsions_meteo.empty or previsions_meteo.iloc[0].date.date() != aujourdhui():
        raise ValueError("Prévisions d’aujourd’hui indisponibles.")
    if not confirme or surface is None or not secteur.strip():
        raise ValueError("Compléter et confirmer le secteur et les consignes.")
    if mode == "Météo en ligne":
        deficit_reference = dr0
    elif eau is not None and eau.iloc[-1].date.date() == aujourdhui()-timedelta(days=1):
        deficit_reference = float(eau.iloc[-1].Dr)
    else:
        raise ValueError("CSV : le bilan doit être complet jusqu’à hier pour proposer l’irrigation d’aujourd’hui.")
    jours_a_prevoir = previsions_meteo.head(horizon)
    plan_sans_pluie = prevoir_irrigation(jours_a_prevoir,kc,kr,ruf,p,eff,deficit_reference,intensite,heures,
        apport_deja_recu=irrigation_aujourdhui,pluie_deja_recue=pluie_aujourdhui)
    plan_eau = prevoir_irrigation(jours_a_prevoir,kc,kr,ruf,p,eff,deficit_reference,intensite,heures,
        pluie_deduite=deduire_pluie,coefficient_pluie=coefficient_pluie,
        apport_deja_recu=irrigation_aujourdhui,pluie_deja_recue=pluie_aujourdhui)
except (ValueError, TypeError, KeyError) as erreur:
    erreur_prevision = str(erreur)

with configuration:
    for information in informations_meteo:
        st.caption(information)
    if erreur_prevision:
        problemes.append(erreur_prevision)
    if mode == "Mes données":
        st.caption("Les seules colonnes météo manquantes du CSV peuvent être complétées avec les archives Open-Meteo. Les pluies efficaces, irrigations, MDS et VWC manquants ne sont jamais inventés.")
    if problemes:
        st.markdown("**Données à compléter / vérifier**")
        for probleme in dict.fromkeys(problemes):
            st.caption("• " + probleme)
    st.download_button("Sauvegarder stock et travaux",json.dumps(base,ensure_ascii=False,indent=2),"suivi.json","application/json")
    if st.checkbox("Afficher les parcelles du catalogue", key="catalogue_visible"):
        st.dataframe(parcelles, hide_index=True)


st.subheader(nom + " · " + stade)
periode = "Aucune mesure de parcelle reçue" if mesures.empty else "Dernière journée passée : " + mesures.date.max().strftime("%d/%m/%Y")
st.caption(mode + " · " + domaine + " · " + secteur + " · " + periode)
jour = None if mesures.empty else mesures.iloc[-1]
resultat_prevu = not plan_eau.empty
dernier = plan_eau.iloc[0] if resultat_prevu else (None if eau is None else eau.iloc[-1])
mds = None if jour is None else jour.get("mds_mm")
ref = None if jour is None or not reference_mds else jour.get("mds_ref_mm")
lecture_mds, rapport = interpreter_mds(mds, ref)
valeurs = [None if dernier is None else dernier.ETc,
           None if jour is None else jour.get("VPD"),mds,
           None if dernier is None else ruf-dernier.Dr]
for colonne, titre, valeur, unite in zip(st.columns(4),["ETc prévue aujourd’hui" if resultat_prevu else "ETc potentielle","VPD météo","MDS dendromètre","Stock projeté avant apport" if resultat_prevu else "Stock utile calculé"],valeurs,["mm/j","kPa","mm","mm"]):
    with colonne.container(border=True):
        st.metric(titre, afficher(valeur) + " " + unite)
        if titre == "VPD météo" and jour is not None:
            st.caption("Dernière journée passée : " + jour.date.strftime("%d/%m/%Y"))
        if titre == "MDS dendromètre" and rapport is not None:
            st.caption(afficher(rapport) + " × référence locale")

with st.container(border=True):
    st.markdown("### Irrigation prévisionnelle d’aujourd’hui" if resultat_prevu else "### Irrigation — bilan disponible")
    if resultat_prevu:
        st.caption("Prévision du " + dernier.date.strftime("%d/%m/%Y") + (" · crédit de pluie conditionnel activé" if deduire_pluie else " · sans crédit de pluie prévue"))
    elif eau is not None:
        st.caption("Résultat historique daté du " + eau.iloc[-1].date.strftime("%d/%m/%Y") + " ; consulter les données à compléter avant une application aujourd’hui.")
    if dernier is None:
        st.info("Bilan en attente · ouvrir « Données et consignes » pour compléter les entrées.")
    elif dernier.Brute == 0:
        st.success("Surveillance · le seuil d’épuisement n’est pas atteint.")
    elif pd.isna(dernier.Possible):
        st.warning("Irrigation nécessaire · disponibilité du réseau à renseigner.")
    elif dernier.Reste > 0.000001:
        st.warning("Apport partiel · la réserve justifie une recharge, que le réseau ne peut pas couvrir en une seule disponibilité.")
    else:
        st.warning("Recharge proposée · confirmer les observations et le programme d’application.")
    proposition = [None if dernier is None else dernier.Possible,
                   None if dernier is None else dernier.Heures,
                   None if dernier is None or surface is None or pd.isna(dernier.Possible) else dernier.Possible*10*surface,
                   None if dernier is None else dernier.Reste]
    for colonne, titre, valeur, unite in zip(st.columns(4),["Dose brute réalisable","Durée à programmer","Volume du secteur","Besoin restant"],proposition,["mm","h","m³","mm"]):
        colonne.metric(titre, afficher(valeur) + " " + unite)
    if dernier is not None:
        st.caption("Dr : " + afficher(dernier.Dr) + " mm · seuil : " + afficher(dernier.Seuil) + " mm · besoin brut total : " + afficher(dernier.Brute) + " mm · durée totale théorique : " + afficher(dernier.Duree_totale) + " h.")
        if (resultat_prevu and plan_eau.Stress.any()) or (eau is not None and eau.Stress.any()):
            st.caption("Demande potentielle au-delà de RU : le stress Ks n’est pas calculé ; contrôler le profil.")
        if resultat_prevu and deduire_pluie:
            st.caption("Pluie efficace prévue déduite aujourd’hui : " + afficher(dernier.Pluie) + " mm — à vérifier après l’événement.")
    st.caption("Dendromètre · " + lecture_mds)


graphes = st.tabs(["Bilan hydrique", "Climat & plante", "Capteurs du sol", "Prévisions météo"])
with graphes[0]:
    if eau is not None:
        gauche, droite = st.columns(2)
        with gauche:
            courbe(eau,["Dr","Seuil","RFU"],"Épuisement du profil et seuils","mm")
        with droite:
            courbe(eau,["ET0","ETc"],"Demande climatique et culturale","mm/j")
        st.caption("Dose nette théorique = Dr au déclenchement ; moitié de RFU comme stratégie à valider.")
        st.download_button("Exporter le bilan hydrique",eau.to_csv(index=False),"irrigation.csv","text/csv")
    elif not plan_eau.empty:
        courbe(plan_eau,["Dr","Seuil","RFU"],"Déficit projeté avant chaque apport proposé","mm")
        st.caption("Projection conditionnelle ; aucun historique d’irrigation n’est créé à partir de la météo seule.")
with graphes[1]:
    gauche, droite = st.columns(2)
    with gauche:
        courbe(mesures,["VPD"],"VPD journalier approché","kPa")
        courbe(mesures,["tmin","tmax"],"Températures minimale et maximale","°C")
    with droite:
        variables_mds = ["mds_mm","mds_ref_mm"] if reference_mds else ["mds_mm"]
        courbe(mesures,variables_mds,"MDS mesuré et référence locale","mm")
        courbe(mesures,["rh"],"Humidité relative","%")
with graphes[2]:
    courbe(mesures,["theta20","theta40"],"Humidité volumique du sol — sondes 20 / 40 cm","% VWC")
    st.caption("Les sondes appuient la lecture du bilan. Un profil calibré peut recaler Dr ; les pourcentages VWC ne se soustraient pas directement à ETc.")


with graphes[3]:
    st.markdown("**Prévisions pour Fquih Ben Salah**")
    st.markdown("[Consulter Maroc Agriculture](" + URL_MAROC + ") · [Documentation Open-Meteo](https://open-meteo.com/en/docs)")
    if previsions_meteo.empty:
        st.info("Météo indisponible : réessayer avec Actualiser la météo. Aucune valeur aléatoire ne la remplace.")
    else:
        colonnes = ["date","tmin","tmax","eto","rain_mm","prob_rain","source_eto","source_pluie","source_prob"]
        noms = {"tmin":"Tmin °C","tmax":"Tmax °C","eto":"ET0 mm/j","rain_mm":"Pluie prévue mm",
            "prob_rain":"Probabilité pluie %","source_eto":"Source ET0","source_pluie":"Source pluie","source_prob":"Source probabilité"}
        st.dataframe(previsions_meteo[colonnes].rename(columns=noms).round(2),hide_index=True)
        gauche, droite = st.columns(2)
        with gauche:
            courbe(previsions_meteo,["tmin","tmax"],"Températures prévues","°C")
        with droite:
            courbe(previsions_meteo,["eto"],"ET0 prévue","mm/j")
        st.download_button("Exporter la météo et ses sources",previsions_meteo.to_csv(index=False,date_format="%Y-%m-%d"),"previsions_meteo.csv","text/csv")
        st.caption("Les pourcentages de pluie sont des probabilités, jamais des millimètres. Case vide = donnée non disponible. L’historique Open-Meteo est modélisé ; il ne remplace pas une station sur la parcelle.")
    if plan_eau.empty:
        st.info("Plan d’irrigation en attente : " + erreur_prevision)
    else:
        st.markdown("**Plan d’irrigation sur " + str(len(plan_eau)) + " jours**")
        champs = ["date","ETc","Pluie","Dr","Possible","Heures","Reste","Dr_apres_apport"]
        libelles = {"Pluie":"Pluie efficace déduite mm","Dr":"Déficit avant apport mm","Possible":"Dose brute mm",
            "Heures":"Durée h","Reste":"Besoin brut restant mm","Dr_apres_apport":"Déficit après apport proposé mm"}
        st.dataframe(plan_eau[champs].rename(columns=libelles).round(2),hide_index=True)
        st.download_button("Exporter le plan hydrique prévisionnel",plan_eau.to_csv(index=False,date_format="%Y-%m-%d"),"plan_irrigation_prevu.csv","text/csv")
        st.caption("Ce plan suppose les apports proposés exécutés avant le jour suivant. Il est recalculé avec les données nouvelles et ne modifie ni les irrigations réellement reçues ni le stock. Kc, Kr, RU et l’efficience sont supposés constants sur ce court horizon.")
        if deduire_pluie:
            st.caption("Sans crédit de pluie, la dose brute réalisable aujourd’hui serait " + afficher(plan_sans_pluie.iloc[0].Possible) + " mm. La différence dépend de la réalisation de la pluie et du coefficient renseigné.")

with st.expander("Fertilisation, stock et travaux", expanded=False):
    onglets = st.tabs(["Fertilisation", "Stock", "Travaux"])
    
    with onglets[0]:
        resultats_nutrition, donnees_nutrition = st.tabs(["Résultats", "Analyses et objectifs"])
        nutr = parametres_fertilisation(demo, parcelle.sol)
        sauvegarde_nutrition = registre.get("nutrition", {}).get(cle, {})
        if sauvegarde_nutrition.get("campagne",aujourdhui().year) == aujourdhui().year:
            nutr.update(sauvegarde_nutrition)
        brouillon_nutrition = cle + "fert:brouillon:" + str(aujourdhui().year)
        nutr.update(st.session_state.get(brouillon_nutrition, {}))
        if not demo and nutr.get("stade_valide") != stade:
            nutr["valide"] = False
        surface_identique = nutr["surface"] is None or nutr["surface"] == surface
        realises = dict.fromkeys(ELEMENTS, 0.0)
        dates_apports = []
        for travail in registre["travaux"]:
            if travail["Secteur"] == cle and travail["État"] == "Réalisé" and travail["Type"] == "Fertilisation" and travail["Réalisé le"][:4] == str(aujourdhui().year):
                surface_identique = surface_identique and surface == travail["Surface"]
                dates_apports.append(travail["Réalisé le"])
                for e in ELEMENTS:
                    realises[e] += travail["Apports"][e]
        with donnees_nutrition:
            st.caption("Campagne " + str(aujourdhui().year) + " · " + parcelle.sol + " · Tous les apports sont exprimés par hectare du secteur.")
            st.markdown("**Production et taille**")
            champs = [("rendement","Rendement prévu olives fraîches (t/ha)",0.0,50.0),
                ("taille","Résidus de taille frais (t/ha)",0.0,50.0),
                ("ms_taille","Matière sèche des résidus (%)",0.0,100.0),
                ("export_taille","Résidus retirés de la parcelle (%)",0.0,100.0),
                ("semaines","Semaines restantes",1.0,52.0)]
            colonnes = st.columns(2)
            for i, (champ,titre,minimum,maximum) in enumerate(champs):
                with colonnes[i % 2]:
                    nutr[champ] = saisie(titre,nutr[champ],cle+"fert:"+champ,minimum,maximum)
            st.caption("Le mélange de référence comprend bois, écorce, rameaux et feuilles. Si seul le bois est retiré, adapter sa composition dans les coefficients. Les résidus laissés sur place ne sont pas exportés.")
            sources_nutrition = st.tabs(["Sol", "Eau", "Organique", "Coefficients et historique"])
            with sources_nutrition[0]:
                st.markdown("**Profil réellement analysé**")
                champs = [("densite","Densité apparente terre fine sèche (g/cm³)",0.5,2.0),
                    ("z_analyse","Profondeur réellement analysée depuis 0 (m)",0.01,2.0),
                    ("z_racines","Profondeur racinaire active retenue (m)",0.01,2.0),
                    ("cailloux","Éléments grossiers (% du volume)",0.0,99.0),
                    ("surface_sol","Surface de sol représentée et accessible (%)",0.01,100.0),
                    ("pH_sol","pH du sol (facultatif)",3.0,10.0)]
                colonnes = st.columns(2)
                for i, (champ,titre,minimum,maximum) in enumerate(champs):
                    with colonnes[i % 2]:
                        nutr[champ] = saisie(titre,nutr[champ],cle+"fert:"+champ,minimum,maximum)
                date_reference = date.fromisoformat(nutr["date_analyse"]) if nutr["date_analyse"] else None
                date_reference = st.date_input("Date de l’analyse de référence",value=date_reference,max_value=aujourdhui(),key=cle+"fert:date_analyse")
                nutr["date_analyse"] = date_reference.isoformat() if date_reference else None
                nutr["methode_P"] = st.selectbox("Méthode d’analyse du phosphore",["Olsen","Bray-1","Mehlich-3","Autre documentée"],
                    index=["Olsen","Bray-1","Mehlich-3","Autre documentée"].index(nutr["methode_P"]),key=cle+"fert:methode_P")
                unites = [["mg N/kg","mg NO3/kg"],["mg P/kg","mg P2O5/kg"],["mg K/kg","mg K2O/kg","cmol(+)/kg","meq/100 g"]]
                for i, colonne in enumerate(st.columns(3)):
                    with colonne:
                        nutr["unites_sol"][i] = st.selectbox("Unité sol : " + ["N","P","K"][i],unites[i],
                            index=unites[i].index(nutr["unites_sol"][i]),key=cle+"fert:unite:"+str(i))
                nutr["sol"] = editer_nutrition(nutr["sol"],cle+"fert:sol")
            with sources_nutrition[1]:
                nutr["eau_nulle"] = st.checkbox("Contribution nutritive de l’eau confirmée nulle",value=nutr["eau_nulle"],key=cle+"fert:eau_nulle")
                if not nutr["eau_nulle"]:
                    nutr["volume_eau"] = saisie("Volume d’irrigation retenu pour la campagne (m³/ha)",nutr["volume_eau"],cle+"fert:volume_eau",0.0,20000.0)
                    nutr["forme_eau_N"] = st.selectbox("Résultat azote de l’eau",["NO3","N"],index=["NO3","N"].index(nutr["forme_eau_N"]),key=cle+"fert:forme_eau_N")
                    nutr["eau"] = editer_nutrition(nutr["eau"],cle+"fert:eau")
                st.caption("Analyse d’eau AVANT injection d’engrais : N ou NO3 selon le choix ; P et K élémentaires en mg/L. Crédit = concentration × volume / 1000 × part valorisable. Le volume futur est une prévision à réviser, pas une mesure reçue.")
            with sources_nutrition[2]:
                nutr["organique_nul"] = st.checkbox("Aucun apport organique crédité cette campagne",value=nutr["organique_nul"],key=cle+"fert:organique_nul")
                if not nutr["organique_nul"]:
                    nutr["masse_organique"] = saisie("Apport organique confirmé (t de produit/ha)",nutr["masse_organique"],cle+"fert:masse_organique",0.0,100.0)
                    nutr["organique"] = editer_nutrition(nutr["organique"],cle+"fert:organique",maximum=100.0)
                st.caption("Teneurs N-P2O5-K2O sur le produit tel qu’apporté. Si l’analyse est sur matière sèche, convertir d’abord la composition. Ne créditer que la part disponible pendant la campagne ; humiques et biostimulants n’ont pas un crédit NPK automatique.")
            with sources_nutrition[3]:
                st.markdown("**Coefficients d’exportation — N, P et K élémentaires**")
                nutr["exports"] = editer_nutrition(nutr["exports"],cle+"fert:exports")
                st.caption("Référence Rodrigues et al. (2012), Cobrançosa au Portugal. Valeurs dérivées des masses et exportations des tableaux 2 et 5 ; elles sont modifiables et ne représentent pas des analyses d’OLEA.")
                st.markdown("**Ajustements et apports minéraux antérieurs**")
                nutr["mineral"] = editer_nutrition(nutr["mineral"],cle+"fert:mineral",minimum=-300.0)
                st.caption("Ajustement signé validé par le diagnostic sol/foliaire et la croissance. Efficience entre 0 et 100 %, strictement positive. L’historique concerne les apports AVANT le registre, sans reprendre ceux marqués réalisés ici.")
                st.markdown("**Analyses foliaires — diagnostic facultatif**")
                date_foliaire = date.fromisoformat(nutr["date_foliaire"]) if nutr["date_foliaire"] else None
                date_foliaire = st.date_input("Date du prélèvement foliaire",value=date_foliaire,max_value=aujourdhui(),key=cle+"fert:date_foliaire")
                nutr["date_foliaire"] = date_foliaire.isoformat() if date_foliaire else None
                nutr["foliaire"] = editer_nutrition(nutr["foliaire"],cle+"fert:foliaire",maximum=100.0)
                st.caption("N, P et K élémentaires en % de matière sèche. Repères UC/CDFA uniquement pour juillet ; les seuils restent à valider selon le protocole local. Le résultat oriente l’ajustement validé et ne crée pas un multiplicateur de dose.")
                mois_foliaire = date.fromisoformat(nutr["date_foliaire"]).month if nutr["date_foliaire"] else None
                diagnostic = []
                for ligne in nutr["foliaire"]:
                    diagnostic.append({"Élément":ligne["Élément"],"Foliaire (%)":ligne["Teneur (%)"],
                        "Lecture":diagnostic_foliaire(ligne["Teneur (%)"],ligne["Élément"],mois_foliaire)})
                st.markdown("**Appui du diagnostic foliaire**")
                st.dataframe(pd.DataFrame(diagnostic),hide_index=True)
            nutr["valide"] = st.checkbox("Programme et répartition hebdomadaire validés au stade observé",value=nutr["valide"],key=cle+"nutrition:"+stade)
            nutr["stade_valide"] = stade if nutr["valide"] else None
            st.caption("Cette validation inclut le diagnostic foliaire, la représentativité du sol et une analyse de référence antérieure aux apports minéraux comptabilisés. Les efficiences et parts mobilisables de simulation ne sont pas des normes agronomiques.")
            if st.button("Enregistrer les paramètres de fertilisation",key=cle+"fert:enregistrer",disabled=lecture_seule):
                if surface is not None and secteur.strip():
                    nutr["surface"] = surface
                    registre.setdefault("nutrition", {})[cle] = nutr
                    enregistrer(base)
                    st.success("Paramètres enregistrés pour ce secteur.")
                else:
                    st.error("Renseigner la surface et le secteur dans Données et consignes.")
          
            st.session_state[brouillon_nutrition] = nutr
        budget, plan = None, pd.DataFrame()
        erreur_nutrition = ""
        try:
            budget = bilan_fertilisation(nutr,realises)
            if nutr["date_analyse"] is None:
                raise ValueError("Renseigner la date d’analyse de référence.")
            if dates_apports and nutr["date_analyse"] > min(dates_apports):
                raise ValueError("Analyse après un apport comptabilisé : revoir la référence pour éviter un double crédit.")
            if nutr["methode_P"] == "Bray-1" and nutr["pH_sol"] is not None and nutr["pH_sol"] >= 7.4:
                raise ValueError("Sol alcalin : vérifier la méthode P avec le laboratoire ; Olsen est généralement plus adapté.")
            if not surface_identique:
                raise ValueError("La surface a changé : utiliser un autre identifiant de secteur.")
            if surface is None or not secteur.strip():
                raise ValueError("Surface et identifiant du secteur à renseigner.")
            if not nutr["valide"]:
                raise ValueError("Valider les paramètres et le programme au stade observé.")
            besoins = budget.set_index("Élément")["Cette semaine (kg/ha)"].to_dict()
            plan, manques = plan_engrais(besoins,registre["stock"],surface)
        except (ValueError,TypeError,KeyError) as erreur:
            erreur_nutrition = str(erreur)
        with resultats_nutrition:
            st.markdown("**Plan hebdomadaire · " + stade + "**")
            st.caption("Simulation" if demo else "Estimation à partir des données saisies · campagne " + str(aujourdhui().year))
            if erreur_nutrition:
                st.info("Plan en attente : compléter / vérifier Analyses et objectifs.")
                with donnees_nutrition:
                    st.warning(erreur_nutrition)
            else:
                st.dataframe(budget[["Élément","Programme minéral","Réalisé (kg/ha)","Solde (kg/ha)","Cette semaine (kg/ha)"]].round(3),hide_index=True)
                st.caption("N, P2O5 et K2O en kg/ha. Les apports avant registre sont déjà déduits du solde.")
                if not plan.empty:
                    st.markdown("**Engrais à préparer cette semaine**")
                    st.dataframe(plan.round(3),hide_index=True)
                    calendrier = []
                    for _, ligne in plan.iterrows():
                        for decalage in [0,3,6]:
                            calendrier.append({"Date":(aujourdhui()+timedelta(days=decalage)).isoformat(),"Produit":ligne.Produit,"kg du secteur":ligne["kg du secteur"]/3})
                    st.markdown("**Trois apports proposés — produits séparés**")
                    st.dataframe(pd.DataFrame(calendrier).round(3),hide_index=True)
                    st.download_button("Exporter le plan hebdomadaire",pd.DataFrame(calendrier).to_csv(index=False),"plan_engrais.csv","text/csv")
                    if st.button("Ajouter le plan aux travaux",key=cle+"fert:taches",disabled=lecture_seule):
                        ajoutes = 0
                        for ligne in calendrier:
                            deja = any(t["Secteur"] == cle and t["Type"] == "Fertilisation" and t["Date"] == ligne["Date"] and t["Produit"] == ligne["Produit"] and t.get("Origine") == "Plan fertilisation" and t["État"] != "Annulé" for t in registre["travaux"])
                            if not deja:
                                registre["travaux"].append({"Secteur":cle,"Surface":surface,"Type":"Fertilisation","Travail":ligne["Produit"]+" · "+afficher(ligne["kg du secteur"])+" kg proposés","Date":ligne["Date"],"État":"Prévu","Produit":ligne["Produit"],"Réalisé le":"","Consommé":None,"Apports":{},"Origine":"Plan fertilisation"})
                                ajoutes += 1
                        enregistrer(base)
                        st.success(str(ajoutes) + " tâches ajoutées. Aucun stock consommé ou réservé.")
                        if ajoutes < len(calendrier):
                            st.info("Les interventions déjà présentes gardent leur dose. Vérifier ou annuler les tâches prévues avant de réviser le plan.")
                elif max(besoins.values()) <= 0.000001:
                    st.success("Aucun complément minéral calculé pour cette semaine.")
                if any(v > 0.000001 for v in manques.values()):
                    st.warning("Besoins non couverts par un produit vérifié sans excès de coélément : " + str({e:round(v,3) for e,v in manques.items()}))
                comparaison = budget.set_index("Élément")[["Fruits","Taille exportée","Crédit sol","Crédit eau","Crédit organique"]]
                st.markdown("**Exportations et contributions disponibles — kg/ha/campagne**")
                st.bar_chart(comparaison,height=230,color=["#47745a","#839aab","#c89845","#679da5","#91ab65"])
                st.caption("Les stocks analytiques du sol ne sont pas les crédits déduits. Les dates J, J+3 et J+6 et les fractions égales restent à valider avec l’irrigation et le stade. Le plan ne réserve pas le stock.")
                st.download_button("Exporter le bilan de fertilisation",budget.to_csv(index=False),"bilan_fertilisation.csv","text/csv")



    with onglets[1]:
        st.caption("Stock commun aux secteurs du même mode. Case vide = inconnu. Les travaux prévus ne réservent pas le stock.")
        colonnes_stock = {}
        for champ in ["Quantité", "N", "P2O5", "K2O"]:
            colonnes_stock[champ] = st.column_config.NumberColumn(min_value=0.0)
        edition = st.data_editor(pd.DataFrame(registre["stock"]), disabled=["Produit"], column_config=colonnes_stock, key=mode+":stock", num_rows="fixed")
        if st.button("Enregistrer le comptage", disabled=lecture_seule):
            nouveau = edition.where(pd.notna(edition), None).to_dict("records")
            valide = True
            for produit in nouveau:
                for champ in ["Quantité", "N", "P2O5", "K2O"]:
                    if pd.isna(produit[champ]):
                        produit[champ] = None
                    elif not math.isfinite(float(produit[champ])) or produit[champ] < 0 or (champ != "Quantité" and produit[champ] > 100):
                        valide = False
                if produit["Unité"] not in ["kg","L","unité"] or produit["Catégorie"] not in CATEGORIES:
                    valide = False
            if valide:
                registre["stock"] = nouveau
                enregistrer(base)
                st.rerun()
            st.error("Vérifier les quantités, les pourcentages, les catégories et les unités.")
        with st.form(mode+":nouveau_produit"):
            nom_produit = st.text_input("Ajouter un produit : nom")
            categorie = st.selectbox("Catégorie", CATEGORIES)
            unite = st.selectbox("Unité", ["kg","L","unité"])
            if st.form_submit_button("Ajouter la ligne", disabled=lecture_seule):
                if nom_produit.strip() and nom_produit not in [p["Produit"] for p in registre["stock"]]:
                    registre["stock"].append({"Produit":nom_produit,"Catégorie":categorie,"Unité":unite,"Quantité":None,"N":None,"P2O5":None,"K2O":None,"Vérifié":False})
                    enregistrer(base)
                    st.rerun()
                st.error("Nom unique requis.")
        st.caption("Après réception, actualiser le comptage. Organiques, humiques et phyto restent dans le journal ; seul un engrais minéral en kg de composition vérifiée entre dans le calcul automatique.")


    
    with onglets[2]:
        travaux = []
        for numero, travail in enumerate(registre["travaux"]):
            if travail["Secteur"] == cle:
                travaux.append(dict(travail, Numero=numero))
        if travaux:
            st.dataframe(pd.DataFrame(travaux).drop(columns=["Apports","Secteur"]), hide_index=True)
        with st.form(cle+"travail"):
            type_travail = st.selectbox("Intervention", INTERVENTIONS)
            titre = st.text_input("Travail / cible / observations")
            jour = st.date_input("Date prévue", aujourdhui())
            produit = st.selectbox("Produit consommé si nécessaire", [""]+[p["Produit"] for p in registre["stock"]])
            if st.form_submit_button("Prévoir la tâche", disabled=lecture_seule):
                if titre.strip() and surface is not None and (type_travail != "Fertilisation" or produit):
                    registre["travaux"].append({"Secteur":cle,"Surface":surface,"Type":type_travail,"Travail":titre,"Date":jour.isoformat(),"État":"Prévu","Produit":produit,"Réalisé le":"","Consommé":None,"Apports":{}})
                    enregistrer(base)
                    st.rerun()
                st.error("Renseigner le travail, la surface et le produit pour une fertilisation.")
        en_attente = [t["Numero"] for t in travaux if t["État"] == "Prévu"]
        if en_attente:
            choix_taches = []
            for n in en_attente:
                choix_taches.append(str(n) + " : " + registre["travaux"][n]["Travail"])
            choix_tache = st.selectbox("Tâche à réaliser", choix_taches, key=cle+"a_realiser")
            numero = int(choix_tache.split(" : ", 1)[0])
            with st.form(cle+"realisation:"+str(numero)):
                quantite = saisie("Quantité réellement consommée · unité du produit", None, cle+"consommation:"+str(numero))
                jour = st.date_input("Date réelle", aujourdhui(), max_value=aujourdhui())
                if st.form_submit_button("Marquer réalisé", disabled=lecture_seule):
                    try:
                        realiser_travail(registre, numero, quantite, jour.isoformat())
                        enregistrer(base)
                        st.rerun()
                    except ValueError as erreur:
                        st.error(str(erreur))
            if st.button("Annuler la tâche", disabled=lecture_seule):
                registre["travaux"][numero]["État"] = "Annulé"
                enregistrer(base)
                st.rerun()
        st.caption("Consommation en kg, L ou unité selon le produit. Pour une récolte : noter rendement olives/ha, rendement huile et qualité dans les observations.")
        st.markdown("[Index phytosanitaire ONSSA](https://eservice.onssa.gov.ma/IndPesticide.aspx)")
