"""Smoke test: .venv\\Scripts\\python test_app.py  (uses a throwaway data dir)"""
import base64
import io
import os
import sqlite3
import tempfile

os.environ.update(DATA_DIR=tempfile.mkdtemp(), ADMIN_PASSWORD="pw")
from PIL import Image  # noqa: E402

import app as site  # noqa: E402

c = site.app.test_client()
auth = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}


def jpeg(w, h, orientation):
    exif = Image.Exif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "orange").save(buf, "JPEG", exif=exif)
    return buf.getvalue()


assert c.get("/admin").status_code == 401
assert c.get("/admin", headers={"Authorization": "Basic " + base64.b64encode(b"x:nope").decode()}).status_code == 401

# stored landscape, EXIF says "rotate 90": the thumbnail must come out portrait, the original untouched
raw = jpeg(3000, 2000, orientation=6)
r = c.post("/admin/upload", headers=auth, data={"title": "Game", "category": "sports", "date": "2026-09-14",
                                                 "photo": (io.BytesIO(raw), "IMG_1.jpg")})
assert r.status_code == 200, r.data
gid = r.json["gallery_id"]
r = c.post("/admin/upload", headers=auth, data={"gallery_id": gid, "photo": (io.BytesIO(raw), "IMG_2.jpg")})
assert r.json["gallery_id"] == gid
assert c.post("/admin/upload", headers=auth, data={"gallery_id": gid, "photo": (io.BytesIO(b"no"), "a.heic")}).status_code == 400
assert c.post("/admin/upload", headers=auth, data={"title": "", "category": "sports", "photo": (io.BytesIO(raw), "x.jpg")}).status_code == 400

pid, file = sqlite3.connect(site.DB).execute("select id, file from photos order by id").fetchone()
assert c.get(f"/media/full/{file}").data == raw
assert Image.open(io.BytesIO(c.get(f"/media/thumb/{file}").data)).size == (533, 800)
assert c.get("/media/../gallery.db").status_code == 404

assert c.get("/admin", headers=auth).status_code == 200
assert f'value="{gid}" selected' in c.get(f"/admin?g={gid}", headers=auth).get_data(as_text=True)
assert c.get(f"/admin/g/{gid}", headers=auth).status_code == 200
home = c.get("/").get_data(as_text=True)
assert "Game" in home and "September 14, 2026" in home
page = c.get(f"/g/{gid}?photo={pid}").get_data(as_text=True)
assert f'href="/media/full/{file}"' in page and 'data-name="IMG_1.jpg"' in page and f"thumb/{file}" in page

r = c.post(f"/admin/g/{gid}", headers=auth, data={"title": "Game!", "category": "other", "date": "2026-09-14"})
assert r.headers["Location"] == f"/admin/g/{gid}"  # relative, so https survives a proxy

# CSRF guard, then real delete
assert c.post(f"/admin/photo/{pid}", headers={**auth, "Sec-Fetch-Site": "cross-site"}, data={"action": "delete"}).status_code == 403
c.post(f"/admin/photo/{pid}", headers=auth, data={"action": "delete"})
assert not (site.MEDIA / "full" / file).exists() and not (site.MEDIA / "thumb" / file).exists()
c.post(f"/admin/g/{gid}", headers=auth, data={"action": "delete"})
assert c.get(f"/g/{gid}").status_code == 404 and not any((site.MEDIA / "full").iterdir())
print("ok")
