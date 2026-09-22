# LSS Photos photo galleries (lss.photos)

Public photo galleries (thumbnail grid, full-size viewer with download and share links) plus a
password-protected `/admin` for uploading. Flask + SQLite + Pillow, served by waitress.
Photos and the database live in `data/`, which is not in git. Back that folder up.

## Run locally

```sh
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt           # macOS/Linux: .venv/bin/pip
ADMIN_PASSWORD=pick-one .venv/Scripts/python app.py     # http://localhost:5000, admin at /admin (any username)
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
   ADMIN_PASSWORD=<admin password from the owner>
   TUNNEL_TOKEN=<token from step A2>
   ```
4. **Start** (in that folder): `docker compose up -d`.
   Domain not ready yet? `docker compose up -d gallery` runs just the site at `http://<blade-ip>:8080`.
   No `docker compose` on the box? Paste `docker-compose.yml` into the App Store's "Install a Custom App"
   instead, with `.` replaced by the folder's full path and the `${...}` values filled in.
5. **Check:**
   - `docker compose ps` shows both containers running; `curl -sI localhost:8080` returns 200
   - `docker compose logs tunnel` shows "Registered tunnel connection"
   - https://lss.photos loads, and https://lss.photos/admin asks for a login (any username + `ADMIN_PASSWORD`)

**Update to the latest code:** repeat B2 (it leaves `data/` and `.env` alone), then `docker compose restart gallery`.

### Good to know

- **Back up `data/`** (photos + `gallery.db`). That folder is the whole site's content.
- **502 from Cloudflare** means the `gallery` container isn't running, or the hostname's service isn't `HTTP` → `gallery:8080`.
- **Deleted photos** can stay in Cloudflare's cache for a while. To remove one right away: Caching → Purge Cache → its URL.
- Cloudflare's free plan limits each upload request to 100 MB. That's fine: the admin page uploads one photo per request.
