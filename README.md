# Studio — automated photo delivery & booking site

An end-to-end system for a solo/small photography business:

- **Upload** a shoot's raw photos through an admin page
- **Auto-crop + auto-edit** every photo (exposure, color, sharpening; optional style preset)
- **Auto-select up to 100 best photos** (default 100) using sharpness/exposure/eyes-open scoring,
  deduplicated across burst sequences
- **Email the client automatically** with a private gallery link + unique access key
- **Client gallery** — client opens their link, sees only their best shots, can download
- **Portfolio site** — pick any delivered photos into a public homepage grid; up to 10 are
  shown randomly on each page load
- **Booking page** — client picks an open slot; it's synced to your Google Calendar,
  and you get a confirmation email sent to the client automatically

## Run it locally

```bash
cd photostudio
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the values (see below)
uvicorn app.main:app --reload
```

- Public site: http://localhost:8000
- Booking: http://localhost:8000/book
- Admin: http://localhost:8000/admin (password = ADMIN_PASSWORD in .env)

## One-time Google Calendar connection

```bash
python -m app.calendar_utils
```

This opens a browser once, you sign into the Google account whose calendar
you want bookings to land on, and it saves a `token.json`. After that,
booking sync runs automatically — no browser needed again (it auto-refreshes).

## How the "best 100" selection works

`app/pipeline.py` scores each photo on:
- **Sharpness** (Laplacian variance — catches blur)
- **Exposure** (histogram clipping — catches blown highlights/crushed blacks)
- **Faces/eyes** (Haar cascade — rewards open eyes, flags likely closed eyes)
- **Duplicates** — a perceptual hash groups burst sequences so you don't get
  10 near-identical shots in your top 100; only the best of each burst competes.

This is a solid heuristic baseline (same idea as what Aftershoot/Imagen use
under the hood), fully open for you to tune the weights in `score_image()`,
or later replace with a model trained on photos you've personally picked
in the past — that's the natural upgrade path once you have a library of
"what I actually chose" data.

## Deploying to your VPS with auto-deploy on every push

This repo is set up so pushing to `main` on GitHub automatically deploys
to your VPS and bumps the version — no manual SSH-and-restart needed
after the first setup.

### One-time VPS setup

```bash
# On the VPS
git clone <your-repo-url> ~/photostudio
cd ~/photostudio
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in real values
chmod +x deploy/deploy.sh
```

Install the systemd service (keeps the app running, restarts it on crash
or reboot):

```bash
sudo cp deploy/studio.service /etc/systemd/system/studio.service
sudo nano /etc/systemd/system/studio.service   # replace YOUR_VPS_USERNAME
sudo systemctl daemon-reload
sudo systemctl enable --now studio
```

Let the deploy script restart the service without a password prompt:

```bash
sudo visudo -f /etc/sudoers.d/studio-deploy
# add this line (replace YOUR_VPS_USERNAME):
# YOUR_VPS_USERNAME ALL=(ALL) NOPASSWD: /bin/systemctl restart studio
```

Put it behind HTTPS with Caddy (easiest option — auto-renewing certs):

```bash
sudo apt install caddy
sudo cp deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile   # replace yourdomain.com
sudo systemctl reload caddy
```

### One-time GitHub setup

In your repo → Settings → Secrets and variables → Actions, add:

| Secret | Value |
|---|---|
| `VPS_HOST` | your VPS's IP address or hostname |
| `VPS_USER` | the SSH username you use to log into the VPS |
| `VPS_SSH_KEY` | a private SSH key GitHub can use to log in (see below) |
| `VPS_PORT` | only needed if SSH isn't on port 22 |

To create a deploy key GitHub can use:

```bash
ssh-keygen -t ed25519 -f deploy_key -N ""
cat deploy_key.pub    # append this to ~/.ssh/authorized_keys on the VPS
cat deploy_key         # paste this whole thing into the VPS_SSH_KEY secret
```

### From then on

Every `git push` to `main`:
1. GitHub Actions automatically bumps a version tag (`v0.1.0` → `v0.1.1`,
   patch by default — write `#minor` or `#major` in a commit message to
   bump those instead) and creates a GitHub Release.
2. It SSHes into your VPS and runs `deploy/deploy.sh`, which pulls the
   new code, reinstalls dependencies only if `requirements.txt` changed,
   writes the new version into a `VERSION` file, and restarts the service.
3. The currently-running version is shown at the bottom of `/admin`, so
   you can always confirm a deploy actually landed.

No manual SSH needed after the one-time setup above.

### Docker deployment

For a server already running another application on port 8000, the included
`docker-compose.yml` exposes Studio on port 8001 and persists the database,
original uploads, processed galleries, portfolio files, and Google token under
`data/`. Copy `.env.example` to `.env`, fill in the production values, then run:

```bash
docker compose up -d --build
```

The application is then available at `http://SERVER_IP:8001`. Configure your
reverse proxy to forward your domain to port 8001 before using HTTPS.

## Scaling notes

- For real shoot volumes (hundreds–thousands of RAW files), move
  `process_shoot()` in `main.py` off the request thread and into a background
  job (FastAPI `BackgroundTasks`, or a queue like Celery/RQ) so uploads don't
  time out.
- Swap local folder storage (`app/processed/`) for S3 or Azure Blob by
  replacing the file read/write calls in `pipeline.py` and `main.py` —
  the rest of the app (DB, email, gallery links) doesn't need to change.
- SQLite (`app/studio.db`) is fine for one photographer; move to Postgres
  if you ever have a team or need concurrent writes.

## What I still need from you to make this fully live

**Required to send real emails:**
- SMTP provider + credentials (Gmail app password is easiest to start, or
  SendGrid/Mailgun/SES for higher volume)
- The email address/name you want client emails to come from

**Required for the booking calendar:**
- A Google Cloud project with the Calendar API enabled, and its
  Client ID + Client Secret (OAuth "Desktop app" type)
- Which calendar to book into, if not your primary one

**Required to launch the site:**
- Studio name, tagline/bio text, and a real domain to point it at
- An admin password of your choosing
- Your bookable hours/days (currently hardcoded as 9am–6pm, 2-hour spacing —
  easy to change in `book.html`)
- Whether you want deposits/payment collected at booking (I didn't wire up
  Stripe — say the word and I'll add it)

**Nice to have, not required to run:**
- A logo/wordmark for the nav bar
- Any brand colors, if you don't want the current charcoal/ivory/amber palette
- Sample photos to test the pipeline end-to-end and tune the auto-edit presets
  to your actual style
