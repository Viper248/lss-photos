"""FCIAC varsity schedules, read from the CIAC master schedule pages that fciac.net/schedules links to.

There is no feed, so this reads the HTML table (date, time, type, home, away, site) for each in-season sport
and keeps the games that involve an FCIAC school. The CIAC site is sometimes slow (minutes per sport), so
syncing runs in a background thread and page views only ever read the database.
"""
import hashlib
import html
import json
from collections import Counter
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import date, datetime, timezone

MASTER = "https://ciac.fpsports.org/MasterSchedule.aspx?TeamLevelID=5&SportGenderListID={}"  # 5 = varsity
SPORTS = {  # CIAC SportGenderListID: (name, season); ids from the links on fciac.net/schedules
    4: ("Football", "fall"), 32: ("Girls Volleyball", "fall"), 7: ("Boys Soccer", "fall"),
    8: ("Girls Soccer", "fall"), 3: ("Field Hockey", "fall"), 10: ("Girls Swimming", "fall"),
    1: ("Boys Cross Country", "fall"), 2: ("Girls Cross Country", "fall"),
    34: ("Boys Basketball", "winter"), 35: ("Girls Basketball", "winter"), 37: ("Boys Ice Hockey", "winter"),
    38: ("Girls Ice Hockey", "winter"), 11: ("Wrestling", "winter"), 9: ("Boys Swimming", "winter"),
    36: ("Gymnastics", "winter"), 39: ("Boys Indoor Track", "winter"), 40: ("Girls Indoor Track", "winter"),
    43: ("Baseball", "spring"), 48: ("Softball", "spring"), 44: ("Boys Lacrosse", "spring"),
    45: ("Girls Lacrosse", "spring"), 49: ("Boys Tennis", "spring"), 50: ("Girls Tennis", "spring"),
    5: ("Boys Golf", "spring"), 30: ("Girls Golf", "spring"), 46: ("Boys Track", "spring"),
    47: ("Girls Track", "spring"), 31: ("Boys Volleyball", "spring"),
}
SEASON_MONTHS = {"fall": {8, 9, 10, 11}, "winter": {11, 12, 1, 2, 3}, "spring": {3, 4, 5, 6}}
SCHOOLS = {  # as the CIAC tables spell them
    "Bridgeport Central", "Danbury", "Darien", "Fairfield Ludlowe", "Fairfield Warde", "Greenwich",
    "Brien McMahon", "New Canaan", "Norwalk", "Ridgefield", "St. Joseph", "Stamford", "Staples", "Trumbull",
    "Westhill", "Wilton",
}
EVERY = 6 * 3600  # seconds between syncs
ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def fetch(sport_id):
    req = urllib.request.Request(MASTER.format(sport_id), headers={"User-Agent": "lss.photos schedule sync"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read().decode("utf-8", "replace")


def parse(page):
    """Rows of the master schedule table: (date ISO, time, type, home, away, site)."""
    for row in ROW.findall(page):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in CELL.findall(row)]
        if len(cells) == 6 and re.fullmatch(r"\d\d/\d\d/\d{4}", cells[0]):
            m, d, y = cells[0].split("/")
            yield (f"{y}-{m}-{d}", *cells[1:])


def sort_time(t):
    """'4:30 PM' -> '16:30' so games sort by time; anything else (TBA) sorts last."""
    try:
        return datetime.strptime(t, "%I:%M %p").strftime("%H:%M")
    except ValueError:
        return "99:99"


def called_off(t):
    """The time column says Canceled / Postponed / Rainout instead of a time."""
    return t != "TBA" and sort_time(t) == "99:99"


def in_season(today=None):
    month = (today or date.today()).month
    return [sid for sid, (_, season) in SPORTS.items() if month in SEASON_MONTHS[season]]


def sync_sport(db, sport_id, page):
    """Replace one sport's games with what the page lists. Games that vanish are deleted, unless someone
    asked to book them: those are kept and marked gone so the request isn't lost."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    name = SPORTS[sport_id][0]
    kept, seen = [], Counter()
    db.execute("begin")
    for day, t, kind, home, away, site in parse(page):
        t = "TBA" if t == "12:01 AM" else t  # the CIAC site's placeholder for "time not set"
        teams = {home, *(a.strip() for a in away.split(","))}
        if not teams & SCHOOLS:
            continue
        seen[day, home, away] += 1  # doubleheaders: same teams, same day
        ident = f"{sport_id}|{day}|{home}|{away}|{seen[day, home, away]}"  # not time or site: those get changed
        key = hashlib.sha1(ident.encode()).hexdigest()[:16]
        db.execute("""insert into games (key, sport_id, sport, date, time, sort, kind, home, away, site, seen, gone)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) on conflict (key) do update set time = excluded.time,
            sort = excluded.sort, kind = excluded.kind, site = excluded.site, seen = excluded.seen, gone = 0""",
                   (key, sport_id, name, day, t, sort_time(t), kind, home, away, site, now))
        kept.append(key)
    stale = "sport_id = ? and key not in (select value from json_each(?))"
    args = (sport_id, json.dumps(kept))
    db.execute(f"update games set gone = 1 where {stale} and id in (select game_id from bookings)", args)
    db.execute(f"delete from games where {stale} and id not in (select game_id from bookings)", args)
    db.execute("commit")
    return len(kept)


def set_status(db, **values):
    for k, v in values.items():
        db.execute("insert into settings (key, value) values (?, ?) on conflict (key) do update set value = excluded.value",
                   (k, str(v)))


def sync_all(db_path, sports=None):
    db = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    errors, total = [], 0
    set_status(db, sync_running=1)
    for sid in sports if sports is not None else in_season():
        try:
            total += sync_sport(db, sid, fetch(sid))
        except Exception as e:  # keep the old games for this sport; try the others
            if db.in_transaction:
                db.execute("rollback")
            errors.append(f"{SPORTS[sid][0]}: {e}")
    set_status(db, sync_running=0, sync_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
               sync_games=total, sync_error="; ".join(errors))
    db.close()


_wake = threading.Event()


def start(db_path):
    """Background sync: now if the last one is older than EVERY, then every EVERY; sync_now() wakes it early."""
    def loop():
        while True:
            db = sqlite3.connect(db_path, timeout=30)
            db.execute("update settings set value = '0' where key = 'sync_running'")  # left over from a restart
            db.commit()
            last = db.execute("select value from settings where key = 'sync_at'").fetchone()
            db.close()
            age = time.time() - datetime.fromisoformat(last[0]).timestamp() if last else EVERY
            if age >= EVERY or _wake.is_set():
                _wake.clear()
                sync_all(db_path)
                continue
            _wake.wait(EVERY - age)
    threading.Thread(target=loop, daemon=True, name="fciac-sync").start()


def sync_now():
    _wake.set()
