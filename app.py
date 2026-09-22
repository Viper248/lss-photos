"""Photo gallery site: public galleries + password-protected /admin for uploads. Run/deploy: see README.md."""
import io
import os
import secrets
import sqlite3
import uuid
from datetime import date
from pathlib import Path

from flask import Flask, abort, g, redirect, render_template, request, send_from_directory, url_for
from PIL import Image, ImageOps

SITE, TAGLINE = "L&S Shots", "Sports Photography"
CATEGORIES = {  # key: (eyebrow, heading, divider label above the section)
    "sports": ("Portfolio", "Sports Galleries", ""),
    "other": ("Beyond the field", "Other Galleries", "Other Photography"),
    "adventures": ("Life outside the lens", "My Adventures", "Personal Adventures"),
}
FORMATS = {"JPEG": ".jpg", "MPO": ".jpg", "PNG": ".png", "WEBP": ".webp"}
THUMB_PX = 800  # long edge; sharp in the grid on retina screens, ~100 KB each
DATA = Path(os.environ.get("DATA_DIR") or Path(__file__).parent / "data")
MEDIA, DB = DATA / "media", DATA / "gallery.db"

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(9)
    print(f" * ADMIN_PASSWORD not set, using {ADMIN_PASSWORD} for this run")

for d in ("full", "thumb"):
    (MEDIA / d).mkdir(parents=True, exist_ok=True)
con = sqlite3.connect(DB)
con.executescript("""
create table if not exists galleries (id integer primary key, title text not null, category text not null,
    date text not null default '', description text not null default '', cover_id integer);
create table if not exists photos (id integer primary key, gallery_id integer not null, file text not null,
    name text not null, w integer not null, h integer not null);
create index if not exists photos_by_gallery on photos (gallery_id);
""")
con.close()

# Cover = chosen cover photo, else the first photo uploaded.
GALLERIES = """select g.*, (select count(*) from photos where gallery_id = g.id) as n,
    coalesce((select file from photos where id = g.cover_id and gallery_id = g.id),
             (select file from photos where gallery_id = g.id order by id limit 1)) as cover
    from galleries g"""

app = Flask(__name__)
app.jinja_env.globals.update(SITE=SITE, TAGLINE=TAGLINE, CATEGORIES=CATEGORIES, today=date.today)


def q(sql, *args):
    if "db" not in g:
        g.db = sqlite3.connect(DB, isolation_level=None)  # autocommit
        g.db.row_factory = sqlite3.Row
    return g.db.execute(sql, args)


@app.teardown_appcontext
def close_db(_):
    if "db" in g:
        g.db.close()


@app.template_filter()
def nice_date(iso):
    d = date.fromisoformat(iso)
    return f"{d:%B} {d.day}, {d.year}"


@app.get("/")
def home():
    return render_template("home.html", galleries=q(GALLERIES + " order by date desc, id desc").fetchall())


@app.get("/g/<int:gid>")
def gallery(gid):
    gal = q(GALLERIES + " where id = ?", gid).fetchone() or abort(404)
    photos = q("select * from photos where gallery_id = ? order by id", gid).fetchall()
    shared = next((p for p in photos if p["id"] == request.args.get("photo", type=int)), None)
    return render_template("gallery.html", gal=gal, photos=photos, og_image=shared["file"] if shared else gal["cover"])


@app.get("/media/<path:path>")
def media(path):
    return send_from_directory(MEDIA, path, max_age=31536000)  # random names, never change


# --- admin ---

@app.before_request
def admin_guard():
    if not request.path.startswith("/admin"):
        return None
    auth = request.authorization
    if not (auth and auth.password and secrets.compare_digest(auth.password.encode(), ADMIN_PASSWORD.encode())):
        return "Login required", 401, {"WWW-Authenticate": 'Basic realm="admin"'}
    if request.method == "POST" and request.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
        abort(403)  # CSRF: browsers resend Basic auth on cross-site posts too
    return None


@app.errorhandler(400)
def bad_request(e):
    return e.description, 400  # plain text, the upload script shows it


def gallery_fields():
    f = request.form
    if not f.get("title", "").strip() or f.get("category") not in CATEGORIES:
        abort(400, "A gallery needs a title and a category.")
    return f["title"].strip(), f["category"], f.get("date", ""), f.get("description", "").strip()


def delete_files(*names):
    for name in names:
        for d in ("full", "thumb"):
            (MEDIA / d / name).unlink(missing_ok=True)


@app.get("/admin")
def admin():
    return render_template("admin.html", galleries=q(GALLERIES + " order by date desc, id desc").fetchall(),
                           selected=request.args.get("g", type=int))


@app.post("/admin/upload")
def upload():  # one photo per request; the admin page loops over the selected files
    f = request.files["photo"]
    data = f.read()
    try:
        img = Image.open(io.BytesIO(data))
        ext = FORMATS[img.format]
        img.thumbnail((THUMB_PX, THUMB_PX))  # JPEGs decode at reduced scale, fast
        thumb = ImageOps.exif_transpose(img)  # bake in the camera's rotation flag
    except Exception:
        abort(400, f"{f.filename}: not a JPEG, PNG or WebP image.")
    gid = request.form.get("gallery_id", type=int)
    if gid:
        q("select 1 from galleries where id = ?", gid).fetchone() or abort(404)
    else:
        gid = q("insert into galleries (title, category, date, description) values (?, ?, ?, ?)",
                *gallery_fields()).lastrowid
    name = uuid.uuid4().hex + ext
    (MEDIA / "full" / name).write_bytes(data)
    thumb.save(MEDIA / "thumb" / name, quality=82, icc_profile=thumb.info.get("icc_profile"))
    q("insert into photos (gallery_id, file, name, w, h) values (?, ?, ?, ?, ?)",
      gid, name, f.filename or name, *thumb.size)
    return {"gallery_id": gid}


@app.route("/admin/g/<int:gid>", methods=["GET", "POST"])
def admin_gallery(gid):
    gal = q(GALLERIES + " where id = ?", gid).fetchone() or abort(404)
    if request.method == "POST":
        if request.form.get("action") == "delete":
            files = [r["file"] for r in q("select file from photos where gallery_id = ?", gid)]
            q("delete from photos where gallery_id = ?", gid)
            q("delete from galleries where id = ?", gid)
            delete_files(*files)
            return redirect(url_for("admin"))
        q("update galleries set title = ?, category = ?, date = ?, description = ? where id = ?", *gallery_fields(), gid)
        return redirect(request.path)  # relative: stays on https behind a tunnel/proxy
    photos = q("select * from photos where gallery_id = ? order by id", gid).fetchall()
    return render_template("admin_gallery.html", gal=gal, photos=photos)


@app.post("/admin/photo/<int:pid>")
def admin_photo(pid):
    p = q("select * from photos where id = ?", pid).fetchone() or abort(404)
    if request.form.get("action") == "delete":
        q("delete from photos where id = ?", pid)
        delete_files(p["file"])
    else:
        q("update galleries set cover_id = ? where id = ?", pid, p["gallery_id"])
    return redirect(url_for("admin_gallery", gid=p["gallery_id"]))


if __name__ == "__main__":
    app.run()
