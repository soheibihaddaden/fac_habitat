#!/usr/bin/env python3
"""
Moniteur de disponibilité des résidences FAC-HABITAT en Île-de-France.

Scrape toutes les résidences IDF, détecte les disponibilités
("Déposer une demande" / "Disponibilité immédiate") et alerte l'utilisateur.

Usage:
    python3 fac_habitat_monitor.py              # scan unique
    python3 fac_habitat_monitor.py --loop 300   # scan toutes les 5 min
    python3 fac_habitat_monitor.py --loop 300 --notify  # scan + notification desktop
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# ── Configuration ────────────────────────────────────────────────────────────

BASE_URL = "https://www.fac-habitat.com"
JSON_URL = f"{BASE_URL}/fr/residences/json"
RESIDENCE_URL = f"{BASE_URL}/fr/residences-etudiantes/id-{{id}}"

# Codes postaux Île-de-France (75, 77, 78, 91, 92, 93, 94, 95)
IDF_PREFIXES = ("75", "77", "78", "91", "92", "93", "94", "95")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Couleurs ANSI
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


# ── Fonctions utilitaires ────────────────────────────────────────────────────

def get_idf_residences():
    """Récupère la liste des résidences IDF depuis l'API JSON."""
    print(f"{CYAN}[*] Récupération de la liste des résidences...{RESET}")
    resp = requests.get(JSON_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    idf = []
    for rid, info in data.items():
        cp = info.get("cp", "")
        if cp.startswith(IDF_PREFIXES):
            idf.append({
                "id": rid,
                "nom": info.get("titre", info.get("titre_fr", f"Résidence {rid}")),
                "ville": info.get("ville", "?"),
                "cp": cp,
                "adresse": info.get("adresse", ""),
            })

    idf.sort(key=lambda r: (r["cp"], r["nom"]))
    print(f"{CYAN}[*] {len(idf)} résidences trouvées en Île-de-France{RESET}\n")
    return idf


def get_iframe_url(residence_id):
    """Récupère l'URL de l'iframe de réservation depuis la page de la résidence."""
    url = RESIDENCE_URL.format(id=residence_id)
    resp = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    iframe = soup.find("iframe", class_="reservation")
    if iframe and iframe.get("src"):
        src = iframe["src"]
        if not src.startswith("http"):
            src = "https://espacelocataire.fac-habitat.com" + src
        return src
    return None


def check_availability(iframe_url):
    """
    Analyse le contenu de l'iframe pour détecter les disponibilités.
    Retourne une liste de dict avec les infos par type de logement.
    """
    resp = requests.get(iframe_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    results = []

    # Chercher toutes les lignes du tableau de réservation
    rows = soup.find_all("tr")
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 2:
            continue

        # Extraire le type de logement (T1, T1 Bis, T2, etc.)
        type_cell = cols[0].get_text(strip=True)
        if not re.match(r"T\d", type_cell):
            continue

        # Extraire le loyer
        loyer = cols[1].get_text(strip=True) if len(cols) > 1 else ""
        surface = cols[2].get_text(strip=True) if len(cols) > 2 else ""

        # Vérifier la dernière colonne pour la disponibilité
        last_col = cols[-1]
        last_col_text = last_col.get_text(" ", strip=True)

        has_btn = last_col.find("a", class_="btn_reserver") is not None
        has_dispo_immed = bool(re.search(
            r"disponibilit[eé]\s*imm[eé]diate", last_col_text, re.IGNORECASE
        ))

        dispo_span = last_col.find("span", class_="dispo")
        is_green = False
        is_red = False
        if dispo_span:
            span_classes = dispo_span.get("class", [])
            is_green = "green" in span_classes
            is_red = "red" in span_classes

        has_aucune = bool(re.search(
            r"aucune\s*disponibilit[eé]", last_col_text, re.IGNORECASE
        ))

        # Déterminer le statut
        if has_dispo_immed or is_green:
            status = "DISPONIBLE"
        elif has_btn and not has_aucune:
            status = "DEPOSER_DEMANDE"
        elif has_btn and has_aucune:
            status = "DEMANDE_POSSIBLE"
        else:
            status = "INDISPONIBLE"

        results.append({
            "type": type_cell,
            "loyer": loyer,
            "surface": surface,
            "status": status,
        })

    # Fallback : chercher directement dans le HTML brut
    if not results:
        has_deposer = "poser une demande" in html.lower() or "btn_reserver" in html
        has_immed = "immédiate" in html or "imm&eacute;diate" in html
        has_aucune = "aucune disponibilit" in html.lower() or "aucune disponibilit" in html

        if has_immed:
            results.append({"type": "?", "loyer": "", "surface": "", "status": "DISPONIBLE"})
        elif has_deposer and not has_aucune:
            results.append({"type": "?", "loyer": "", "surface": "", "status": "DEPOSER_DEMANDE"})
        elif has_deposer:
            results.append({"type": "?", "loyer": "", "surface": "", "status": "DEMANDE_POSSIBLE"})

    return results


def notify_desktop(title, message):
    """Envoie une notification desktop (Linux)."""
    try:
        subprocess.run(
            ["notify-send", "-u", "critical", "-t", "30000", title, message],
            check=False, timeout=5,
        )
    except FileNotFoundError:
        pass


def play_alert_sound():
    """Joue un bip sonore."""
    try:
        subprocess.run(["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"],
                        check=False, timeout=5)
    except FileNotFoundError:
        # Fallback : bip terminal
        print("\a", end="", flush=True)


def format_status(status):
    """Formatte le statut avec couleurs."""
    if status == "DISPONIBLE":
        return f"{GREEN}{BOLD}★ DISPONIBILITÉ IMMÉDIATE ★{RESET}"
    elif status == "DEPOSER_DEMANDE":
        return f"{GREEN}● Déposer une demande{RESET}"
    elif status == "DEMANDE_POSSIBLE":
        return f"{YELLOW}○ Demande possible (aucune dispo){RESET}"
    else:
        return f"{RED}✗ Aucune disponibilité{RESET}"


# ── Boucle principale ───────────────────────────────────────────────────────

def scan_all(residences, notify=False):
    """Scanne toutes les résidences et affiche les résultats."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*80}")
    print(f"{BOLD}  FAC-HABITAT — Scan de disponibilité Île-de-France")
    print(f"  {now}{RESET}")
    print(f"{'='*80}\n")

    available = []
    demande_possible = []
    errors = []

    for i, res in enumerate(residences, 1):
        label = f"{res['nom']} ({res['ville']}, {res['cp']})"
        print(f"  [{i:2d}/{len(residences)}] {label}...", end=" ", flush=True)

        try:
            iframe_url = get_iframe_url(res["id"])
            if not iframe_url:
                print(f"{YELLOW}iframe non trouvée{RESET}")
                errors.append(label)
                continue

            results = check_availability(iframe_url)

            if not results:
                print(f"{RED}✗ Pas de données{RESET}")
                continue

            statuses = [r["status"] for r in results]

            if "DISPONIBLE" in statuses:
                print(f"{GREEN}{BOLD}★ DISPO IMMÉDIATE !{RESET}")
                available.append((res, results))
            elif "DEPOSER_DEMANDE" in statuses:
                print(f"{GREEN}● Demande ouverte{RESET}")
                available.append((res, results))
            elif "DEMANDE_POSSIBLE" in statuses:
                print(f"{YELLOW}○ Demande possible{RESET}")
                demande_possible.append((res, results))
            else:
                print(f"{RED}✗{RESET}")

        except Exception as e:
            print(f"{RED}Erreur: {e}{RESET}")
            errors.append(label)

        # Pause pour ne pas surcharger le serveur
        time.sleep(0.5)

    # ── Résumé ──
    print(f"\n{'='*80}")
    print(f"{BOLD}  RÉSUMÉ{RESET}")
    print(f"{'='*80}")

    if available:
        print(f"\n{GREEN}{BOLD}  ✓ DISPONIBILITÉS TROUVÉES ({len(available)}) :{RESET}\n")
        alert_lines = []
        for res, results in available:
            url = f"{BASE_URL}/fr/residences-etudiantes/id-{res['id']}"
            print(f"    {BOLD}{res['nom']}{RESET} — {res['ville']} ({res['cp']})")
            print(f"    {CYAN}{url}{RESET}")
            for r in results:
                if r["status"] in ("DISPONIBLE", "DEPOSER_DEMANDE"):
                    print(f"      {r['type']:10s} {r['loyer']:30s} {format_status(r['status'])}")
                    alert_lines.append(f"{res['nom']} - {r['type']}: {r['status']}")
            print()

        if notify:
            msg = "\n".join(alert_lines[:5])
            notify_desktop("FAC-HABITAT — Disponibilité !", msg)
            play_alert_sound()

    if demande_possible:
        print(f"\n{YELLOW}  ○ Demandes possibles (mais aucune dispo) ({len(demande_possible)}) :{RESET}\n")
        for res, results in demande_possible:
            url = f"{BASE_URL}/fr/residences-etudiantes/id-{res['id']}"
            print(f"    {res['nom']} — {res['ville']} ({res['cp']})")
            print(f"    {CYAN}{url}{RESET}")
            for r in results:
                if r["status"] == "DEMANDE_POSSIBLE":
                    print(f"      {r['type']:10s} {r['loyer']:30s} {format_status(r['status'])}")
            print()

    if not available and not demande_possible:
        print(f"\n{RED}  ✗ Aucune disponibilité sur l'ensemble des résidences IDF.{RESET}\n")

    if errors:
        print(f"\n{YELLOW}  ⚠ Erreurs sur {len(errors)} résidence(s) : {', '.join(errors[:5])}{RESET}\n")

    return len(available) > 0


def main():
    parser = argparse.ArgumentParser(
        description="Moniteur de disponibilité FAC-HABITAT Île-de-France"
    )
    parser.add_argument(
        "--loop", type=int, default=0, metavar="SECONDS",
        help="Intervalle de vérification en secondes (ex: 300 pour 5 min). "
             "0 = scan unique."
    )
    parser.add_argument(
        "--notify", action="store_true",
        help="Activer les notifications desktop (nécessite notify-send)"
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Afficher la liste des résidences IDF et quitter"
    )
    args = parser.parse_args()

    residences = get_idf_residences()

    if args.list:
        print(f"\n{BOLD}Résidences FAC-HABITAT en Île-de-France ({len(residences)}) :{RESET}\n")
        for i, r in enumerate(residences, 1):
            url = f"{BASE_URL}/fr/residences-etudiantes/id-{r['id']}"
            print(f"  {i:2d}. {r['nom']:30s} {r['ville']:25s} ({r['cp']})  {url}")
        return

    if args.loop > 0:
        print(f"{CYAN}[*] Mode boucle : scan toutes les {args.loop} secondes{RESET}")
        print(f"{CYAN}[*] Appuyez sur Ctrl+C pour arrêter{RESET}\n")
        try:
            while True:
                scan_all(residences, notify=args.notify)
                print(f"\n{CYAN}[*] Prochain scan dans {args.loop}s...{RESET}")
                time.sleep(args.loop)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}[*] Arrêt du moniteur.{RESET}")
    else:
        scan_all(residences, notify=args.notify)


if __name__ == "__main__":
    main()
