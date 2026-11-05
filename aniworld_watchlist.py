#!/usr/bin/env python3
"""
AniWorld Watchlist-Tool
=======================

Holt deine Watchlist von aniworld.to, speichert sie lokal und gleicht sie bei
jedem Update ab:
  * neue Anime werden hinzugefügt (nur für diese werden Details geladen)
  * Anime, die nicht mehr in der Watchlist sind (z.B. fertig geschaut),
    wandern in die Liste "Entfernt / fertig"
  * alles andere bleibt, wie es ist – nichts wird komplett neu geladen

Benutzung (ohne Argumente startet ein Menü):
  python3 aniworld_watchlist.py                 Menü
  python3 aniworld_watchlist.py login           einloggen (einmalig)
  python3 aniworld_watchlist.py sync            Watchlist abgleichen
  python3 aniworld_watchlist.py zufall          zufälligen Anime vorschlagen
  python3 aniworld_watchlist.py zufall -g Action -n 3 --oeffnen
  python3 aniworld_watchlist.py zufall -s dub    (nur mit deutscher Synchro)
  python3 aniworld_watchlist.py zufall -s sub    (mit deutschen Untertiteln)
  python3 aniworld_watchlist.py zufall -s nursub (nur Untertitel, keine Synchro)
  python3 aniworld_watchlist.py zufall -f angefangen   (weiterschauen)
  python3 aniworld_watchlist.py zufall -f eine         (nur noch 1 Folge offen)
  python3 aniworld_watchlist.py liste -f offen   (nur mit noch offenen Folgen)
  python3 aniworld_watchlist.py liste [-g Genre] [-s dub|sub|nursub] [--entfernt]
  python3 aniworld_watchlist.py suche <text>
  python3 aniworld_watchlist.py genres
  python3 aniworld_watchlist.py online-einrichten   Seite auf GitHub Pages stellen (einmalig)
  python3 aniworld_watchlist.py hochladen           Online-Seite manuell aktualisieren

Benötigt nur Python 3 – keine zusätzlichen Pakete.
"""

import argparse
import getpass
import html
import http.cookiejar
import json
import os
import random
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime

BASIS = "https://aniworld.to"
WATCHLIST_URL = BASIS + "/account/watchlist"
ORDNER = os.path.dirname(os.path.abspath(__file__))
DATEN_DATEI = os.path.join(ORDNER, "anime_daten.json")
COOKIE_DATEI = os.path.join(ORDNER, ".aniworld_cookies.txt")
MD_DATEI = os.path.join(ORDNER, "anime_liste.md")
HTML_DATEI = os.path.join(ORDNER, "anime_liste.html")
# Repo-Ordner für GitHub Pages. Auf GitHub (Actions) ist das Repo selbst der Ordner.
SEITE_ORDNER = os.environ.get("AW_SEITE_ORDNER") or os.path.join(ORDNER, "seite")
AUF_GITHUB = bool(os.environ.get("GITHUB_ACTIONS"))
SYNC_WORKFLOW = "sync.yml"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36")
ZUFALL_GEDAECHTNIS = 5  # die letzten N Vorschläge werden nicht direkt wiederholt


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _ssl_kontext():
    # Die python.org-Installation auf macOS hat oft keine Zertifikate –
    # dann nehmen wir die des Systems.
    for datei in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem"):
        if os.path.exists(datei):
            return ssl.create_default_context(cafile=datei)
    return ssl.create_default_context()


class Client:
    def __init__(self):
        self.jar = http.cookiejar.MozillaCookieJar(COOKIE_DATEI)
        if os.path.exists(COOKIE_DATEI):
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=_ssl_kontext()),
        )

    def holen(self, url, daten=None):
        if daten is not None:
            daten = urllib.parse.urlencode(daten).encode()
        req = urllib.request.Request(url, data=daten, headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "de-DE,de;q=0.9",
            "Referer": BASIS + "/",
        })
        for versuch in range(3):
            try:
                with self.opener.open(req, timeout=30) as antwort:
                    text = antwort.read().decode("utf-8", errors="replace")
                    return text, antwort.geturl()
            except urllib.error.HTTPError:
                raise
            except Exception:
                if versuch == 2:
                    raise
                time.sleep(2)

    def speichern(self):
        self.jar.save(ignore_discard=True, ignore_expires=True)
        try:
            os.chmod(COOKIE_DATEI, 0o600)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Parsen
# --------------------------------------------------------------------------- #

KARTE_RE = re.compile(
    r'<a[^>]+href="(?:https://aniworld\.to)?/anime/stream/([^/"?#]+)/?"[^>]*>(.*?)</a>',
    re.S)


def _text(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def watchlist_karten(seite):
    """Liefert [(slug, titel, cover)] aus einer Watchlist-Seite (Reihenfolge bleibt)."""
    ergebnis, gesehen = [], set()
    for slug, inhalt in KARTE_RE.findall(seite):
        h3 = re.search(r"<h3[^>]*>(.*?)</h3>", inhalt, re.S)
        if not h3 or slug in gesehen:
            continue  # nur echte Anime-Karten, keine Navigationslinks
        gesehen.add(slug)
        cover = re.search(r'(?:data-src|src)="(/public/img/cover/[^"]+)"', inhalt)
        ergebnis.append((slug, _text(h3.group(1)),
                         BASIS + cover.group(1) if cover else ""))
    return ergebnis


def ist_eingeloggt(seite):
    return bool(seite) and ('href="/logout"' in seite or "/account" in seite) \
        and 'action="https://aniworld.to/login"' not in seite \
        and 'name="password"' not in seite


def weitere_seiten(seite):
    """Findet Pagination-Links der Watchlist (falls es welche gibt)."""
    links = set()
    for href in re.findall(r'href="([^"]*account/watchlist[^"]*)"', seite):
        href = html.unescape(href)
        if re.search(r"(page|seite|p)=?\d+|/\d+/?$", href):
            links.add(urllib.parse.urljoin(BASIS, href))
    return links


def anime_details(seite):
    """Genres, Jahr, Beschreibung und Staffeln von der Anime-Seite."""
    d = {}
    genres = re.findall(r'<a[^>]*itemprop="genre"[^>]*>(.*?)</a>', seite, re.S)
    d["genres"] = list(dict.fromkeys(_text(g) for g in genres if _text(g)))
    jahr = re.search(r'itemprop="startDate"[^>]*>\s*<a[^>]*>(\d{4})', seite)
    d["jahr"] = int(jahr.group(1)) if jahr else None
    ende = re.search(r'itemprop="endDate"[^>]*>\s*<a[^>]*>([^<]+)', seite)
    d["bis"] = _text(ende.group(1)) if ende else None
    beschr = re.search(r'data-full-description="([^"]*)"', seite)
    d["beschreibung"] = html.unescape(beschr.group(1)).strip() if beschr else ""
    staffeln = re.findall(r'href="/anime/stream/[^/"]+/staffel-(\d+)"', seite)
    d["staffeln"] = max(map(int, staffeln)) if staffeln else 0
    d["filme"] = '/filme"' in seite
    imdb = re.search(r'data-imdb="(tt\d+)"', seite)
    d["imdb"] = imdb.group(1) if imdb else None
    return d


# --------------------------------------------------------------------------- #
# Daten
# --------------------------------------------------------------------------- #

def daten_datei():
    """Mit Online-Seite liegen die Daten im Repo, damit der Sync auf GitHub
    und der Sync auf dem Mac dieselben Daten benutzen."""
    if not online_eingerichtet():
        return DATEN_DATEI
    im_repo = os.path.join(SEITE_ORDNER, "anime_daten.json")
    if not os.path.exists(im_repo) and os.path.exists(DATEN_DATEI):
        shutil.move(DATEN_DATEI, im_repo)  # einmaliger Umzug
    return im_repo


def daten_laden():
    if os.path.exists(daten_datei()):
        with open(daten_datei(), encoding="utf-8") as f:
            daten = json.load(f)
    else:
        daten = {}
    daten.setdefault("aktiv", {})
    daten.setdefault("entfernt", {})
    daten.setdefault("letzter_sync", None)
    daten.setdefault("zufall_verlauf", [])
    return daten


def daten_speichern(daten):
    tmp = daten_datei() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(daten, f, ensure_ascii=False, indent=2)
    os.replace(tmp, daten_datei())


def jetzt():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# Befehle
# --------------------------------------------------------------------------- #

def befehl_login(_args=None):
    print("Login bei AniWorld (deine Daten werden NICHT gespeichert,")
    print("nur das Login-Cookie in .aniworld_cookies.txt).\n")
    email = input("E-Mail: ").strip()
    passwort = getpass.getpass("Passwort (unsichtbar): ")
    client = Client()
    client.holen(BASIS + "/login")  # Session-Cookie holen
    client.holen(BASIS + "/login", {"email": email, "password": passwort,
                                     "autoLogin": "on"})
    seite, _ = client.holen(WATCHLIST_URL)
    if not ist_eingeloggt(seite):
        print("\n❌ Login hat nicht geklappt. E-Mail/Passwort prüfen.")
        print("   Alternative: python3 aniworld_watchlist.py cookie")
        return False
    client.speichern()
    print("\n✅ Eingeloggt! Jetzt kannst du 'sync' ausführen.")
    return True


def befehl_cookie(_args=None):
    print("Cookie manuell setzen (falls 'login' nicht klappt):")
    print("  1. aniworld.to im Browser öffnen und einloggen")
    print("  2. Entwicklertools (Cmd+Alt+I) → Speicher/Application → Cookies")
    print("  3. Wert von 'aniworld_session' kopieren (und ggf. 'rememberLogin')\n")
    client = Client()
    for name in ("aniworld_session", "rememberLogin"):
        wert = input(f"{name} (leer lassen zum Überspringen): ").strip()
        if wert:
            client.jar.set_cookie(http.cookiejar.Cookie(
                0, name, wert, None, False, "aniworld.to", True, False, "/",
                True, True, None, False, None, None, {}))
    seite, _ = client.holen(WATCHLIST_URL)
    if ist_eingeloggt(seite):
        client.speichern()
        print("✅ Cookie funktioniert.")
    else:
        print("❌ Mit diesem Cookie bist du nicht eingeloggt.")


def watchlist_holen(client):
    """Holt ALLE Seiten der Watchlist. Gibt None zurück, wenn nicht eingeloggt."""
    seite, _ = client.holen(WATCHLIST_URL)
    if not ist_eingeloggt(seite):
        return None
    karten = watchlist_karten(seite)
    besucht, offen = {WATCHLIST_URL}, weitere_seiten(seite)
    while offen:
        url = offen.pop()
        if url in besucht:
            continue
        besucht.add(url)
        time.sleep(0.5)
        s, _ = client.holen(url)
        bekannt = {k[0] for k in karten}
        karten += [k for k in watchlist_karten(s) if k[0] not in bekannt]
        offen |= weitere_seiten(s) - besucht
    return karten


# --------------------------------------------------------------------------- #
# Folgen-Fortschritt (welche Folgen auf AniWorld als gesehen markiert sind)
# --------------------------------------------------------------------------- #

FOLGE_RE = re.compile(r'<a\s+href="/anime/stream/[^/"]+/staffel-(\d+)/episode-(\d+)"([^>]*)>')
FOLGEN_PARALLEL = 4  # gleichzeitige Anfragen beim Prüfen


def staffel_folgen(seite, staffel):
    """(gesamt, gesehen) für eine Staffel-Seite."""
    alle, gesehen = set(), set()
    for st, ep, attribute in FOLGE_RE.findall(seite):
        if int(st) != staffel:
            continue
        alle.add(ep)
        if re.search(r'class="[^"]*\bseen\b', attribute):
            gesehen.add(ep)
    return len(alle), len(gesehen)


def folgen_aktualisieren(client, slug, eintrag, voll=False):
    """Prüft nur Staffeln, in denen sich etwas geändert haben kann:
    noch nicht komplett gesehen, noch nie geprüft, oder letzte Staffel eines
    laufenden Anime (neue Folgen). Gibt die Anzahl der Anfragen zurück."""
    cache = eintrag.setdefault("folgen", {})
    laufend = eintrag.get("bis") in (None, "Heute")
    anzahl = eintrag.get("staffeln") or 0

    def noetig(st):
        c = cache.get(str(st))
        return voll or c is None or c[1] < c[0] or (st == anzahl and laufend)

    offen = [st for st in range(1, anzahl + 1) if noetig(st)]
    anfragen = 0
    while offen:
        st = offen.pop(0)
        seite, _ = client.holen(f"{BASIS}/anime/stream/{slug}/staffel-{st}")
        anfragen += 1
        cache[str(st)] = list(staffel_folgen(seite, st))
        staffeln = [int(x) for x in re.findall(rf'href="/anime/stream/{re.escape(slug)}/staffel-(\d+)"', seite)]
        if staffeln and max(staffeln) > anzahl:  # neue Staffel erschienen
            offen += [s for s in range(anzahl + 1, max(staffeln) + 1) if s not in offen]
            anzahl = eintrag["staffeln"] = max(staffeln)
    eintrag["gesamt"] = sum(c[0] for c in cache.values())
    eintrag["gesehen"] = sum(c[1] for c in cache.values())
    eintrag["offen"] = eintrag["gesamt"] - eintrag["gesehen"]
    return anfragen


def alle_folgen_aktualisieren(client, aktiv, voll=False):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    kandidaten = [(s, e) for s, e in aktiv.items() if e.get("staffeln")]
    print(f"Prüfe gesehene Folgen ({len(kandidaten)} Anime) …")
    fertig = anfragen = fehler = 0
    with ThreadPoolExecutor(FOLGEN_PARALLEL) as pool:
        jobs = {pool.submit(folgen_aktualisieren, client, s, e, voll): e for s, e in kandidaten}
        for job in as_completed(jobs):
            fertig += 1
            try:
                anfragen += job.result()
            except Exception:
                fehler += 1
            print(f"\r   {fertig}/{len(kandidaten)}", end="", flush=True)
    print(f"\r   {anfragen} Staffeln geprüft" + (f", {fehler} Fehler" if fehler else "") + "      ")


def befehl_sync(args=None):
    if online_eingerichtet() and not AUF_GITHUB:
        holen_von_github()
    client = Client()
    print("Lade Watchlist …")
    karten = watchlist_holen(client)
    if karten is None:
        print("❌ Nicht eingeloggt (oder Login abgelaufen). Bitte zuerst 'login'.")
        if AUF_GITHUB:
            print("   Auf dem Mac 'login' ausführen und das Secret AW_COOKIES neu setzen.")
            sys.exit(1)
        return
    daten = daten_laden()
    aktiv, entfernt = daten["aktiv"], daten["entfernt"]

    # Sicherheitsnetz: leere Watchlist bei vorher vielen Einträgen ist verdächtig
    if not karten and len(aktiv) > 3:
        if not sys.stdin.isatty() or input(f"⚠️  Watchlist ist leer, lokal sind {len(aktiv)} Anime. "
                 "Wirklich alle als entfernt markieren? (j/N) ").lower() != "j":
            print("Abgebrochen, nichts geändert.")
            return

    aktuell = {slug for slug, _, _ in karten}
    neu = [k for k in karten if k[0] not in aktiv]
    weg = [slug for slug in aktiv if slug not in aktuell]

    # Entfernte (z.B. fertig geschaute) ins Archiv verschieben
    for slug in weg:
        eintrag = aktiv.pop(slug)
        eintrag["entfernt_am"] = jetzt()
        entfernt[slug] = eintrag
        print(f"  ➖ entfernt: {eintrag['titel']}")

    # Neue hinzufügen – Details nur für diese laden
    for i, (slug, titel, cover) in enumerate(neu, 1):
        if slug in entfernt:  # war schon mal drin → Details wiederverwenden
            eintrag = entfernt.pop(slug)
            eintrag.pop("entfernt_am", None)
        else:
            eintrag = {"titel": titel, "url": f"{BASIS}/anime/stream/{slug}"}
            try:
                time.sleep(0.5)
                seite, _ = client.holen(eintrag["url"])
                eintrag.update(anime_details(seite))
            except Exception as e:
                print(f"     (Details für {titel} nicht geladen: {e})")
        eintrag["titel"] = titel
        eintrag["cover"] = cover or eintrag.get("cover", "")
        eintrag["hinzugefuegt_am"] = jetzt()
        aktiv[slug] = eintrag
        print(f"  ➕ neu [{i}/{len(neu)}]: {titel}")

    # Reihenfolge wie auf AniWorld übernehmen
    reihenfolge = {slug: i for i, (slug, _, _) in enumerate(karten)}
    daten["aktiv"] = dict(sorted(aktiv.items(), key=lambda kv: reihenfolge.get(kv[0], 1e9)))
    if not getattr(args, "schnell", False):
        alle_folgen_aktualisieren(client, daten["aktiv"], getattr(args, "voll", False))
    daten["letzter_sync"] = jetzt()
    daten_speichern(daten)
    exportieren(daten)
    client.speichern()

    print(f"\n✅ Fertig: {len(neu)} neu, {len(weg)} entfernt, "
          f"{len(daten['aktiv'])} in der Watchlist.")
    print(f"   Liste: {os.path.basename(MD_DATEI)} und {os.path.basename(HTML_DATEI)}")
    hochladen()


# AniWorld führt die verfügbaren Sprachen als Genre-Tags
SPRACH_TAGS = {"Ger": "Deutsche Synchro", "GerSub": "Deutsche Untertitel",
               "EngSub": "Englische Untertitel"}
SPRACH_FILTER = {
    "dub":    ("nur mit deutscher Synchro",        lambda s: "Ger" in s),
    "sub":    ("mit deutschen Untertiteln",        lambda s: "GerSub" in s),
    "nursub": ("nur Untertitel, keine Synchro",    lambda s: "GerSub" in s and "Ger" not in s),
}


def _sprachen(eintrag):
    return [g for g in eintrag.get("genres", []) if g in SPRACH_TAGS]


def _echte_genres(eintrag):
    return [g for g in eintrag.get("genres", []) if g not in SPRACH_TAGS]


FOLGEN_FILTER = {
    "offen":      ("noch offene Folgen",        lambda e: e["offen"] > 0),
    "eine":       ("genau 1 offene Folge",      lambda e: e["offen"] == 1),
    "angefangen": ("angefangen, nicht fertig",  lambda e: e["gesehen"] > 0 and e["offen"] > 0),
    "neu":        ("noch nicht angefangen",     lambda e: e["gesehen"] == 0 and e["offen"] > 0),
    "fertig":     ("alle Folgen gesehen",       lambda e: e["offen"] == 0),
}


def _passt(eintrag, genre, sprache=None, folgen=None):
    if sprache and not SPRACH_FILTER[sprache][1](set(_sprachen(eintrag))):
        return False
    if folgen and folgen != "alle" and \
            ("offen" not in eintrag or not FOLGEN_FILTER[folgen][1](eintrag)):
        return False
    if not genre:
        return True
    g = genre.lower()
    return any(g in x.lower() for x in _echte_genres(eintrag))


def _filter_text(genre, sprache, folgen=None):
    teile = []
    if folgen and folgen != "alle":
        teile.append(FOLGEN_FILTER[folgen][0])
    if genre:
        teile.append(f"Genre: {genre}")
    if sprache:
        teile.append(SPRACH_FILTER[sprache][0])
    return ", ".join(teile)


def _zeigen(eintrag, lang=False):
    info = []
    if eintrag.get("jahr"):
        info.append(str(eintrag["jahr"]))
    if eintrag.get("staffeln"):
        info.append(f"{eintrag['staffeln']} Staffel(n)")
    if eintrag.get("filme"):
        info.append("+ Filme")
    print(f"🎬 {eintrag['titel']}" + (f"  ({', '.join(info)})" if info else ""))
    if _echte_genres(eintrag):
        print(f"   Genres: {', '.join(_echte_genres(eintrag))}")
    if _sprachen(eintrag):
        print(f"   Sprache: {', '.join(SPRACH_TAGS[s] for s in _sprachen(eintrag))}")
    if "offen" in eintrag:
        print(f"   Folgen: {eintrag['gesehen']}/{eintrag['gesamt']} gesehen"
              + (f" · {eintrag['offen']} offen" if eintrag["offen"] else " · alles gesehen ✓"))
    if lang and eintrag.get("beschreibung"):
        b = eintrag["beschreibung"].replace("\n", " ")
        print(f"   {b[:400]}{'…' if len(b) > 400 else ''}")
    print(f"   {eintrag['url']}")


def befehl_zufall(args):
    daten = daten_laden()
    if not daten["aktiv"]:
        print("Noch keine Daten – bitte zuerst 'sync' ausführen.")
        return
    genre = getattr(args, "genre", None)
    sprache = getattr(args, "sprache", None)
    folgen = getattr(args, "folgen", "offen") or "offen"
    if folgen != "alle" and not any("offen" in e for e in daten["aktiv"].values()):
        folgen = "alle"  # Fortschritt noch nie geladen
    anzahl = getattr(args, "anzahl", 1) or 1
    kandidaten = [s for s, e in daten["aktiv"].items() if _passt(e, genre, sprache, folgen)]
    filt = _filter_text(genre, sprache, folgen)
    if not kandidaten:
        print(f"Kein Anime gefunden ({filt}). Siehe 'genres'.")
        return
    if filt:
        print(f"\nAuswahl aus {len(kandidaten)} Anime ({filt})")
    # Kürzlich vorgeschlagene vermeiden, solange genug Auswahl da ist
    frisch = [s for s in kandidaten if s not in daten["zufall_verlauf"]]
    if len(frisch) >= anzahl:
        kandidaten = frisch
    auswahl = random.sample(kandidaten, min(anzahl, len(kandidaten)))
    print()
    for slug in auswahl:
        _zeigen(daten["aktiv"][slug], lang=True)
        print()
        if getattr(args, "oeffnen", False):
            webbrowser.open(daten["aktiv"][slug]["url"])
    daten["zufall_verlauf"] = (auswahl + daten["zufall_verlauf"])[:ZUFALL_GEDAECHTNIS]
    daten_speichern(daten)


def befehl_liste(args):
    daten = daten_laden()
    quelle = daten["entfernt"] if getattr(args, "entfernt", False) else daten["aktiv"]
    genre = getattr(args, "genre", None)
    sprache = getattr(args, "sprache", None)
    folgen = getattr(args, "folgen", None)
    eintraege = [e for e in quelle.values() if _passt(e, genre, sprache, folgen)]
    for i, e in enumerate(eintraege, 1):
        zusatz = f" – entfernt am {e['entfernt_am']}" if "entfernt_am" in e else ""
        genres = f"  [{', '.join(_echte_genres(e)[:3])}]" if _echte_genres(e) else ""
        dub = " 🇩🇪" if "Ger" in _sprachen(e) else ""
        stand = f"  ({e['gesehen']}/{e['gesamt']}" + (f", {e['offen']} offen)" if e["offen"] else " ✓)") \
            if "offen" in e else ""
        print(f"{i:4}. {e['titel']}{dub}{stand}{genres}{zusatz}")
    filt = _filter_text(genre, sprache, folgen)
    print(f"\n{len(eintraege)} Anime" + (f" ({filt})" if filt else "")
          + f" · letzter Sync: {daten['letzter_sync'] or 'nie'}")


def befehl_suche(args):
    daten = daten_laden()
    text = " ".join(args.text).lower()
    treffer = [e for e in list(daten["aktiv"].values()) + list(daten["entfernt"].values())
               if text in e["titel"].lower() or text in e.get("beschreibung", "").lower()]
    for e in treffer:
        _zeigen(e)
        if "entfernt_am" in e:
            print(f"   (nicht mehr in der Watchlist seit {e['entfernt_am']})")
        print()
    print(f"{len(treffer)} Treffer.")


def befehl_genres(_args=None):
    daten = daten_laden()
    zaehler = {}
    for e in daten["aktiv"].values():
        for g in e.get("genres", []):
            zaehler[g] = zaehler.get(g, 0) + 1
    print("Sprachen:")
    for tag, name in SPRACH_TAGS.items():
        print(f"  {zaehler.get(tag, 0):4}  {name}")
    print("\nGenres:")
    for g, n in sorted(zaehler.items(), key=lambda kv: (-kv[1], kv[0])):
        if g not in SPRACH_TAGS:
            print(f"  {n:4}  {g}")


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def exportieren(daten):
    zeilen = [f"# Meine AniWorld-Watchlist", "",
              f"Stand: {daten['letzter_sync']} · {len(daten['aktiv'])} Anime", "",
              "| # | Titel | Jahr | Staffeln | Folgen gesehen | Sprache | Genres |",
              "|---|---|---|---|---|---|---|"]
    for i, e in enumerate(daten["aktiv"].values(), 1):
        zeilen.append(f"| {i} | [{e['titel']}]({e['url']}) | {e.get('jahr') or ''} | "
                      f"{e.get('staffeln') or ''} | {str(e['gesehen']) + '/' + str(e['gesamt']) if 'offen' in e else ''} | {', '.join(_sprachen(e))} | {', '.join(_echte_genres(e))} |")
    if daten["entfernt"]:
        zeilen += ["", f"## Entfernt / fertig ({len(daten['entfernt'])})", ""]
        for e in sorted(daten["entfernt"].values(), key=lambda e: e["entfernt_am"], reverse=True):
            zeilen.append(f"- [{e['titel']}]({e['url']}) – {e['entfernt_am']}")
    with open(MD_DATEI, "w", encoding="utf-8") as f:
        f.write("\n".join(zeilen) + "\n")

    nutzdaten = json.dumps([
        {"t": e["titel"], "u": e["url"], "c": e.get("cover", ""), "g": _echte_genres(e), "l": _sprachen(e),
         "gs": e.get("gesehen"), "ge": e.get("gesamt"),
         "j": e.get("jahr"), "s": e.get("staffeln"), "b": e.get("beschreibung", "")[:300]}
        for e in daten["aktiv"].values()], ensure_ascii=False).replace("</", "<\\/")
    with open(HTML_DATEI, "w", encoding="utf-8") as f:
        f.write(HTML_VORLAGE.replace("__DATEN__", nutzdaten)
                .replace("__STAND__", html.escape(daten["letzter_sync"] or ""))
                .replace("__SYNC__", html.escape(sync_link())))


HTML_VORLAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Meine Watchlist</title>
<style>
:root{--bg:#f6f6f8;--fg:#1b1b1f;--muted:#6b6b76;--card:#fff;--line:#e3e3e8;--accent:#6d4aff}
@media (prefers-color-scheme:dark){:root{--bg:#121216;--fg:#ececf1;--muted:#9a9aa6;--card:#1c1c22;--line:#2c2c35;--accent:#9b84ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);padding:16px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px;align-items:center;z-index:2}
h1{font-size:18px;margin:0 auto 0 0}small{color:var(--muted)}
input,select,button{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
button{background:var(--accent);color:#fff;border:0;cursor:pointer;font-weight:600}
.knopf{padding:7px 10px;border:1px solid var(--line);border-radius:8px;color:var(--fg);text-decoration:none;font-weight:600}.knopf[href=""]{display:none}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px;padding:16px}
a.k{color:inherit;text-decoration:none;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
a.k img{width:100%;aspect-ratio:2/3;object-fit:cover;display:block;background:var(--line)}
a.k div{padding:8px}a.k b{display:block;font-size:14px}a.k span{font-size:12px;color:var(--muted);display:block}a.k .st{color:var(--fg)}
.bar{display:block;height:4px;border-radius:2px;background:var(--line);margin-top:6px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent)}
#zufall{display:none;margin:16px;padding:16px;background:var(--card);border:2px solid var(--accent);border-radius:12px;gap:16px}
#zufall img{width:120px;border-radius:8px}#zufall h2{margin:0 0 4px}#zufall p{margin:6px 0;color:var(--muted)}
</style></head><body>
<header><h1>Meine Watchlist <small id="anz"></small></h1>
<input id="q" placeholder="Suchen …"><select id="g"><option value="">Alle Genres</option></select>
<select id="l"><option value="">Alle Sprachen</option><option value="dub">Deutsche Synchro</option><option value="sub">Deutsche Untertitel</option><option value="nursub">Nur Untertitel (keine Synchro)</option></select>
<select id="f"><option value="">Alle Folgen</option><option value="offen" selected>Noch offene Folgen</option><option value="eine">Genau 1 offene Folge</option><option value="angefangen">Angefangen</option><option value="neu">Noch nicht angefangen</option><option value="fertig">Alles gesehen</option></select>
<button id="btn">🎲 Zufällig</button>
<a id="sync" class="knopf" href="__SYNC__" target="_blank" title="Öffnet GitHub: dort auf „Run workflow“ klicken, nach 1–2 Minuten diese Seite neu laden">🔄 Aktualisieren</a></header>
<section id="zufall"></section><main id="grid"></main>
<script>
const D=__DATEN__;const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
[...new Set(D.flatMap(a=>a.g))].sort().forEach(g=>$('#g').insertAdjacentHTML('beforeend',`<option>${esc(g)}</option>`));
const SF={dub:l=>l.includes('Ger'),sub:l=>l.includes('GerSub'),nursub:l=>l.includes('GerSub')&&!l.includes('Ger')};
const SN={Ger:'DE Synchro',GerSub:'DE Sub',EngSub:'EN Sub'};
const FF={offen:a=>a.ge-a.gs>0,eine:a=>a.ge-a.gs===1,angefangen:a=>a.gs>0&&a.ge-a.gs>0,neu:a=>a.gs===0&&a.ge>0,fertig:a=>a.ge-a.gs===0};
if(!D.some(a=>a.ge!=null))$('#f').value='';
const stand=a=>a.ge==null?'':a.ge-a.gs>0?`${a.gs}/${a.ge} · ${a.ge-a.gs} offen`:`${a.gs}/${a.ge} ✓`;
const balken=a=>a.ge?`<i class="bar"><i style="width:${Math.round(100*a.gs/a.ge)}%"></i></i>`:'';
const gefiltert=()=>{const q=$('#q').value.toLowerCase(),g=$('#g').value,l=$('#l').value,f=$('#f').value;return D.filter(a=>(!g||a.g.includes(g))&&(!l||SF[l](a.l))&&(!f||(a.ge!=null&&FF[f](a)))&&a.t.toLowerCase().includes(q))};
function zeigen(){const L=gefiltert();$('#anz').textContent=`· ${L.length} Anime · Stand __STAND__`;
$('#grid').innerHTML=L.map(a=>`<a class="k" href="${esc(a.u)}" target="_blank"><img loading="lazy" src="${esc(a.c)}" alt=""><div><b>${esc(a.t)}</b><span>${esc([a.j,a.l.includes('Ger')?'DE Synchro':'',a.g.slice(0,2).join(', ')].filter(Boolean).join(' · '))}</span>${a.ge!=null?`<span class="st">${esc(stand(a))}</span>${balken(a)}`:''}</div></a>`).join('')}
$('#btn').onclick=()=>{const L=gefiltert();if(!L.length)return;const a=L[Math.floor(Math.random()*L.length)];const z=$('#zufall');z.style.display='flex';
z.innerHTML=`<img src="${esc(a.c)}" alt=""><div><h2>${esc(a.t)}</h2><small>${esc([a.j,a.s?a.s+' Staffel(n)':'',stand(a),a.l.map(x=>SN[x]).join(', '),a.g.join(', ')].filter(Boolean).join(' · '))}</small><p>${esc(a.b)}…</p><a href="${esc(a.u)}" target="_blank">▶ Auf AniWorld öffnen</a></div>`};
$('#q').oninput=$('#g').onchange=$('#l').onchange=$('#f').onchange=zeigen;zeigen();
</script></body></html>
"""


# --------------------------------------------------------------------------- #
# Online stellen (GitHub Pages)
# --------------------------------------------------------------------------- #
# Nur der Ordner "seite/" ist ein Git-Repo und wird hochgeladen (Seite, Daten,
# Script) – das Login-Cookie bleibt lokal bzw. liegt als Secret bei GitHub.

def _git(*args, pruefen=True):
    return subprocess.run(["git", "-C", SEITE_ORDNER, *args], check=pruefen,
                          capture_output=True, text=True)


def _gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout.strip()


def online_eingerichtet():
    return os.path.isdir(os.path.join(SEITE_ORDNER, ".git"))


def holen_von_github():
    """Holt Änderungen, die ein Sync auf GitHub gemacht hat."""
    r = _git("pull", "-q", "--rebase", "--autostash", "origin", "main", pruefen=False)
    if r.returncode != 0:
        print(f"⚠️  Konnte Stand von GitHub nicht holen: {(r.stderr or r.stdout).strip()}")


def sync_link():
    """Link zur GitHub-Seite, auf der man den Sync startet."""
    if not online_eingerichtet():
        return ""
    url = _git("remote", "get-url", "origin", pruefen=False).stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", url)
    return f"https://github.com/{m.group(1)}/{m.group(2)}/actions/workflows/{SYNC_WORKFLOW}" if m else ""


def hochladen(_args=None):
    """Kopiert die HTML-Liste nach seite/index.html und pusht sie, falls geändert."""
    if not online_eingerichtet():
        if _args is not None:
            print("Noch nicht eingerichtet – zuerst: python3 aniworld_watchlist.py online-einrichten")
        return
    if not os.path.exists(HTML_DATEI):
        exportieren(daten_laden())
    shutil.copyfile(HTML_DATEI, os.path.join(SEITE_ORDNER, "index.html"))
    skript_im_repo = os.path.join(SEITE_ORDNER, os.path.basename(__file__))
    if os.path.abspath(__file__) != os.path.abspath(skript_im_repo):
        shutil.copyfile(__file__, skript_im_repo)  # GitHub nutzt immer die aktuelle Version
    try:
        _git("add", "-A")
        if not _git("status", "--porcelain").stdout.strip():
            print("🌐 Online-Seite ist schon aktuell.")
            return
        _git("commit", "-m", f"Watchlist aktualisiert ({jetzt()})"
             + (" – GitHub" if AUF_GITHUB else ""))
        _git("pull", "-q", "--rebase", "-X", "theirs", "origin", "main")
        _git("push", "-q", "origin", "HEAD")
        url = _git("config", "--get", "watchlist.url", pruefen=False).stdout.strip()
        print(f"🌐 Online aktualisiert (in ca. 1 Minute sichtbar){': ' + url if url else ''}")
    except subprocess.CalledProcessError as e:
        print(f"⚠️  Hochladen fehlgeschlagen: {(e.stderr or e.stdout).strip()}")
        print("   Später nochmal mit: python3 aniworld_watchlist.py hochladen")


def befehl_online_einrichten(_args=None):
    if online_eingerichtet():
        print("Ist schon eingerichtet. Zum Hochladen: python3 aniworld_watchlist.py hochladen")
        return
    if not shutil.which("gh"):
        print("Dafür brauchst du die GitHub-CLI:\n  brew install gh\n  gh auth login")
        return
    try:
        login = _gh("api", "user", "--jq", ".login")
    except subprocess.CalledProcessError:
        print("Du bist bei GitHub nicht eingeloggt. Bitte zuerst: gh auth login")
        return
    name = input("Name des Repos [anime-watchlist]: ").strip() or "anime-watchlist"
    url = f"https://{login}.github.io/{name}/"
    print(f"\nDeine Watchlist wird öffentlich unter {url} erreichbar sein")
    print("(nur Titel, Cover, Genres – keine Login-Daten).")
    if input("Fortfahren? (j/N) ").lower() != "j":
        print("Abgebrochen.")
        return
    os.makedirs(SEITE_ORDNER, exist_ok=True)
    exportieren(daten_laden())
    shutil.copyfile(HTML_DATEI, os.path.join(SEITE_ORDNER, "index.html"))
    open(os.path.join(SEITE_ORDNER, ".nojekyll"), "w").close()
    try:
        _git("init", "-q", "-b", "main")
        _git("config", "user.name", login)
        _git("config", "user.email", f"{login}@users.noreply.github.com")
        _git("config", "watchlist.url", url)
        _git("add", "-A")
        _git("commit", "-q", "-m", "Watchlist")
        _gh("auth", "setup-git")
        _gh("repo", "create", name, "--public", "--source", SEITE_ORDNER, "--push",
            "--description", "Meine Anime-Watchlist")
        _gh("api", "-X", "POST", f"repos/{login}/{name}/pages",
            "-f", "source[branch]=main", "-f", "source[path]=/")
    except subprocess.CalledProcessError as e:
        print(f"❌ Fehler: {(e.stderr or e.stdout).strip()}")
        return
    print(f"\n✅ Fertig! In 1–2 Minuten ist die Seite online:\n   {url}")
    print("   Ab jetzt wird sie bei jedem 'sync' automatisch aktualisiert.")


# --------------------------------------------------------------------------- #
# Menü / CLI
# --------------------------------------------------------------------------- #

def menue():
    class A:  # einfache Argument-Attrappe
        pass
    while True:
        d = daten_laden()
        print(f"\n=== AniWorld Watchlist · {len(d['aktiv'])} Anime · "
              f"letzter Sync: {d['letzter_sync'] or 'nie'} ===")
        print(" 1) Watchlist aktualisieren (sync)")
        print(" 2) 🎲 Zufälliger Anime")
        print(" 3) 🎲 Zufälliger Anime mit Filter (Genre / Synchro / Untertitel)")
        print(" 4) Liste anzeigen")
        print(" 5) Suchen")
        print(" 6) Genres anzeigen")
        print(" 7) Entfernte / fertige Anime")
        print(" 8) Liste im Browser öffnen")
        print(" 9) Login")
        print("10) Online-Seite " + ("hochladen" if online_eingerichtet() else "einrichten (GitHub Pages)"))
        print(" 0) Beenden")
        wahl = input("> ").strip()
        a = A()
        if wahl == "1":
            befehl_sync()
        elif wahl in ("2", "3"):
            a.genre, a.sprache = None, None
            if wahl == "3":
                a.genre = input("Genre (leer = egal): ").strip() or None
                print("Sprache:  1) deutsche Synchro  2) deutsche Untertitel"
                      "  3) nur Untertitel, keine Synchro  (leer = egal)")
                a.sprache = {"1": "dub", "2": "sub", "3": "nursub"}.get(input("> ").strip())
                print("Folgen:   1) noch offene (Standard)  2) genau 1 offene  3) angefangen"
                      "  4) noch nicht angefangen  5) alle")
                a.folgen = {"2": "eine", "3": "angefangen", "4": "neu", "5": "alle"}.get(input("> ").strip(), "offen")
            a.anzahl = 1
            a.oeffnen = False
            befehl_zufall(a)
            d = daten_laden()
            if d["aktiv"] and d["zufall_verlauf"] and d["zufall_verlauf"][0] in d["aktiv"] \
                    and input("Im Browser öffnen? (j/N) ").lower() == "j":
                webbrowser.open(d["aktiv"][d["zufall_verlauf"][0]]["url"])
        elif wahl == "4":
            a.genre, a.sprache, a.entfernt = None, None, False
            befehl_liste(a)
        elif wahl == "5":
            a.text = [input("Suchtext: ")]
            befehl_suche(a)
        elif wahl == "6":
            befehl_genres()
        elif wahl == "7":
            a.genre, a.sprache, a.entfernt = None, None, True
            befehl_liste(a)
        elif wahl == "8":
            if os.path.exists(HTML_DATEI):
                webbrowser.open("file://" + HTML_DATEI)
            else:
                print("Noch keine Liste – bitte zuerst syncen.")
        elif wahl == "10":
            hochladen(True) if online_eingerichtet() else befehl_online_einrichten()
        elif wahl == "9":
            befehl_login()
        elif wahl == "0":
            break


def main():
    p = argparse.ArgumentParser(description="AniWorld-Watchlist lokal verwalten.")
    sub = p.add_subparsers(dest="befehl")
    sub.add_parser("login", help="bei AniWorld einloggen").set_defaults(f=befehl_login)
    sub.add_parser("cookie", help="Login-Cookie manuell eintragen").set_defaults(f=befehl_cookie)
    sy = sub.add_parser("sync", help="Watchlist und gesehene Folgen abgleichen")
    sy.add_argument("--schnell", action="store_true", help="gesehene Folgen nicht prüfen")
    sy.add_argument("--voll", action="store_true", help="alle Staffeln neu prüfen")
    sy.set_defaults(f=befehl_sync)
    z = sub.add_parser("zufall", help="zufälligen Anime vorschlagen")
    z.add_argument("-g", "--genre")
    z.add_argument("-s", "--sprache", choices=SPRACH_FILTER,
                   help="dub = deutsche Synchro, sub = deutsche Untertitel, "
                        "nursub = nur Untertitel (keine Synchro)")
    z.add_argument("-f", "--folgen", choices=[*FOLGEN_FILTER, "alle"], default="offen",
                   help="offen (Standard) = noch offene Folgen, eine = genau 1 offene Folge, "
                        "angefangen, neu, fertig, alle")
    z.add_argument("-n", "--anzahl", type=int, default=1)
    z.add_argument("--oeffnen", action="store_true", help="direkt im Browser öffnen")
    z.set_defaults(f=befehl_zufall)
    l = sub.add_parser("liste", help="Liste anzeigen")
    l.add_argument("-g", "--genre")
    l.add_argument("-s", "--sprache", choices=SPRACH_FILTER)
    l.add_argument("-f", "--folgen", choices=[*FOLGEN_FILTER, "alle"])
    l.add_argument("--entfernt", action="store_true", help="entfernte/fertige zeigen")
    l.set_defaults(f=befehl_liste)
    s = sub.add_parser("suche", help="in Titeln und Beschreibungen suchen")
    s.add_argument("text", nargs="+")
    s.set_defaults(f=befehl_suche)
    sub.add_parser("genres", help="Genres und Sprachen mit Anzahl").set_defaults(f=befehl_genres)
    sub.add_parser("online-einrichten", help="Seite auf GitHub Pages stellen").set_defaults(f=befehl_online_einrichten)
    sub.add_parser("hochladen", help="Online-Seite jetzt aktualisieren").set_defaults(f=hochladen)
    args = p.parse_args()
    try:
        if not args.befehl:
            menue()
        else:
            args.f(args)
    except KeyboardInterrupt:
        print()
    except urllib.error.URLError as e:
        print(f"❌ Verbindungsproblem: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
