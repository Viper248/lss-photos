"""Smoke test: .venv\\Scripts\\python test_app.py  (uses a throwaway data dir)"""
import atexit
import io
import os
import shutil
import sqlite3
import tempfile
from datetime import timedelta

DATA_DIR = tempfile.mkdtemp()
atexit.register(shutil.rmtree, DATA_DIR, ignore_errors=True)
os.environ.update(DATA_DIR=DATA_DIR, ADMIN_PASSWORD="pw", FCIAC_SYNC="0")  # no network in tests
from PIL import Image  # noqa: E402

import app as site  # noqa: E402
import fciac  # noqa: E402

c = site.app.test_client()
db = sqlite3.connect(site.DB)


def jpeg(w, h, orientation):
    exif = Image.Exif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "orange").save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def section(name):
    return db.execute("select id from sections where name = ?", (name,)).fetchone()[0]


def text(r):
    return r.get_data(as_text=True)


# --- logins: first account comes from ADMIN_PASSWORD; attempts are logged; bad ones rate-limited per IP
r = c.get("/admin/g/1?x=1")
assert r.status_code == 302 and r.headers["Location"] == "/admin/login?next=/admin/g/1?x%3D1"
assert c.post("/admin/upload").status_code == 401  # upload script gets text, not a login page
assert c.post("/admin/login", data={"username": "admin", "password": "nope"}).status_code == 401
assert c.post("/admin/login", data={"username": "ghost", "password": "pw"}).status_code == 401
r = c.post("/admin/login", data={"username": "ADMIN", "password": "pw", "next": "//evil.com"},
           headers={"User-Agent": "Mozilla/5.0 (iPhone) Safari/604.1"})
assert r.status_code == 302 and r.headers["Location"] == "/admin"  # usernames ignore case; no open redirect
logins = text(c.get("/admin/logins"))
assert "ghost" in logins and "iPhone" in logins and "2 failed in the last 24 hours" in logins
assert db.execute("select count(*) from logins where ok").fetchone()[0] == 1

attacker = site.app.test_client()
attacker.environ_base["REMOTE_ADDR"] = "10.0.0.9"
for _ in range(10):
    attacker.post("/admin/login", data={"username": "admin", "password": "guess"})
assert attacker.post("/admin/login", data={"username": "admin", "password": "pw"}).status_code == 429
assert attacker.post("/admin/login", data={"username": "admin", "password": "pw"},
                     headers={"CF-Connecting-IP": "10.0.0.10"}).status_code == 302  # other visitors unaffected

# --- galleries in sections and subsections
sports, baseball = section("Sports Galleries"), section("Baseball")
raw = jpeg(3000, 2000, orientation=6)  # stored landscape, EXIF says "rotate 90": thumbnail must come out portrait
r = c.post("/admin/upload", data={"title": "Game", "section_id": baseball, "date": "2026-09-14",
                                  "photo": (io.BytesIO(raw), "IMG_1.jpg")})
assert r.status_code == 200, r.data
gid = r.json["gallery_id"]
r = c.post("/admin/upload", data={"gallery_id": gid, "photo": (io.BytesIO(raw), "IMG_2.jpg")})
assert r.json["gallery_id"] == gid
assert c.post("/admin/upload", data={"gallery_id": gid, "photo": (io.BytesIO(b"no"), "a.heic")}).status_code == 400
assert c.post("/admin/upload", data={"title": "", "section_id": sports, "photo": (io.BytesIO(raw), "x.jpg")}).status_code == 400
assert c.post("/admin/upload", data={"title": "X", "section_id": 999, "photo": (io.BytesIO(raw), "x.jpg")}).status_code == 400
other = c.post("/admin/upload", data={"title": "Scrimmage", "section_id": sports, "date": "2026-09-01",
                                      "photo": (io.BytesIO(raw), "a.jpg")}).json["gallery_id"]
r = c.post("/admin/upload", headers={"Accept": "text/html"}, data={"gallery_id": other, "photo": [  # no script: all at once
    (io.BytesIO(raw), "b.jpg"), (io.BytesIO(b"no"), "c.heic"), (io.BytesIO(raw), "d.jpg")]})
assert r.status_code == 302 and r.headers["Location"].endswith(f"/admin/g/{other}")
assert db.execute("select count(*) from photos where gallery_id = ?", (other,)).fetchone()[0] == 3
assert "Skipped c.heic" in text(c.get(f"/admin/g/{other}"))

pid, file = db.execute("select id, file from photos order by id").fetchone()
assert c.get(f"/media/full/{file}").data == raw
assert Image.open(io.BytesIO(c.get(f"/media/thumb/{file}").data)).size == (533, 800)
assert c.get("/media/../gallery.db").status_code == 404

hub = text(c.get("/admin"))
assert "Game" in hub and "Scrimmage" in hub and "Baseball" in hub and "3 photos" in hub
assert f'value="{gid}" selected' in text(c.get(f"/admin/upload?g={gid}"))
assert c.get(f"/admin/g/{gid}").status_code == 200
home = text(c.get("/"))
assert "Game" in home and "September 14, 2026" in home
assert f'data-sub="{baseball}">Baseball</button>' in home and "Field Hockey</button>" not in home  # only used subsections
page = text(c.get(f"/g/{gid}?photo={pid}"))
assert f'href="/media/full/{file}"' in page and 'data-name="IMG_1.jpg"' in page and f"thumb/{file}" in page
assert "Sports Galleries</a> › Baseball" in page

r = c.post(f"/admin/g/{gid}", data={"title": "Game!", "section_id": section("Other Galleries"), "date": "2026-09-14"})
assert r.headers["Location"] == f"/admin/g/{gid}"  # relative, so https survives a proxy

# --- sections: add, rename, reorder, delete (only when empty)
c.post("/admin/sections", data={"action": "add", "name": "Portraits", "eyebrow": "People"})
portraits = section("Portraits")
c.post("/admin/sections", data={"action": "add", "name": "Lacrosse", "parent_id": sports})
c.post("/admin/sections", data={"action": "save", "id": portraits, "name": "Senior Portraits", "eyebrow": "People"})
c.post("/admin/sections", data={"action": "up", "id": portraits})
order = [r[0] for r in db.execute("select name from sections where parent_id is null order by position")]
assert order == ["Sports Galleries", "Other Galleries", "Senior Portraits", "My Adventures"], order
home = text(c.get("/"))
assert home.index("Senior Portraits") < home.index("My Adventures")
assert [r[0] for r in db.execute("select name from sections where parent_id = ? order by position", (sports,))] == \
    ["Baseball", "Field Hockey", "Lacrosse"]
def delete_section(sid):
    return text(c.post("/admin/sections", data={"action": "delete", "id": sid}, follow_redirects=True))


assert "still has subsections" in delete_section(sports)
assert "still has galleries" in delete_section(section("Other Galleries"))
assert "Deleted Lacrosse" in delete_section(section("Lacrosse"))
assert not db.execute("select 1 from sections where name = 'Lacrosse'").fetchone()

# --- contact and social links
r = c.post("/admin/links", data={"email": "not-an-email", "instagram": "@lss"})
assert r.status_code == 400 and "look like an email" in text(r)
assert c.post("/admin/links", data={"website": "javascript://x"}).status_code == 400
c.post("/admin/links", data={"email": "hi@lss.photos", "instagram": "@lssphotos", "website": "https://lss.photos/about"})
home = text(c.get("/"))
assert 'href="mailto:hi@lss.photos"' in home and 'href="https://instagram.com/lssphotos"' in home
assert 'href="https://lss.photos/about"' in home and "#s-tiktok" not in home

# --- accounts: add, personal passwords, reset signs the user out, can't delete yourself
assert "at least 8" in text(c.post("/admin/accounts", data={"action": "add", "username": "sam", "password": "short",
                                                            "confirm": "short"}, follow_redirects=True))
c.post("/admin/accounts", data={"action": "add", "username": "sam", "password": "sampass1", "confirm": "sampass1"})
sam_id = db.execute("select id from users where username = 'sam'").fetchone()[0]
sam = site.app.test_client()
assert sam.post("/admin/login", data={"username": "sam", "password": "sampass1"}).status_code == 302
assert "Signed in as <b>sam</b>" in text(sam.get("/admin"))
c.post("/admin/accounts", data={"action": "reset", "id": sam_id, "password": "newpass12", "confirm": "newpass12"})
assert sam.get("/admin").status_code == 302  # old session no longer valid
assert sam.post("/admin/login", data={"username": "sam", "password": "newpass12"}).status_code == 302
sam.post("/admin/accounts", data={"action": "mine", "current": "wrong", "password": "x" * 8, "confirm": "x" * 8})
sam.post("/admin/accounts", data={"action": "mine", "current": "newpass12", "password": "mine1234", "confirm": "mine1234"})
assert sam.get("/admin").status_code == 200  # changing your own password keeps you logged in
admin_id = db.execute("select id from users where username = 'admin'").fetchone()[0]
sam.post("/admin/accounts", data={"action": "delete", "id": sam_id})
assert db.execute("select 1 from users where id = ?", (sam_id,)).fetchone()  # can't delete yourself
sam.post("/admin/accounts", data={"action": "delete", "id": admin_id})
assert c.get("/admin").status_code == 302  # deleted account is signed out
sam.post("/admin/logout")
assert sam.get("/admin").status_code == 302

# --- game calendar: sync keeps Norwalk's teams (every level), booking requests go pending -> accepted
HEADS = ["Date", "Time", "Team Level", "Type", "Status", "Title", "Home/Away", "Opponent", "Result", "Score", "Details",
         "Site", "Transportation"]
MENU = {"Field Hockey - Girls": 3, "Football - Boys": 4, "Soccer - Boys": 7, "Fall Golf - Boys": 5, "Swimming - Girls": 10}


def school(*blocks):
    """A CIAC all-teams schedule page. blocks: (sport, team, games); a game is (day, time, level, home/away,
    opponents, site) plus optional status. Extra opponents become rowspan rows, as on the real page."""
    menu = "".join(f'<a class=" dsToolbarMenuLink " href="/DashboardTeamSchedule.aspx?SportGenderListID={i}&amp;'
                   f'TeamLevelID=0&amp;SchoolID=112&amp;Status=0&amp;SeasonID=-1"><i class="fa x"></i>{n}</a>'
                   for n, i in MENU.items())
    out = [f"<html><h1>CIAC</h1>{menu}"]
    for sport, team, games in blocks:
        rows = "<tr>" + "".join(f"<th>{h}</th>" for h in HEADS) + "</tr>"
        for d, t, level, where, opp, site_, *status in games:
            opp = opp.split(", ")
            cells = [f"{d:%a}".upper() + f" {d.month}/{d.day}", t, level, "League", (status or ["Normal"])[0], "",
                     where, opp[0], "", "", "", site_, ""]
            rows += "<tr>" + "".join(f'<td rowspan="{len(opp)}">{c}</td>' for c in cells) + "</tr>"
            rows += "".join(f"<tr><td>{o}</td><td></td></tr>" for o in opp[1:])
        out.append(f"<div class='TeamBlocl'><div class='HeaderText'><h1>{sport}</h1></div><div class='TeamHeaderWrapper'>"
                   f"<img/><div class='TeamHeaderTextWrapper'><span class='TeamHeaderSchool'>{team}</span><br/></div></div>"
                   f"<table>{rows}</table></div>")
    return "".join(out)


today = site.local_today()
soon, later, gone_day = today + timedelta(days=2), today + timedelta(days=3), today - timedelta(days=5)
coop = ("Swimming - Girls", "Norwalk/Brien McMahon",
        [(soon, "3:30 PM", "Varsity", "Away", "Wilton, Staples", "Wilton - Pool", "Postponed")])
norwalk = school(
    ("Field Hockey - Girls", "Norwalk", [
        (soon, "4:00 PM", "Varsity", "Home", "Greenwich", "Norwalk High School - Stadium"),
        (soon, "12:01 AM", "Junior Varsity", "Away", "Staples", "Staples - Field"),
        (later, "10:00 AM", "Varsity", "Home", "Ridgefield", "DH game 1"),
        (later, "1:00 PM", "Varsity", "Home", "Ridgefield", "DH game 2"),
        (gone_day, "4:00 PM", "Varsity", "Away", "Stamford &amp; Co", "past")]),
    ("Football - Boys", "Norwalk", [(soon, "5:30 PM", "Freshman", "Away", "Trumbull", "Trumbull - Cork Field")]),
    ("Fall Golf - Boys", "Norwalk", [(soon, "3:00 PM", "Varsity", "Home", "Darien", "Oak Hills Golf Course")]),
    coop)
mcmahon = school(("Field Hockey - Girls", "Brien McMahon", [(soon, "5:00 PM", "Varsity", "Home", "Trumbull", "McMahon alone")]),
                 coop)  # the co-op is on both schools' pages: read once
assert fciac.sync(db, [norwalk, mcmahon]) == 8
games = {r[0]: r for r in db.execute("select site, time, sort, sport, level, sport_id from games")}
assert "McMahon alone" not in games and games["Wilton - Pool"][1] == "Postponed"
assert games["Staples - Field"][1] == "TBA" and games["Norwalk High School - Stadium"][2] == "16:00"
assert games["past"][3] == "Field Hockey" and fciac.called_off("Postponed") and not fciac.called_off("TBA")
assert games["Oak Hills Golf Course"][3:] == ("Boys Golf", "Varsity", 5) and games["Trumbull - Cork Field"][4] == "Freshman"
assert games["past"][0] == "past" and db.execute("select date from games where site = 'past'").fetchone()[0] == gone_day.isoformat()
game_id = db.execute("select id from games where site like 'Norwalk High%'").fetchone()[0]
past_id = db.execute("select id from games where site = 'past'").fetchone()[0]

cal = text(c.get(f"/calendar?m={soon:%Y-%m}&d={soon}"))
assert "Greenwich at Norwalk" in cal and "Norwalk at Staples" in cal and f'/calendar/game/{game_id}"' in cal
assert "Meet: Norwalk/Brien McMahon, Wilton, Staples" in cal and "Book LSS Photos" in cal and "McMahon alone" not in cal
assert "Football · Freshman" in cal and "Field Hockey · Junior Varsity" in cal and "Darien at Norwalk" in cal
assert "Greenwich at Norwalk" not in text(c.get(f"/calendar?m={soon:%Y-%m}&d={soon}&f=1&layer=fciac&sport=7"))
freshmen = text(c.get(f"/calendar?m={soon:%Y-%m}&d={soon}&f=1&layer=fciac&level=Freshman"))
assert "Norwalk at Trumbull" in freshmen and "Greenwich at Norwalk" not in freshmen
assert "Greenwich at Norwalk" not in text(c.get(f"/calendar?m={soon:%Y-%m}&d={soon}&f=1&layer=lss"))  # nothing booked yet

visitor = site.app.test_client()
visitor.environ_base["REMOTE_ADDR"] = "10.1.1.1"
form = {"name": "Pat Parent", "email": "pat@example.com", "phone": "", "note": "#12, goalie"}
assert visitor.post(f"/calendar/game/{game_id}", data={**form, "email": ""}).status_code == 400  # needs a way to reply
assert visitor.post(f"/calendar/game/{game_id}", data={**form, "website": "spam"}).status_code == 400  # honeypot
assert "already been played" in text(visitor.post(f"/calendar/game/{past_id}", data=form))
r = visitor.post(f"/calendar/game/{game_id}", data=form)
assert r.status_code == 302 and r.headers["Location"].startswith("/booking/")
status_url = r.headers["Location"]
assert "Waiting for a reply" in text(visitor.get(status_url)) and "#12, goalie" in text(visitor.get(status_url))
assert "Requested, waiting for a reply" in text(c.get(f"/calendar?m={soon:%Y-%m}&d={soon}"))
booking_id = db.execute("select id from bookings").fetchone()[0]
for _ in range(site.MAX_REQUESTS):
    visitor.post(f"/calendar/game/{game_id}", data=form)
assert "Too many requests" in text(visitor.post(f"/calendar/game/{game_id}", data=form))
db.execute("delete from bookings where id != ?", (booking_id,))
db.commit()

sam.post("/admin/login", data={"username": "sam", "password": "mine1234"})  # admin was deleted above
admin_page = text(sam.get("/admin/bookings"))
assert "Pat Parent" in admin_page and "mailto:pat@example.com" in admin_page and '<span class="count"' in admin_page
sam.post("/admin/bookings", data={"id": booking_id, "action": "accept", "reply": "See you on the home sideline"})
status = text(visitor.get(status_url))
assert "Accepted: LSS Photos will be there" in status and "See you on the home sideline" in status
same_day = db.execute("select id from games where site = 'Staples - Field'").fetchone()[0]
warning = text(visitor.get(f"/calendar/game/{same_day}"))
assert "already booked for another game that day" in warning and "Greenwich at Norwalk" in warning
assert "already booked for another game" not in text(visitor.get(f"/calendar/game/{game_id}"))  # not about itself
only_lss = text(c.get(f"/calendar?m={soon:%Y-%m}&d={soon}&f=1&layer=lss"))
assert "Greenwich at Norwalk" in only_lss and "LSS Photos will be there" in only_lss and "Norwalk at Staples" not in only_lss
assert "will be there" not in text(c.get(f"/calendar?m={soon:%Y-%m}&d={soon}&f=1&layer=fciac"))  # schedule layer alone

# --- private events: requested like a game, never shown publicly beyond "private booking"
guest = site.app.test_client()
guest.environ_base["REMOTE_ADDR"] = "10.2.2.2"
event = {"what": "Sweet 16 party", "date": later.isoformat(), "time": "18:30", "place": "12 Main St", "name": "Jo Guest",
         "email": "", "phone": "203-555-0100", "note": "Three hours"}
assert f'value="{later}"' in text(guest.get(f"/calendar/private?d={later}"))
assert "what the event is" in text(guest.post("/calendar/private", data={**event, "what": ""}))
assert "between today and two years" in text(guest.post("/calendar/private", data={**event, "date": gone_day.isoformat()}))
assert "start time" in text(guest.post("/calendar/private", data={**event, "time": "25:99"}))
r = guest.post("/calendar/private", data=event)
assert r.status_code == 302 and r.headers["Location"].startswith("/booking/")
private_url = r.headers["Location"]
status = text(guest.get(private_url))
assert "Private event" in status and "Sweet 16 party" in status and "6:30 PM" in status and "12 Main St" in status
assert "Sweet 16" in text(sam.get("/admin/bookings"))
private_booking = db.execute("select b.id from bookings b join games g on g.id = b.game_id where g.private").fetchone()[0]
sam.post("/admin/bookings", data={"id": private_booking, "action": "accept"})
cal = text(c.get(f"/calendar?m={later:%Y-%m}&d={later}"))
assert "has a private booking this day" in cal and "Sweet 16" not in cal and "Private event" not in cal
assert "Private event" not in text(c.get(f"/calendar?m={later:%Y-%m}&d={later}"))  # not in the sport filter either
dh1 = db.execute("select id from games where site = 'DH game 1'").fetchone()[0]
warning = text(visitor.get(f"/calendar/game/{dh1}"))
assert "a private event" in warning and "Sweet 16" not in warning
private_game = db.execute("select game_id from bookings where id = ?", (private_booking,)).fetchone()[0]
assert c.get(f"/calendar/game/{private_game}").status_code == 404

# --- busy days: no requests, games or private, and the calendar says so
r = sam.post("/admin/bookings", data={"action": "busy", "date": soon.isoformat(), "until": "", "note": "Wedding"},
             follow_redirects=True)
assert "as busy" in text(r) and "Wedding" in text(r)
cal = text(c.get(f"/calendar?m={soon:%Y-%m}&d={soon}"))
assert "isn't available this day" in cal and "Greenwich at Norwalk" in cal and f'/calendar/game/{same_day}"' not in cal
assert "Wedding" not in cal  # the note is only for the admin
assert "isn't available on" in text(visitor.get(f"/calendar/game/{same_day}"))
assert visitor.post(f"/calendar/game/{same_day}", data=form).status_code == 400
assert "isn't available on" in text(guest.post("/calendar/private", data={**event, "date": soon.isoformat()}))
assert "You have a booking this day" in text(sam.get("/admin/bookings"))  # busy, but Greenwich was already accepted
r = sam.post("/admin/bookings", data={"action": "busy", "date": (today + timedelta(days=10)).isoformat(),
                                      "until": (today + timedelta(days=12)).isoformat(), "note": ""})
assert db.execute("select count(*) from busy_days").fetchone()[0] == 4
assert "on or after the first" in text(sam.post("/admin/bookings", data={
    "action": "busy", "date": later.isoformat(), "until": soon.isoformat()}, follow_redirects=True))
sam.post("/admin/bookings", data={"action": "free", "date": soon.isoformat()})
assert "isn't available" not in text(visitor.get(f"/calendar/game/{same_day}"))

# --- schedule changes: vanished games deleted, or kept and marked gone if booked; private events untouched
fciac.sync(db, [school(("Field Hockey - Girls", "Norwalk", [(later, "10:00 AM", "Varsity", "Home", "Ridgefield", "DH game 1")]))])
assert db.execute("select gone from games where id = ?", (game_id,)).fetchone()[0] == 1  # booked: kept, marked gone
assert not db.execute("select 1 from games where site = 'Staples - Field'").fetchone()  # unbooked: deleted
assert db.execute("select gone from games where id = ?", (private_game,)).fetchone()[0] == 0
assert "no longer on the schedule" in text(visitor.get(status_url))
visitor.post(status_url)
assert "You canceled this request" in text(visitor.get(status_url))

fciac.fetch = lambda sid: (_ for _ in ()).throw(OSError("timed out")) if sid == 19 else norwalk
fciac.sync_all(site.DB)
assert "timed out" in dict(db.execute("select key, value from settings"))["sync_error"]
assert not db.execute("select 1 from games where site = 'Staples - Field'").fetchone()  # a failed sync changes nothing
assert "timed out" in text(sam.get("/admin/bookings"))
fciac.fetch = lambda sid: "This IP Address has been blocked." if sid == 112 else mcmahon
fciac.sync_all(site.DB)
assert "blocked" in dict(db.execute("select key, value from settings"))["sync_error"]
fciac.fetch = lambda sid: {112: norwalk, 19: mcmahon}[sid]
fciac.sync_all(site.DB)
assert dict(db.execute("select key, value from settings"))["sync_error"] == ""
assert db.execute("select 1 from games where site = 'Staples - Field'").fetchone()

# --- CSRF guard, then real deletes
sam.post("/admin/login", data={"username": "sam", "password": "mine1234"})
assert sam.post(f"/admin/photo/{pid}", headers={"Sec-Fetch-Site": "cross-site"}, data={"action": "delete"}).status_code == 403
sam.post(f"/admin/photo/{pid}", data={"action": "delete"})
assert not (site.MEDIA / "full" / file).exists() and not (site.MEDIA / "thumb" / file).exists()
sam.post(f"/admin/g/{gid}", data={"action": "delete"})
sam.post(f"/admin/g/{other}", data={"action": "delete"})
assert c.get(f"/g/{gid}").status_code == 404 and not any((site.MEDIA / "full").iterdir())
print("ok")
