# Studio — automated photo delivery & booking site

A photography portfolio, booking site, and private delivery workspace.

- Create a client/person and named shoot; bulk upload JPEG, PNG, WebP, or TIFF.
- Select 1–100 strongest photos (100 default); edit 0–selection count (20 default).
- Processing runs on one background worker. Queued/unfinished jobs persist in SQLite and resume on restart.
- Review the ranked selection, remove unwanted photos, and approve before delivery.
- Email the approved gallery and a six-digit single-use OTP, valid for 24 hours.
- Client sessions last seven days; resending rotates the OTP and invalidates previous sessions.
- Individual and ZIP downloads use the approved manifest, capped at 100 distinct photos.
  Edited files replace originals within that count. Originals/rejects are never publicly served.
- Each client gallery shows its ten highest-ranked selected photos in a randomized slideshow.
- Portrait homepage images appear in three uncropped, gently scrolling columns with a pause control.
- Existing public portfolio, independent booking, calendar sync, dashboard, and camera routes remain.

Camera RAW formats such as ARW/CR3/NEF must be exported to a supported image format before upload.
Unedited selections preserve their colors, but delivery files are converted to JPEG without original metadata.
Automated culling is a sharpness/exposure/face heuristic, not a trained aesthetic ranking model.
Built-in editing applies tone, color presets and sharpening; it does not provide AI masking.

## External AI / Photoshop-compatible MCP editor

`/admin/editor` exposes connection status, tool name, editing instructions, and enable/disable controls.
A real editing service is required; no Photoshop or Aftershoot account is bundled or connected automatically.
Configure these server environment variables:

```dotenv
RETOUCH_MCP_URL=https://your-editor.example/mcp
RETOUCH_MCP_TOKEN=your-provider-token
```

The connector uses Streamable HTTP JSON-RPC (JSON or SSE responses), initializes an MCP session,
checks `tools/list`, and invokes the chosen tool with `image_base64`, `mime_type`, and `instructions`.
The tool must accept these parameters and return an MCP `image` content block containing the edited image.
An editor exposing a different schema needs a bridge that translates this contract to its own tools.
The provider determines whether masking, relighting, skin retouch, or other AI operations are supported.
Connection testing lists tools only; it sends no client photographs. Enabling the connection authorizes
selected edit images to be sent to that provider. Tool failures mark the job failed for admin retry;
there is no silent fallback labeled as AI. The full photo pipeline was tested with a mocked editor,
not a live external editing account. Use the [MCP documentation](https://modelcontextprotocol.io/docs)
when adapting an editor.

## Delivery security and migration

Schema changes are additive. Existing shoots and originals remain intact. Previously delivered
key-only galleries now need admin review and a new OTP email before client access.
Five incorrect attempts lock an OTP. Codes are PBKDF2-hashed and consumed atomically; session tokens
are random and stored as hashes. Only manually published `_portfolio` images remain on `/media`.
Use HTTPS for production client delivery; the current VM's HTTP URL does not encrypt credentials or photos.
Back up `data/studio.db`, application code, and the persistent photo volumes before deployment.
Run a single application worker with this local queue; use an external job queue before scaling workers.

## Tests

```bash
pip install -r requirements.txt pytest httpx
python -m pytest tests/test_delivery.py -q
```

Tests use temporary databases/photos and mocked email, checking selection/edit counts, corrupt images,
OTP expiry/reuse/attempt limits, cross-gallery isolation, old public-path blocking, and approved ZIP contents.

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

This is a heuristic baseline, open for you to tune the weights in `score_image()`,
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

- Processing currently uses a persistent SQLite-backed single-worker queue. For large shoots or
  multiple application replicas, move jobs to a dedicated queue such as Celery/RQ.
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