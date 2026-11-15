#!/usr/bin/env python3
"""Holt deine Yone-Spiele aus der Riot-API und schreibt mine.json (Winrate je Modus und Gegner).

Der API-Key kommt aus der Umgebungsvariable RIOT_API_KEY (GitHub-Secret), nie in den Code.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GAME_NAME = "yuya"
TAG_LINE = "sigma"
CLUSTER = "europe"          # Routing-Region für EUW
CHAMPION = "Yone"
PAGES = 5                   # 5 x 100 = die letzten 500 Spiele durchsuchen
MAX_NEW_PER_RUN = 400       # so viele neue Spiele pro Lauf
PAUSE = 1.3                 # Sekunden zwischen Aufrufen (Dev-Key: 100 pro 2 Minuten)

QUEUES = {
    420: "Ranked Solo/Duo",
    440: "Ranked Flex",
    400: "Normal",
    430: "Normal",
    490: "Normal",
    450: "ARAM",
    1700: "Arena",
}
MODE_ORDER = ["Ranked Solo/Duo", "Ranked Flex", "Normal", "ARAM", "Arena"]
LANES = ("TOP", "MIDDLE")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "matches.json")
OUT_FILE = os.path.join(HERE, "mine.json")


def get(url, key):
    """GET mit Wiederholung bei Rate-Limit und Serverfehlern."""
    for attempt in range(8):
        req = urllib.request.Request(url, headers={
            "X-Riot-Token": key,
            "User-Agent": "yone-hub/1.0 (persoenliche Statistik-Seite)",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After", "5")) + 1)
                continue
            if e.code in (500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            if e.code in (401, 403):
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    body = ""
                sys.exit("Riot-API meldet %d: Der API-Key wird nicht akzeptiert. Länge des Keys: %d Zeichen, "
                         "beginnt mit RGAPI-: %s. Antwort: %s" % (e.code, len(key), key.startswith("RGAPI-"), body))
            if e.code == 404:
                return None
            raise
        except urllib.error.URLError:
            time.sleep(3 * (attempt + 1))
    sys.exit("Riot-API nicht erreichbar: " + url)


def analyse_match(match, puuid):
    """Macht aus einem Match einen kleinen Eintrag, oder {"skip": 1} wenn es nicht zählt."""
    info = match.get("info", {})
    parts = info.get("participants", [])
    me = next((p for p in parts if p.get("puuid") == puuid), None)
    if not me or me.get("championName") != CHAMPION:
        return {"skip": 1}
    mode = QUEUES.get(info.get("queueId"))
    if not mode or me.get("gameEndedInEarlySurrender"):
        return {"skip": 1}
    lane = me.get("teamPosition") or ""
    opp = None
    if lane in LANES:
        for p in parts:
            if p.get("teamPosition") == lane and p.get("teamId") != me.get("teamId"):
                opp = p.get("championName")
                break
    return {"q": mode, "w": bool(me.get("win")), "lane": lane, "opp": opp,
            "t": info.get("gameStartTimestamp", 0)}


def aggregate(cache):
    """Rechnet alle gespeicherten Einträge zu mine.json um."""
    modes = {}
    for rec in cache.values():
        if rec.get("skip"):
            continue
        m = modes.setdefault(rec["q"], {"g": 0, "w": 0, "matchups": {}})
        m["g"] += 1
        m["w"] += 1 if rec["w"] else 0
        if rec.get("opp") and rec.get("lane") in LANES:
            lane = m["matchups"].setdefault(rec["lane"], {})
            o = lane.setdefault(rec["opp"], {"g": 0, "w": 0})
            o["g"] += 1
            o["w"] += 1 if rec["w"] else 0
    ordered = {k: modes[k] for k in MODE_ORDER if k in modes}
    return {
        "riotId": "%s#%s" % (GAME_NAME, TAG_LINE),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "modes": ordered,
    }


def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))


def main():
    key = os.environ.get("RIOT_API_KEY", "").strip()
    if not key:
        sys.exit("RIOT_API_KEY fehlt. Lege ihn in GitHub unter Settings > Secrets and variables > Actions an.")

    acc_url = "https://%s.api.riotgames.com/riot/account/v1/accounts/by-riot-id/%s/%s" % (
        CLUSTER, urllib.parse.quote(GAME_NAME), urllib.parse.quote(TAG_LINE))
    acc = get(acc_url, key)
    if not acc or "puuid" not in acc:
        sys.exit("Riot-ID %s#%s wurde nicht gefunden." % (GAME_NAME, TAG_LINE))
    puuid = acc["puuid"]

    cache = load_cache()
    new_ids = []
    for page in range(PAGES):
        url = "https://%s.api.riotgames.com/lol/match/v5/matches/by-puuid/%s/ids?start=%d&count=100" % (
            CLUSTER, puuid, page * 100)
        ids = get(url, key) or []
        time.sleep(PAUSE)
        new_ids += [i for i in ids if i not in cache and i not in new_ids]
        if len(ids) < 100:
            break

    print("Neue Spiele zu prüfen: %d" % len(new_ids))
    for n, mid in enumerate(new_ids[:MAX_NEW_PER_RUN], 1):
        match = get("https://%s.api.riotgames.com/lol/match/v5/matches/%s" % (CLUSTER, mid), key)
        time.sleep(PAUSE)
        cache[mid] = analyse_match(match, puuid) if match else {"skip": 1}
        if n % 25 == 0:
            save_cache(cache)

    save_cache(cache)
    out = aggregate(cache)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    total = sum(m["g"] for m in out["modes"].values())
    print("Fertig: %d Yone-Spiele in mine.json." % total)


if __name__ == "__main__":
    main()
