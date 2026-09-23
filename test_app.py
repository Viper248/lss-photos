"""Smoke test: .venv\\Scripts\\python test_app.py  (uses a throwaway data dir)"""
import io
import os
import sqlite3
import tempfile

os.environ.update(DATA_DIR=tempfile.mkdtemp(), ADMIN_PASSWORD="pw")
from PIL import Image  # noqa: E402

import app as site  # noqa: E402

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

# --- CSRF guard, then real deletes
sam.post("/admin/login", data={"username": "sam", "password": "mine1234"})
assert sam.post(f"/admin/photo/{pid}", headers={"Sec-Fetch-Site": "cross-site"}, data={"action": "delete"}).status_code == 403
sam.post(f"/admin/photo/{pid}", data={"action": "delete"})
assert not (site.MEDIA / "full" / file).exists() and not (site.MEDIA / "thumb" / file).exists()
sam.post(f"/admin/g/{gid}", data={"action": "delete"})
sam.post(f"/admin/g/{other}", data={"action": "delete"})
assert c.get(f"/g/{gid}").status_code == 404 and not any((site.MEDIA / "full").iterdir())
print("ok")
