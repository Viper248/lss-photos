"""Photo gallery site: public galleries, FCIAC game calendar with booking requests, and /admin (user accounts)
for uploads, bookings and site settings. Run/deploy: see README.md."""
import io
import os
import re
import secrets
import sqlite3
import uuid
from calendar import Calendar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (Flask, abort, flash, g, redirect, render_template, request, send_from_directory, session,
                   url_for)
from PIL import Image, ImageOps
from werkzeug.security import check_password_hash, generate_password_hash

import fciac

SITE, TAGLINE = "LSS Photos", "Photography"
DEFAULT_SECTIONS = [  # (heading, eyebrow, divider label above the section, subsections); editable in /admin/sections
    ("Sports Galleries", "Portfolio", "", ["Baseball", "Field Hockey"]),
    ("Other Galleries", "Beyond the field", "Other Photography", []),
    ("My Adventures", "Life outside the lens", "Personal Adventures", []),
]
LINKS = {  # key: (label, placeholder, what a bare handle is appended to); shown as footer icons
    "email": ("Email", "you@example.com", "mailto:"),
    "phone": ("Phone", "(203) 555-0123", "tel:"),
    "instagram": ("Instagram", "@yourname or a full link", "https://instagram.com/"),
    "facebook": ("Facebook", "page name or a full link", "https://facebook.com/"),
    "tiktok": ("TikTok", "@yourname or a full link", "https://tiktok.com/@"),
    "youtube": ("YouTube", "@channel or a full link", "https://youtube.com/@"),
    "x": ("X (Twitter)", "@yourname or a full link", "https://x.com/"),
    "website": ("Website", "example.com", "https://"),
}
FORMATS = {"JPEG": ".jpg", "MPO": ".jpg", "PNG": ".png", "WEBP": ".webp"}
THUMB_PX = 800  # long edge; sharp in the grid on retina screens, ~100 KB each
MAX_FAILS, FAIL_WINDOW = 10, timedelta(minutes=15)  # per IP, then logins are refused for a while
MAX_REQUESTS = 5  # booking requests per IP per hour
LOCAL = ZoneInfo("America/New_York")  # game times on the CIAC site are local
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
DATA = Path(os.environ.get("DATA_DIR") or Path(__file__).parent / "data")
MEDIA, DB = DATA / "media", DATA / "gallery.db"

for d in ("full", "thumb"):
    (MEDIA / d).mkdir(parents=True, exist_ok=True)
con = sqlite3.connect(DB, isolation_level=None)
con.executescript("""
create table if not exists sections (id integer primary key, parent_id integer, name text not null,
    eyebrow text not null default '', divider text not null default '', position integer not null default 0);
create table if not exists galleries (id integer primary key, title text not null, section_id integer,
    date text not null default '', description text not null default '', cover_id integer);
create table if not exists photos (id integer primary key, gallery_id integer not null, file text not null,
    name text not null, w integer not null, h integer not null);
create index if not exists photos_by_gallery on photos (gallery_id);
create table if not exists users (id integer primary key, username text not null unique collate nocase,
    password text not null, created text not null);
create table if not exists logins (id integer primary key, at text not null, username text not null,
    ok integer not null, ip text not null, agent text not null);
create index if not exists logins_by_ip on logins (ip, at);
create table if not exists settings (key text primary key, value text not null);
create table if not exists games (id integer primary key, key text not null unique, sport_id integer not null,
    sport text not null, date text not null, time text not null, sort text not null, kind text not null,
    home text not null, away text not null, site text not null, seen text not null, gone integer not null default 0);
create index if not exists games_by_date on games (date, sort);
create table if not exists bookings (id integer primary key, game_id integer not null, token text not null unique,
    name text not null, email text not null, phone text not null, note text not null,
    status text not null default 'pending', reply text not null default '', ip text not null,
    created text not null, decided text);
create index if not exists bookings_by_game on bookings (game_id);
""")
con.execute("begin")  # the one-time setup below happens all at once or not at all
if not con.execute("select 1 from sections").fetchone():
    for pos, (name, eyebrow, divider, subs) in enumerate(DEFAULT_SECTIONS):
        sid = con.execute("insert into sections (name, eyebrow, divider, position) values (?, ?, ?, ?)",
                          (name, eyebrow, divider, pos)).lastrowid
        for sub_pos, sub in enumerate(subs):
            con.execute("insert into sections (parent_id, name, position) values (?, ?, ?)", (sid, sub, sub_pos))
if "category" in [r[1] for r in con.execute("pragma table_info(galleries)")]:  # databases from before sections
    con.execute("alter table galleries add column section_id integer")
    for key, (name, *_) in zip(("sports", "other", "adventures"), DEFAULT_SECTIONS):
        con.execute("update galleries set section_id = (select id from sections where name = ? and parent_id is null)"
                    " where category = ?", (name, key))
    con.execute("alter table galleries drop column category")
if not con.execute("select 1 from users").fetchone():  # first run: one account, from ADMIN_PASSWORD
    first_pw = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
    con.execute("insert into users (username, password, created) values ('admin', ?, ?)",
                (generate_password_hash(first_pw), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    print(f" * Created login: username admin, password {'from ADMIN_PASSWORD' if os.environ.get('ADMIN_PASSWORD') else first_pw}")
con.execute("commit")
con.close()

KEY_FILE = DATA / "secret_key"  # signs login cookies; kept so restarts don't log everyone out
if not KEY_FILE.exists():
    KEY_FILE.write_text(secrets.token_hex(32))
    KEY_FILE.chmod(0o600)

# Cover = chosen cover photo, else the first photo uploaded. top_id/section = heading, sub_id/sub = subsection.
GALLERIES = """select g.*, coalesce(s.parent_id, s.id) as top_id, coalesce(p.name, s.name) as section,
    iif(s.parent_id is null, null, s.id) as sub_id, iif(s.parent_id is null, null, s.name) as sub,
    (select count(*) from photos where gallery_id = g.id) as n,
    coalesce((select file from photos where id = g.cover_id and gallery_id = g.id),
             (select file from photos where gallery_id = g.id order by id limit 1)) as cover
    from galleries g left join sections s on s.id = g.section_id left join sections p on p.id = s.parent_id"""
NEWEST = " order by g.date desc, g.id desc"
DUMMY_HASH = generate_password_hash("x")  # unknown usernames take as long to reject as wrong passwords
# Each game's booking status: accepted if any request was accepted, else pending if any is waiting.
GAMES = """select g.*, (select case when sum(status = 'accepted') then 'accepted' when sum(status = 'pending')
    then 'pending' end from bookings where game_id = g.id) as booked from games g"""
BOOKINGS = """select b.*, g.sport, g.date, g.time, g.sort, g.home, g.away, g.site, g.gone
    from bookings b join games g on g.id = b.game_id"""

if os.environ.get("FCIAC_SYNC", "1") != "0":
    fciac.start(DB)

app = Flask(__name__)
app.config.update(SECRET_KEY=KEY_FILE.read_text().strip(), SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30))
app.jinja_env.globals.update(SITE=SITE, TAGLINE=TAGLINE, LINKS=LINKS, today=date.today)


def q(sql, *args):
    if "db" not in g:
        g.db = sqlite3.connect(DB, isolation_level=None)  # autocommit
        g.db.row_factory = sqlite3.Row
    return g.db.execute(sql, args)


@app.teardown_appcontext
def close_db(_):
    if "db" in g:
        g.db.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sections():
    """Headings in order, each with .subs (its subsections, in order)."""
    rows = [dict(r, subs=[]) for r in q("select * from sections order by position, id")]
    tops = {r["id"]: r for r in rows if r["parent_id"] is None}
    for r in rows:
        if r["parent_id"] in tops:
            tops[r["parent_id"]]["subs"].append(r)
    return list(tops.values())


def settings():
    return {r["key"]: r["value"] for r in q("select * from settings")}


def link_href(key, value):
    if key == "email":
        return "mailto:" + value
    if key == "phone":
        return "tel:" + re.sub(r"[^\d+]", "", value)
    return value if re.match(r"https?://", value, re.I) else LINKS[key][2] + value.lstrip("@")


@app.context_processor
def social_links():
    s = settings()
    return {"social": [(key, LINKS[key][0], link_href(key, s[key])) for key in LINKS if s.get(key)]}


@app.template_filter()
def nice_date(iso):
    d = date.fromisoformat(iso)
    return f"{d:%B} {d.day}, {d.year}"


@app.template_filter()
def device(agent):
    browser = next((name for key, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                           ("Chrome/", "Chrome"), ("Safari/", "Safari")) if key in agent), "Browser")
    system = next((name for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                          ("Windows", "Windows"), ("Mac OS X", "Mac"), ("Linux", "Linux")) if key in agent), "")
    return f"{browser} on {system}" if system else browser


@app.get("/")
def home():
    return render_template("home.html", galleries=q(GALLERIES + NEWEST).fetchall(), sections=sections())


@app.get("/g/<int:gid>")
def gallery(gid):
    gal = q(GALLERIES + " where g.id = ?", gid).fetchone() or abort(404)
    photos = q("select * from photos where gallery_id = ? order by id", gid).fetchall()
    shared = next((p for p in photos if p["id"] == request.args.get("photo", type=int)), None)
    return render_template("gallery.html", gal=gal, photos=photos, og_image=shared["file"] if shared else gal["cover"])


@app.get("/media/<path:path>")
def media(path):
    return send_from_directory(MEDIA, path, max_age=31536000)  # random names, never change


# --- calendar: FCIAC games with LSS bookings on top ---

def local_today():
    return datetime.now(LOCAL).date()


def month_arg(value, fallback):
    try:
        return date.fromisoformat(value + "-01")
    except (TypeError, ValueError):
        return fallback


app.jinja_env.globals.update(called_off=fciac.called_off, SCHOOLS=sorted(fciac.SCHOOLS))


@app.template_filter()
def matchup(g):
    if not g["away"]:
        return g["home"]
    return f"{g['away']} at {g['home']}" if "," not in g["away"] else f"{g['home']} meet: {g['away']}"


@app.get("/calendar")
def calendar_page():
    a, today = request.args, local_today()
    month = month_arg(a.get("m"), today.replace(day=1))
    try:
        day = date.fromisoformat(a.get("d", ""))
    except ValueError:
        day = today if today.replace(day=1) == month else month
    layers = set(a.getlist("layer")) if "f" in a else {"fciac", "lss"}  # f: the filter form was submitted
    sport, school = a.get("sport", type=int), a.get("school", "")
    weeks = Calendar(firstweekday=6).monthdatescalendar(month.year, month.month)  # Sunday first
    sql, args = GAMES + " where not g.gone and g.date between ? and ?", [weeks[0][0].isoformat(), weeks[-1][-1].isoformat()]
    if sport:
        sql, args = sql + " and g.sport_id = ?", args + [sport]
    if school:
        sql, args = sql + " and (g.home = ? or instr(', ' || g.away || ',', ', ' || ? || ',') > 0)", args + [school, school]
    games = q(sql + " order by g.date, g.sort, g.sport", *args).fetchall()
    if "fciac" not in layers:  # bookings layer alone: only the games LSS is booked for or asked about
        games = [gm for gm in games if gm["booked"]] if "lss" in layers else []
    if "lss" not in layers:  # schedule layer alone: no booking marks
        games = [dict(gm, booked=None) for gm in games]
    days = {}
    for gm in games:
        d = days.setdefault(gm["date"], {"n": 0, "accepted": 0, "pending": 0})
        d["n"] += 1
        if gm["booked"]:
            d[gm["booked"]] += 1
    s = settings()
    keep = {"f": 1, "layer": sorted(layers), "sport": sport, "school": school or None}  # carried by every link
    return render_template("calendar.html", keep=keep, weeks=weeks, month=month, day=day, this_day=today, days=days, layers=layers,
                           games=[gm for gm in games if gm["date"] == day.isoformat()], sport=sport, school=school,
                           sports=q("select distinct sport_id, sport from games order by sport").fetchall(),
                           prev=(month - timedelta(days=1)).replace(day=1), next=(month + timedelta(days=31)).replace(day=1),
                           synced=s.get("sync_at"), syncing=s.get("sync_running") == "1")


@app.route("/calendar/game/<int:game_id>", methods=["GET", "POST"])
def book_game(game_id):
    gm = q(GAMES + " where g.id = ? and not g.gone", game_id).fetchone() or abort(404)
    open_for_requests = gm["date"] >= local_today().isoformat() and not fciac.called_off(gm["time"])
    form, error = request.form, None
    if request.method == "POST":
        ip, hour_ago = client_ip(), (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        name, email, phone = form.get("name", "").strip(), form.get("email", "").strip(), form.get("phone", "").strip()
        if form.get("website"):  # hidden field only bots fill in
            abort(400, "Request not sent.")
        if not open_for_requests:
            error = "This game can't be booked any more."
        elif not name or not (email or phone):
            error = "Please give your name and an email address or phone number."
        elif email and not EMAIL.fullmatch(email):
            error = "That email address doesn't look right."
        elif q("select count(*) from bookings where ip = ? and created > ?", ip, hour_ago).fetchone()[0] >= MAX_REQUESTS:
            error = "Too many requests from here in the last hour. Please try again later."
        else:
            token = secrets.token_urlsafe(12)
            q("insert into bookings (game_id, token, name, email, phone, note, ip, created) values (?, ?, ?, ?, ?, ?, ?, ?)",
              game_id, token, name[:100], email[:200], phone[:40], form.get("note", "").strip()[:1000], ip, now())
            return redirect(url_for("booking_status", token=token))
    return render_template("book.html", gm=gm, open_for_requests=open_for_requests, form=form, error=error), 400 if error else 200


@app.route("/booking/<token>", methods=["GET", "POST"])
def booking_status(token):
    b = q(BOOKINGS + " where b.token = ?", token).fetchone() or abort(404)
    if request.method == "POST" and b["status"] in ("pending", "accepted"):
        q("update bookings set status = 'canceled', decided = ? where id = ?", now(), b["id"])
        return redirect(request.path)
    return render_template("booking.html", b=b)


# --- login ---

def client_ip():
    return request.headers.get("CF-Connecting-IP") or request.remote_addr or ""  # set by Cloudflare's tunnel


def log_in(user):
    session.clear()
    session.permanent = True
    session.update(uid=user["id"], v=user["password"][-16:])  # a password change signs out other devices


@app.before_request
def admin_guard():
    if not request.path.startswith("/admin"):
        return None
    if request.method == "POST" and request.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
        abort(403)  # CSRF (the Lax cookie also stops most of these)
    uid = session.get("uid")
    user = uid and q("select * from users where id = ?", uid).fetchone()
    g.user = user if user and secrets.compare_digest(session.get("v", ""), user["password"][-16:]) else None
    if g.user:
        g.pending = q("select count(*) from bookings where status = 'pending'").fetchone()[0]  # for the tab
        return None
    if request.endpoint == "login":
        return None
    if request.method == "GET":
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    return "Your login has expired. Log in again in another tab, then retry.", 401


@app.route("/admin/login", methods=["GET", "POST"])
def login():
    nxt = request.values.get("next", "")
    nxt = nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else url_for("admin")
    if request.method == "GET":
        return redirect(nxt) if g.user else render_template("login.html", next=nxt)
    ip, name = client_ip(), request.form.get("username", "").strip()
    since = (datetime.now(timezone.utc) - FAIL_WINDOW).isoformat(timespec="seconds")
    if q("select count(*) from logins where ip = ? and not ok and at > ?", ip, since).fetchone()[0] >= MAX_FAILS:
        return render_template("login.html", next=nxt, error="Too many failed attempts. Try again in 15 minutes."), 429
    user = q("select * from users where username = ?", name).fetchone()
    ok = check_password_hash(user["password"] if user else DUMMY_HASH, request.form.get("password", "")) and bool(user)
    q("insert into logins (at, username, ok, ip, agent) values (?, ?, ?, ?, ?)",
      now(), name[:60], ok, ip, request.headers.get("User-Agent", "")[:300])
    if not ok:
        return render_template("login.html", next=nxt, username=name, error="Wrong username or password."), 401
    log_in(user)
    return redirect(nxt)


@app.post("/admin/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# --- admin: galleries ---

@app.errorhandler(400)
def bad_request(e):
    return e.description, 400  # plain text, the upload script shows it


def gallery_fields():
    f = request.form
    sid = f.get("section_id", type=int)
    if not f.get("title", "").strip() or not q("select 1 from sections where id = ?", sid).fetchone():
        abort(400, "A gallery needs a title and a section.")
    return f["title"].strip(), sid, f.get("date", ""), f.get("description", "").strip()


def delete_files(*names):
    for name in names:
        for d in ("full", "thumb"):
            (MEDIA / d / name).unlink(missing_ok=True)


@app.get("/admin")
def admin():
    galleries = q(GALLERIES + NEWEST).fetchall()
    return render_template("admin.html", galleries=galleries, sections=sections(),
                           photos=sum(gal["n"] for gal in galleries))


@app.get("/admin/upload")
def upload_page():
    return render_template("admin_upload.html", galleries=q(GALLERIES + NEWEST).fetchall(), sections=sections(),
                           selected=request.args.get("g", type=int))


def read_photo(f):  # -> (file bytes, extension, thumbnail), or None if it isn't a JPEG, PNG or WebP
    data = f.read()
    try:
        img = Image.open(io.BytesIO(data))
        ext = FORMATS[img.format]
        img.thumbnail((THUMB_PX, THUMB_PX))  # JPEGs decode at reduced scale, fast
        return data, ext, ImageOps.exif_transpose(img)  # bake in the camera's rotation flag
    except Exception:
        return None


@app.post("/admin/upload")
def upload():  # the upload page's script sends one photo per request; without it the browser sends them all at once
    gid, bad = request.form.get("gallery_id", type=int), []
    if gid:
        q("select 1 from galleries where id = ?", gid).fetchone() or abort(404)
    for f in request.files.getlist("photo"):
        photo = read_photo(f)
        if not photo:
            bad.append(f.filename)
            continue
        data, ext, thumb = photo
        if not gid:
            gid = q("insert into galleries (title, section_id, date, description) values (?, ?, ?, ?)",
                    *gallery_fields()).lastrowid
        name = uuid.uuid4().hex + ext
        (MEDIA / "full" / name).write_bytes(data)
        thumb.save(MEDIA / "thumb" / name, quality=82, icc_profile=thumb.info.get("icc_profile"))
        q("insert into photos (gallery_id, file, name, w, h) values (?, ?, ?, ?, ?)",
          gid, name, f.filename or name, *thumb.size)
    html = "text/html" in request.headers.get("Accept", "")  # a plain form post, not the upload script
    if bad and not (html and gid):
        abort(400, f"{', '.join(bad)}: not a JPEG, PNG or WebP image.")
    if not gid:
        abort(400, "Choose some photos to upload.")
    if not html:
        return {"gallery_id": gid}
    if bad:
        flash(f"Skipped {', '.join(bad)}: not a JPEG, PNG or WebP image.")
    return redirect(url_for("admin_gallery", gid=gid))


@app.route("/admin/g/<int:gid>", methods=["GET", "POST"])
def admin_gallery(gid):
    gal = q(GALLERIES + " where g.id = ?", gid).fetchone() or abort(404)
    if request.method == "POST":
        if request.form.get("action") == "delete":
            files = [r["file"] for r in q("select file from photos where gallery_id = ?", gid)]
            q("delete from photos where gallery_id = ?", gid)
            q("delete from galleries where id = ?", gid)
            delete_files(*files)
            flash(f"Deleted {gal['title']}.")
            return redirect(url_for("admin"))
        q("update galleries set title = ?, section_id = ?, date = ?, description = ? where id = ?", *gallery_fields(), gid)
        flash("Saved.")
        return redirect(request.path)  # relative: stays on https behind a tunnel/proxy
    photos = q("select * from photos where gallery_id = ? order by id", gid).fetchall()
    return render_template("admin_gallery.html", gal=gal, photos=photos, sections=sections())


@app.post("/admin/photo/<int:pid>")
def admin_photo(pid):
    p = q("select * from photos where id = ?", pid).fetchone() or abort(404)
    if request.form.get("action") == "delete":
        q("delete from photos where id = ?", pid)
        delete_files(p["file"])
    else:
        q("update galleries set cover_id = ? where id = ?", pid, p["gallery_id"])
    return redirect(url_for("admin_gallery", gid=p["gallery_id"]))


# --- admin: sections (headings and subsections) ---

@app.route("/admin/sections", methods=["GET", "POST"])
def admin_sections():
    if request.method == "GET":
        return render_template("admin_sections.html", sections=sections())
    f, action = request.form, request.form.get("action")
    name = f.get("name", "").strip()
    if action == "add":
        parent = f.get("parent_id", type=int)
        if not name or (parent and not q("select 1 from sections where id = ? and parent_id is null", parent).fetchone()):
            flash("Give the new section a name.")
        else:
            q("insert into sections (parent_id, name, eyebrow, divider, position) values (?, ?, ?, ?,"
              " (select coalesce(max(position), -1) + 1 from sections where parent_id is ?))",
              parent, name, f.get("eyebrow", "").strip(), f.get("divider", "").strip(), parent)
            flash(f"Added {name}.")
        return redirect(request.path)
    s = q("select * from sections where id = ?", f.get("id", type=int)).fetchone() or abort(404)
    if action == "save":
        if not name:
            flash("A section needs a name.")
        else:
            q("update sections set name = ?, eyebrow = ?, divider = ? where id = ?",
              name, f.get("eyebrow", "").strip(), f.get("divider", "").strip(), s["id"])
            flash(f"Saved {name}.")
    elif action in ("up", "down"):
        ids = [r["id"] for r in q("select id from sections where parent_id is ? order by position, id", s["parent_id"])]
        i = ids.index(s["id"])
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            for pos, sid in enumerate(ids):
                q("update sections set position = ? where id = ?", pos, sid)
    elif action == "delete":
        if q("select 1 from sections where parent_id = ?", s["id"]).fetchone():
            flash(f"{s['name']} still has subsections. Delete those first.")
        elif q("select 1 from galleries where section_id = ?", s["id"]).fetchone():
            flash(f"{s['name']} still has galleries. Move them to another section first.")
        else:
            q("delete from sections where id = ?", s["id"])
            flash(f"Deleted {s['name']}.")
    return redirect(request.path)


# --- admin: contact and social links ---

@app.route("/admin/links", methods=["GET", "POST"])
def admin_links():
    if request.method == "GET":
        return render_template("admin_links.html", values=settings())
    values, errors = {k: request.form.get(k, "").strip() for k in LINKS}, []
    for key, v in values.items():
        label = LINKS[key][0]
        if not v:
            continue
        if key == "email" and not EMAIL.fullmatch(v):
            errors.append(f"{label}: that doesn't look like an email address.")
        elif key == "phone" and len(re.sub(r"\D", "", v)) < 7:
            errors.append(f"{label}: enter the full phone number.")
        elif key not in ("email", "phone") and (" " in v or ("://" in v and not re.match(r"https?://", v, re.I))):
            errors.append(f"{label}: enter a handle like @name, or a link starting with https://")
    if errors:
        for e in errors:
            flash(e)
        return render_template("admin_links.html", values=values), 400
    for key, v in values.items():
        q("insert into settings (key, value) values (?, ?) on conflict (key) do update set value = excluded.value", key, v)
    flash("Links saved. They show in the footer of every page.")
    return redirect(request.path)


# --- admin: accounts and login history ---

def password_problem(pw, confirm):
    if len(pw) < 8:
        return "Passwords need at least 8 characters."
    if pw != confirm:
        return "The two passwords don't match."
    return None


@app.route("/admin/accounts", methods=["GET", "POST"])
def admin_accounts():
    if request.method == "GET":
        users = q("select u.*, (select max(at) from logins where ok and username = u.username collate nocase) as last"
                  " from users u order by u.username collate nocase").fetchall()
        return render_template("admin_accounts.html", users=users)
    f, action = request.form, request.form.get("action")
    pw, confirm = f.get("password", ""), f.get("confirm", "")
    if action == "add":
        name = f.get("username", "").strip()
        problem = password_problem(pw, confirm)
        if not re.fullmatch(r"[\w.@-]{2,40}", name):
            flash("Usernames are 2 to 40 letters, numbers, dots, dashes or underscores (no spaces).")
        elif problem:
            flash(problem)
        elif q("select 1 from users where username = ?", name).fetchone():
            flash(f"There is already an account called {name}.")
        else:
            q("insert into users (username, password, created) values (?, ?, ?)", name, generate_password_hash(pw), now())
            flash(f"Added {name}. Give them their password; they can change it after logging in.")
    elif action == "mine":
        if not check_password_hash(g.user["password"], f.get("current", "")):
            flash("Your current password is wrong.")
        elif problem := password_problem(pw, confirm):
            flash(problem)
        else:
            q("update users set password = ? where id = ?", generate_password_hash(pw), g.user["id"])
            log_in(q("select * from users where id = ?", g.user["id"]).fetchone())
            flash("Password changed. Other devices logged in as you have been signed out.")
    else:
        user = q("select * from users where id = ?", f.get("id", type=int)).fetchone() or abort(404)
        if user["id"] == g.user["id"]:
            flash("Use Change my password for your own account.")
        elif action == "delete":
            q("delete from users where id = ?", user["id"])
            flash(f"Deleted {user['username']}. Their login history is kept.")
        elif problem := password_problem(pw, confirm):
            flash(problem)
        else:
            q("update users set password = ? where id = ?", generate_password_hash(pw), user["id"])
            flash(f"New password set for {user['username']}. They have been signed out everywhere.")
    return redirect(request.path)


@app.get("/admin/logins")
def admin_logins():
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    return render_template("admin_logins.html", logins=q("select * from logins order by id desc limit 300").fetchall(),
                           failed=q("select count(*) from logins where not ok and at > ?", since).fetchone()[0])



# --- admin: booking requests and the FCIAC sync ---

@app.route("/admin/bookings", methods=["GET", "POST"])
def admin_bookings():
    if request.method == "POST":
        if request.form.get("action") == "sync":
            fciac.sync_now()
            flash("Checking the FCIAC schedule now. It can take a few minutes; reload this page to see when it's done.")
            return redirect(request.path)
        b = q("select * from bookings where id = ?", request.form.get("id", type=int)).fetchone() or abort(404)
        status = {"accept": "accepted", "decline": "declined", "reopen": "pending"}.get(request.form.get("action")) or abort(400)
        q("update bookings set status = ?, reply = ?, decided = ? where id = ?",
          status, request.form.get("reply", b["reply"]).strip()[:1000], now() if status != "pending" else None, b["id"])
        flash(f"{b['name']}'s request is now {status}. They see this on their status page.")
        return redirect(request.path)
    today = local_today().isoformat()
    pending = q(BOOKINGS + " where b.status = 'pending' order by g.date, g.sort").fetchall()
    accepted = q(BOOKINGS + " where b.status = 'accepted' and g.date >= ? order by g.date, g.sort", today).fetchall()
    booked_days = {}
    for b in accepted:
        booked_days.setdefault(b["date"], []).append(b)
    past = q(BOOKINGS + " where b.status in ('declined', 'canceled') or (b.status = 'accepted' and g.date < ?)"
             " order by g.date desc limit 50", today).fetchall()
    s = settings()
    return render_template("admin_bookings.html", pending=pending, accepted=accepted, past=past, booked_days=booked_days,
                           sync=s, games=q("select count(*) from games where not gone").fetchone()[0])


if __name__ == "__main__":
    app.run()
