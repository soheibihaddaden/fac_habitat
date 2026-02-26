#!/usr/bin/env python3
"""
Moniteur de disponibilité FAC-HABITAT Île-de-France.
Tourne via GitHub Actions, envoie des notifications Telegram,
et génère une page HTML statique pour GitHub Pages.
"""

import json
import os
import re
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

from bs4 import BeautifulSoup
import requests

# ── Configuration ────────────────────────────────────────────────────────────

BASE_URL = "https://www.fac-habitat.com"
JSON_URL = f"{BASE_URL}/fr/residences/json"
RESIDENCE_URL = f"{BASE_URL}/fr/residences-etudiantes/id-{{id}}"

IDF_PREFIXES = ("75", "77", "78", "91", "92", "93", "94", "95")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Telegram (via variables d'environnement GitHub Secrets)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Fichier pour éviter les notifications en doublon
STATE_FILE = "last_state.json"


# ── Scraping ─────────────────────────────────────────────────────────────────

def get_idf_residences():
    """Récupère la liste des résidences IDF depuis l'API JSON."""
    resp = requests.get(JSON_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    idf = []
    for rid, info in data.items():
        cp = info.get("cp", "")
        nom = info.get("titre", info.get("titre_fr", ""))
        if cp.startswith(IDF_PREFIXES) and "logifac" not in nom.lower():
            idf.append({
                "id": rid,
                "nom": info.get("titre", info.get("titre_fr", f"Résidence {rid}")),
                "ville": info.get("ville", "?"),
                "cp": cp,
            })

    idf.sort(key=lambda r: (r["cp"], r["nom"]))
    return idf


def get_iframe_url(residence_id):
    """Récupère l'URL de l'iframe de réservation."""
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
    """Analyse l'iframe pour détecter les disponibilités."""
    resp = requests.get(iframe_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    results = []
    rows = soup.find_all("tr")
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 2:
            continue

        type_cell = cols[0].get_text(strip=True)
        if not re.match(r"T\d", type_cell):
            continue

        loyer = cols[1].get_text(strip=True) if len(cols) > 1 else ""
        surface = cols[2].get_text(strip=True) if len(cols) > 2 else ""

        last_col = cols[-1]
        last_col_text = last_col.get_text(" ", strip=True)

        has_btn = last_col.find("a", class_="btn_reserver") is not None
        has_dispo_immed = bool(re.search(
            r"disponibilit[eé]\s*imm[eé]diate", last_col_text, re.IGNORECASE
        ))

        dispo_span = last_col.find("span", class_="dispo")
        is_green = False
        if dispo_span:
            is_green = "green" in dispo_span.get("class", [])

        has_aucune = bool(re.search(
            r"aucune\s*disponibilit[eé]", last_col_text, re.IGNORECASE
        ))

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

    if not results:
        has_deposer = "poser une demande" in html.lower() or "btn_reserver" in html
        has_immed = "immédiate" in html or "imm&eacute;diate" in html
        has_aucune = "aucune disponibilit" in html.lower()

        if has_immed:
            results.append({"type": "?", "loyer": "", "surface": "", "status": "DISPONIBLE"})
        elif has_deposer and not has_aucune:
            results.append({"type": "?", "loyer": "", "surface": "", "status": "DEPOSER_DEMANDE"})

    return results


# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(message):
    """Envoie un message via Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] Token ou Chat ID manquant, notification ignorée.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print("[TELEGRAM] Message envoyé !")
            else:
                print(f"[TELEGRAM] Erreur: {resp.status}")
    except Exception as e:
        print(f"[TELEGRAM] Erreur d'envoi: {e}")


# ── État (anti-doublon) ─────────────────────────────────────────────────────

def load_previous_state():
    """Charge l'état précédent pour détecter les changements."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── Génération HTML ──────────────────────────────────────────────────────────

def generate_html(all_results, scan_time):
    """Génère une page HTML statique avec les résultats."""

    disponibles = []
    demandes = []
    indisponibles = []

    for res, logements in all_results:
        statuses = [l["status"] for l in logements]
        if "DISPONIBLE" in statuses:
            disponibles.append((res, logements))
        elif "DEPOSER_DEMANDE" in statuses:
            demandes.append((res, logements))
        elif "DEMANDE_POSSIBLE" in statuses:
            demandes.append((res, logements))
        else:
            indisponibles.append((res, logements))

    def render_card(res, logements, card_class):
        url = f"{BASE_URL}/fr/residences-etudiantes/id-{res['id']}"
        rows = ""
        for l in logements:
            badge = {
                "DISPONIBLE": '<span class="badge bg-success">Disponibilité immédiate</span>',
                "DEPOSER_DEMANDE": '<span class="badge bg-primary">Déposer une demande</span>',
                "DEMANDE_POSSIBLE": '<span class="badge bg-warning text-dark">Demande possible</span>',
                "INDISPONIBLE": '<span class="badge bg-secondary">Indisponible</span>',
            }.get(l["status"], "")
            rows += f"""<tr>
                <td>{l['type']}</td>
                <td>{l['loyer']}</td>
                <td>{l['surface']}</td>
                <td>{badge}</td>
            </tr>"""

        return f"""
        <div class="col-md-6 col-lg-4 mb-4">
            <div class="card {card_class} h-100">
                <div class="card-body">
                    <h5 class="card-title">{res['nom']}</h5>
                    <p class="card-text text-muted">{res['ville']} ({res['cp']})</p>
                    <table class="table table-sm table-bordered">
                        <thead><tr><th>Type</th><th>Loyer</th><th>Surface</th><th>Statut</th></tr></thead>
                        <tbody>{rows}</tbody>
                    </table>
                    <a href="{url}" target="_blank" class="btn btn-sm btn-outline-primary">Voir sur FAC-HABITAT</a>
                </div>
            </div>
        </div>"""

    dispo_cards = "".join(render_card(r, l, "border-success") for r, l in disponibles)
    demande_cards = "".join(render_card(r, l, "border-primary") for r, l in demandes)
    indispo_cards = "".join(render_card(r, l, "border-secondary") for r, l in indisponibles)

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FAC-HABITAT IDF — Disponibilités</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body {{ background: #f4f6f9; }}
        .card.border-success {{ border-width: 3px; }}
        .hero {{ background: linear-gradient(135deg, #1a8754, #0d6efd); color: white; padding: 2rem 0; }}
        .badge {{ font-size: 0.8rem; }}
        .stats {{ font-size: 1.3rem; font-weight: 600; }}
    </style>
</head>
<body>
    <div class="hero text-center">
        <div class="container">
            <h1>FAC-HABITAT Île-de-France</h1>
            <p class="lead">Disponibilités des résidences étudiantes</p>
            <p>Dernier scan : <strong>{scan_time}</strong></p>
            <div class="row justify-content-center mt-3">
                <div class="col-auto stats"><span class="badge bg-success fs-6">{len(disponibles)}</span> Dispo immédiate</div>
                <div class="col-auto stats"><span class="badge bg-primary fs-6">{len(demandes)}</span> Demande ouverte</div>
                <div class="col-auto stats"><span class="badge bg-secondary fs-6">{len(indisponibles)}</span> Indisponible</div>
            </div>
        </div>
    </div>

    <div class="container mt-4">
        {"<h2 class='text-success mb-3'>Disponibilité immédiate</h2><div class='row'>" + dispo_cards + "</div>" if disponibles else ""}
        {"<h2 class='text-primary mb-3 mt-4'>Demande ouverte</h2><div class='row'>" + demande_cards + "</div>" if demandes else ""}
        {"<h2 class='text-secondary mb-3 mt-4'>Indisponible</h2><div class='row'>" + indispo_cards + "</div>" if indisponibles else ""}
    </div>

    <footer class="text-center text-muted py-4">
        <small>Mise à jour automatique toutes les 10 minutes via GitHub Actions.</small>
    </footer>
</body>
</html>"""
    return html


# ── Scan principal ───────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  FAC-HABITAT Monitor — Île-de-France")
    print("=" * 60)

    residences = get_idf_residences()
    print(f"[*] {len(residences)} résidences IDF trouvées\n")

    previous_state = load_previous_state()
    current_state = {}
    all_results = []
    new_availabilities = []

    for i, res in enumerate(residences, 1):
        label = f"{res['nom']} ({res['ville']})"
        print(f"  [{i:2d}/{len(residences)}] {label}...", end=" ", flush=True)

        try:
            iframe_url = get_iframe_url(res["id"])
            if not iframe_url:
                print("skip (pas d'iframe)")
                all_results.append((res, []))
                continue

            logements = check_availability(iframe_url)
            all_results.append((res, logements))

            for l in logements:
                key = f"{res['id']}_{l['type']}"
                current_state[key] = l["status"]

                # Notifier seulement si le statut CHANGE vers un meilleur état
                prev = previous_state.get(key, "INDISPONIBLE")
                rank = {"INDISPONIBLE": 0, "DEMANDE_POSSIBLE": 1, "DEPOSER_DEMANDE": 2, "DISPONIBLE": 3}
                if rank.get(l["status"], 0) > rank.get(prev, 0):
                    new_availabilities.append({
                        "residence": res["nom"],
                        "ville": res["ville"],
                        "type": l["type"],
                        "loyer": l["loyer"],
                        "status": l["status"],
                        "prev_status": prev,
                        "url": f"{BASE_URL}/fr/residences-etudiantes/id-{res['id']}",
                    })

            statuses = [l["status"] for l in logements]
            if "DISPONIBLE" in statuses:
                print("DISPO !")
            elif "DEPOSER_DEMANDE" in statuses:
                print("demande ouverte")
            elif "DEMANDE_POSSIBLE" in statuses:
                print("demande possible")
            else:
                print("indisponible")

        except Exception as e:
            print(f"erreur: {e}")
            all_results.append((res, []))

        time.sleep(0.5)

    # ── Sauvegarder l'état ──
    save_state(current_state)

    # ── Générer la page HTML ──
    scan_time = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    html = generate_html(all_results, scan_time)
    os.makedirs("public", exist_ok=True)
    with open("public/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n[*] Page HTML générée dans public/index.html")

    # ── Envoyer les notifications Telegram ──
    if new_availabilities:
        print(f"\n[!] {len(new_availabilities)} NOUVELLE(S) DISPONIBILITÉ(S) !")

        msg = "<b>🏠 FAC-HABITAT — Changement détecté !</b>\n\n"
        for a in new_availabilities:
            status_map = {
                "DISPONIBLE": ("🟢", "Dispo immédiate"),
                "DEPOSER_DEMANDE": ("🔵", "Demande ouverte"),
                "DEMANDE_POSSIBLE": ("🟡", "Demande possible"),
            }
            emoji, status_txt = status_map.get(a["status"], ("⚪", a["status"]))
            prev_map = {
                "INDISPONIBLE": "Indisponible",
                "DEMANDE_POSSIBLE": "Demande possible",
                "DEPOSER_DEMANDE": "Demande ouverte",
            }
            prev_txt = prev_map.get(a.get("prev_status", ""), "?")
            msg += (
                f"{emoji} <b>{a['residence']}</b> — {a['ville']}\n"
                f"   {a['type']} | {a['loyer']}\n"
                f"   {prev_txt} → <b>{status_txt}</b>\n"
                f"   <a href=\"{a['url']}\">Voir / Réserver</a>\n\n"
            )

        send_telegram(msg)
    else:
        print("\n[*] Pas de nouvelle disponibilité depuis le dernier scan.")

    # ── Résumé console ──
    nb_dispo = sum(1 for _, l in all_results if any(x["status"] == "DISPONIBLE" for x in l))
    nb_demande = sum(1 for _, l in all_results if any(x["status"] == "DEPOSER_DEMANDE" for x in l))
    print(f"\n[*] Résumé : {nb_dispo} dispo immédiate, {nb_demande} demande ouverte")


if __name__ == "__main__":
    main()
