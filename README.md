# Attendance backend

A small Flask API that sits between the ESP32 RFID reader and the Aiven MySQL
database. The ESP32 cannot open a TLS connection to MySQL's native protocol
(no Arduino library implements the required in-protocol SSL upgrade), but it
*can* make a normal HTTPS request — so the device POSTs attendance events
here over HTTPS, and this service does the actual encrypted MySQL insert.

## API

### `POST /attendance`

Headers:
- `Content-Type: application/json`
- `X-API-Key: <shared secret>`

Body:
```json
{ "name": "Abilesh Mathavan P" }
```

Responses:
- `201` — logged
- `400` — missing/invalid `name`
- `401` — missing/incorrect API key
- `500` — database error

### `GET /health`

Returns `{"status": "ok"}` — useful for uptime checks.

## Local setup

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
```

1. In the Aiven console, open your MySQL service -> **Overview** -> download
   the CA certificate, save it as `backend/ca.pem`.
2. Copy `.env.example` to `.env` and fill in your real `DB_PASSWORD`,
   `DB_CA_CERT` (path to `ca.pem`), and a random `API_KEY` (this is the
   shared secret the ESP32 will send — pick something long and random, not a
   real password).
3. Run it:
   ```bash
   set -a; source .env; set +a   # or use python-dotenv / your OS's env tool
   python app.py
   ```
4. Test it:
   ```bash
   curl -X POST http://localhost:5000/attendance \
     -H "Content-Type: application/json" \
     -H "X-API-Key: <your API_KEY>" \
     -d "{\"name\": \"Test User\"}"
   ```

## Deploying

Any host that can run a small Python web service works (Render, Railway,
Fly.io, a VPS with gunicorn behind nginx, etc.). In production, run it with
gunicorn instead of the Flask dev server:

```bash
gunicorn -w 2 -b 0.0.0.0:$PORT app:app
```

Whichever host you pick, set the same environment variables from
`.env.example` in its dashboard/config (don't commit `.env` or `ca.pem` —
both are already gitignored), and make sure `ca.pem` is available at the
path you set `DB_CA_CERT` to (upload it as part of your deploy, or read it
from an environment variable if your host doesn't support file uploads).

Once deployed, you'll have a public HTTPS URL — put that in the ESP32
firmware's `include/secrets.h` as `BACKEND_URL`, along with the same
`API_KEY` you set here.
