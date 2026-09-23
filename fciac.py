"""Every Norwalk High game, every sport and level (varsity, JV, freshman...), from the CIAC site that fciac.net/schedules
links to.

There is no feed, so this reads Norwalk's all-teams schedule page: one block per sport, each with a table of dates,
times, levels, opponents and sites. Brien McMahon's page is read too, for co-op teams it hosts. The page covers the
whole school year, so there is no season list to keep up to date. Syncing runs in a background thread and page views
only ever read the database.
"""
import hashlib
import html
import json
import re
import sqlite3
import threading
import time
import urllib.request
from collections import Counter
from datetime import date, datetime, timezone

PAGE = "https://ciac.fpsports.org/DashboardTeamSchedule.aspx?SportGenderListID=0&TeamLevelID=0&SchoolID={}&Status=0"
SCHOOLS = [112, 19]  # CIAC SchoolIDs: Norwalk, then Brien McMahon (only its co-op teams with Norwalk are kept)
TEAM = "Norwalk"  # kept: teams named Norwalk, or a co-op with Norwalk in it ("Norwalk/Brien McMahon")
ONE_GENDER = {"Football", "Baseball", "Softball", "Field Hockey", "Gymnastics"}  # shown without "Boys"/"Girls"
EVERY = 6 * 3600  # seconds between syncs
HEADER = "<div class='HeaderText'><h1>"  # starts each team's block
BLOCK = re.compile(HEADER + r"([^<]+)</h1>.*?class='TeamHeaderSchool'>([^<]+)<(.*?)(?=" + HEADER + "|$)", re.S)
MENU = re.compile(r"SportGenderListID=(\d+)&amp;[^\"]*\">(?:<i [^>]*></i>)?([^<]+ - [^<]+)</a>")  # sport menu: id, name
ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)


def fetch(school_id):
    req = urllib.request.Request(PAGE.format(school_id), headers={"User-Agent": "lss.photos schedule sync"})
    with urllib.request.urlopen(req, timeout=300) as r:  # the site blocks requests without a User-Agent
        return r.read().decode("utf-8", "replace")


def text(cell):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()


def sport_name(ciac):
    """'Soccer - Boys' -> 'Boys Soccer', 'Fall Golf - Boys' -> 'Boys Golf', 'Football - Boys' -> 'Football'."""
    sport, _, who = ciac.partition(" - ")
    sport = re.sub(r"^(Fall|Winter|Spring) ", "", sport)
    return f"{who} {sport}" if who in ("Boys", "Girls", "Unified") and sport not in ONE_GENDER else sport


def game_date(day, today):
    """'TUE 9/15' -> the nearest year (to today) where 9/15 is a Tuesday. The page leaves the year out."""
    dow, md = day.split()
    m, d = map(int, md.split("/"))
    options = []
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            options.append(date(y, m, d))
        except ValueError:  # Feb 29
            pass
    fits = [o for o in options if o.strftime("%a").upper() == dow[:3]] or options
    return min(fits, key=lambda o: abs(o - today)).isoformat()


def sort_time(t):
    """'4:30 PM' -> '16:30' so games sort by time; anything else (TBA) sorts last."""
    try:
        return datetime.strptime(t, "%I:%M %p").strftime("%H:%M")
    except ValueError:
        return "99:99"


def called_off(t):
    """The time column says Canceled / Postponed / Rainout instead of a time."""
    return t != "TBA" and sort_time(t) == "99:99"


def parse(page, done=None, today=None):
    """Games Norwalk plays in: dicts with sport_id, sport, level, date, time, kind, home, away, site.
    Several opponents (a cross country or golf meet) come as one game with extra opponent rows under it.
    done: (sport, team) blocks already read from another school's page, skipped and added to."""
    today, done = today or date.today(), set() if done is None else done
    if "has been blocked" in page:
        raise ValueError("the CIAC site has blocked this server's IP address")
    ids = {name: int(sid) for sid, name in MENU.findall(page)}
    blocks = BLOCK.findall(page)
    if "TeamHeaderSchool" not in page or not blocks:
        raise ValueError("no team schedules found; the CIAC page layout may have changed")
    for ciac_sport, team, body in blocks:
        team = team.strip()
        if TEAM not in (t.strip() for t in team.split("/")) or (ciac_sport, team) in done:
            continue
        done.add((ciac_sport, team))
        heads, game = [], None
        for row in ROW.findall(body):
            if "<th" in row:
                heads = [text(c) for c in CELL.findall(row)]
                continue
            cells = [text(c) for c in CELL.findall(row)]
            if len(cells) < len(heads):  # another opponent in the meet above
                if game and cells and cells[0]:
                    game["opponents"].append(cells[0])
                continue
            if game:
                yield finish(game, team)
            c = dict(zip(heads, cells))
            if not re.fullmatch(r"[A-Z]{3} \d\d?/\d\d?", c.get("Date", "")):
                game = None
                continue
            status, t = c.get("Status", ""), c.get("Time", "") or "TBA"
            game = {"sport_id": ids.get(ciac_sport.strip(), 0), "sport": sport_name(ciac_sport.strip()),
                    "level": c.get("Team Level", ""), "date": game_date(c["Date"], today),
                    "time": status if status not in ("", "Normal") else "TBA" if t == "12:01 AM" else t,
                    "kind": c.get("Title") or c.get("Type", ""), "where": c.get("Home/Away", ""),
                    "opponents": [c["Opponent"]] if c.get("Opponent") else [], "site": c.get("Site", "")}
        if game:
            yield finish(game, team)


def finish(game, team):
    """Home and away the way the calendar shows them: 'away at home', 'home meet: a, b', or both blank-home
    for someone else's meet or a neutral site."""
    opp, where = game.pop("opponents"), game.pop("where")
    if not opp:
        game["home"], game["away"] = (game["kind"] or team), ""
    elif where == "Home":
        game["home"], game["away"] = team, ", ".join(opp)
    elif where == "Away" and len(opp) == 1:
        game["home"], game["away"] = opp[0], team
    else:
        game["home"], game["away"] = "", ", ".join([team, *opp])
    return game


def sync(db, pages):
    """Replace the schedule with what the pages list. Games that vanish are deleted, unless someone asked to book
    them: those are kept and marked gone so the request isn't lost. Private events are left alone."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    done = set()
    games = [gm for page in pages for gm in parse(page, done)]
    kept, seen = [], Counter()
    db.execute("begin")
    for gm in games:
        who = (gm["sport_id"], gm["level"], gm["date"], gm["home"], gm["away"])
        seen[who] += 1  # doubleheaders: same teams, same day
        level = "" if gm["level"] == "Varsity" else f"{gm['level']}|"  # varsity keys match the old varsity-only sync
        ident = f"{gm['sport_id']}|{level}{gm['date']}|{gm['home']}|{gm['away']}|{seen[who]}"  # not time or site
        key = hashlib.sha1(ident.encode()).hexdigest()[:16]
        db.execute("""insert into games (key, sport_id, sport, level, date, time, sort, kind, home, away, site, seen, gone)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) on conflict (key) do update set sport = excluded.sport,
            time = excluded.time, sort = excluded.sort, kind = excluded.kind, site = excluded.site, seen = excluded.seen,
            gone = 0""", (key, gm["sport_id"], gm["sport"], gm["level"], gm["date"], gm["time"], sort_time(gm["time"]),
                          gm["kind"], gm["home"], gm["away"], gm["site"], now))
        kept.append(key)
    stale = "not private and key not in (select value from json_each(?))"
    args = (json.dumps(kept),)
    db.execute(f"update games set gone = 1 where {stale} and id in (select game_id from bookings)", args)
    db.execute(f"delete from games where {stale} and id not in (select game_id from bookings)", args)
    db.execute("commit")
    return len(kept)


def set_status(db, **values):
    for k, v in values.items():
        db.execute("insert into settings (key, value) values (?, ?) on conflict (key) do update set value = excluded.value",
                   (k, str(v)))


def sync_all(db_path):
    db = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    set_status(db, sync_running=1)
    try:  # all or nothing: if a page can't be read, keep the old schedule
        total, error = sync(db, [fetch(sid) for sid in SCHOOLS]), ""
    except Exception as e:
        if db.in_transaction:
            db.execute("rollback")
        total, error = db.execute("select count(*) from games where not private and not gone").fetchone()[0], str(e)
    set_status(db, sync_running=0, sync_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
               sync_games=total, sync_error=error)
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
