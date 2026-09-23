# LSS Photos photo galleries (lss.photos)

Public photo galleries (thumbnail grid, full-size viewer with download and share links) plus an
`/admin` with its own accounts. Flask + SQLite + Pillow, served by waitress.
Photos, the database and the login-cookie key live in `data/`, which is not in git. Back that folder up.

**Admin** (log in at `/admin`):
- **Galleries**: every gallery, grouped by section, with edit / add photos / view / delete
- **Upload**: new gallery or more photos for an existing one
- **Sections**: add, rename, reorder and delete the home-page headings (Sports Galleries, My Adventures…)
  and their subsections (Baseball, Field Hockey…). Visitors filter a heading's galleries by subsection.
  A section can only be deleted once it's empty.
- **Links**: email, phone, Instagram, Facebook, TikTok, YouTube, X, website; shown as icons in the footer
- **Accounts**: each person gets their own username and password; anyone logged in can add accounts,
  set someone's password (signs them out) or change their own
- **Login history**: every login attempt with time, username, IP and device. After 10 failed attempts
  from one IP in 15 minutes, that IP can't log in for a while.

## Run locally

```sh
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt           # macOS/Linux: .venv/bin/pip
ADMIN_PASSWORD=pick-one .venv/Scripts/python app.py     # http://localhost:5000, admin at /admin (username admin)
.venv/Scripts/python test_app.py                        # smoke test, prints "ok"
```

## Deploy: ZimaBlade + https://lss.photos

`docker-compose.yml` runs two containers on the ZimaBlade: the site (waitress, port 8080) and a
Cloudflare Tunnel that serves it at `https://lss.photos` without opening any router ports.
Part A needs the owner's Cloudflare account. Part B is done over SSH (by the owner, or a Claude Code
instance that can SSH into the Blade); it needs two values from the owner: the admin password and the
tunnel token.

### A. Cloudflare (owner, once)

1. **Buy `lss.photos` on Cloudflare** (dashboard → Domain Registration → Register Domains). Cloudflare
   sells `.photos` at cost and sets up DNS itself. Bought it elsewhere? Add it to Cloudflare on the Free
   plan and change the registrar's nameservers to the two Cloudflare gives you.
2. **Create a tunnel:** Zero Trust dashboard (one.dash.cloudflare.com) → Networks → Tunnels
   (newer UI: Connectors) → Create a tunnel → Cloudflared → name it `lss-photos`. Copy the token: the
   long string after `--token` in the install command it shows. Don't run that command; the compose
   file runs cloudflared.
3. **Route the domain to the site:** in the tunnel, add a public hostname (newer UI: published
   application route): hostname `lss.photos`, path empty, service type `HTTP`, URL `gallery:8080`.
   Optionally add `www.lss.photos` the same way.

### B. ZimaBlade (over SSH)

1. **SSH in:** `ssh casaos@<blade-ip>`. CasaOS has SSH on by default with user `casaos`; change its
   default password. ZimaOS: Settings → Developer Mode → SSH on. Commands below may need `sudo`.
2. **Copy the code** onto the SATA data drive, not the 32 GB eMMC (drives are under `/media/`).
   From a clone of https://github.com/viper248/lss-photos (private: that machine needs GitHub access):
   ```sh
   git archive HEAD | ssh casaos@<blade-ip> "mkdir -p /media/<drive>/lss-photos && tar -x -C /media/<drive>/lss-photos"
   ```
3. **Create `.env`** in that folder on the Blade, then `chmod 600 .env`:
   ```
   ADMIN_PASSWORD=<first admin password from the owner>
   TUNNEL_TOKEN=<token from step A2>
   ```
4. **Start** (in that folder): `docker compose up -d`.
   Domain not ready yet? `docker compose up -d gallery` runs just the site at `http://<blade-ip>:8080`.
   No `docker compose` on the box? Paste `docker-compose.yml` into the App Store's "Install a Custom App"
   instead, with `.` replaced by the folder's full path and the `${...}` values filled in.
5. **Check:**
   - `docker compose ps` shows both containers running; `curl -sI localhost:8080` returns 200
   - `docker compose logs tunnel` shows "Registered tunnel connection"
   - https://lss.photos loads, and https://lss.photos/admin asks for a login: username `admin`, password `ADMIN_PASSWORD`.
     That creates the first account only. Afterwards passwords are managed in Admin → Accounts,
     and changing `ADMIN_PASSWORD` does nothing.

**Update to the latest code:** repeat B2 (it leaves `data/` and `.env` alone), then `docker compose restart gallery`.

### Good to know

- **Upgrading from the version without accounts:** the first start after updating creates the `admin`
  account from `ADMIN_PASSWORD` and puts existing galleries under their old headings. Move sports
  galleries into Baseball / Field Hockey from each gallery's Edit page.
- **Locked out of every account?** In the site's folder on the Blade, run
  `docker compose exec gallery python -c "import sqlite3; c = sqlite3.connect('data/gallery.db'); c.execute('delete from users'); c.commit()"`
  then `docker compose restart gallery`. `admin` / `ADMIN_PASSWORD` works again (galleries are untouched).
- **Back up `data/`** (photos + `gallery.db`). That folder is the whole site's content.
- **502 from Cloudflare** means the `gallery` container isn't running, or the hostname's service isn't `HTTP` → `gallery:8080`.
- **Deleted photos** can stay in Cloudflare's cache for a while. To remove one right away: Caching → Purge Cache → its URL.
- Cloudflare's free plan limits each upload request to 100 MB. That's fine: the admin page uploads one photo per request.
