# L&S Shots photo galleries

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

## Deploy to the ZimaBlade (Docker)

`docker-compose.yml` runs two containers: the site (waitress, port 8080) and a Cloudflare Tunnel
that serves it at `https://<your domain>` without opening router ports.

1. **SSH in.** CasaOS has SSH on by default (user `casaos`; change the default password).
   ZimaOS: Settings → Developer Mode → SSH on.
2. **Copy the code** onto the SATA data drive, not the 32 GB eMMC (drives mount under `/media/`).
   From a clone of this repo:
   ```sh
   git archive HEAD | ssh <user>@<blade-ip> "mkdir -p /media/<drive>/gallery && tar -x -C /media/<drive>/gallery"
   ```
3. **Create `.env`** in that folder on the blade (ask the owner for both values):
   ```
   ADMIN_PASSWORD=<admin password>
   TUNNEL_TOKEN=<Cloudflare Zero Trust → Tunnels → token; public hostname → http://gallery:8080>
   ```
4. **Start:** `docker compose up -d` (no tunnel yet: `docker compose up -d gallery`).
   No `docker compose` on the box? Paste `docker-compose.yml` into the App Store's
   "Install a Custom App" instead, with `.` replaced by the folder's full path and the `${...}` values filled in.
5. **Check:** `curl -sI localhost:8080` returns 200; logs with `docker compose logs -f`.

**Update:** repeat step 2 (it leaves `data/` and `.env` alone), then `docker compose restart gallery`.
