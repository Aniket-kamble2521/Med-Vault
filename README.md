# Medical History Portable Vault (Modular MVP)

Production-style modular Flask app with:

- Flask Blueprints
- Secure auth (`Flask-Login` + `bcrypt`)
- Encrypted medical summary fields (Fernet)
- File upload/search/download/delete
- Emergency QR with expiring token
- Starter schema for future modules (appointments, vitals, reminders, etc.)

## Run locally

### 1) Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) Start the server

```bash
python app.py
```

Open `http://127.0.0.1:5000`.

## New folder structure

```text
app.py
app_pkg/
  __init__.py
  db.py
  extensions.py
  models.py
  routes/
    auth.py
    core.py
    emergency.py
    modules.py
  services/
    security.py
    files.py
    summary.py
templates/
static/
schema.sql
```

## Step-by-step upgrade path

### Step 1 (done): Refactor structure

- `app_pkg/__init__.py` now builds the app and registers blueprints.
- `app.py` is now only a startup file.
- Routes are separated by responsibility.

### Step 2 (done): Authentication + security

- `app_pkg/routes/auth.py`: signup/login/logout.
- Password hashing via `bcrypt`.
- Session/login protection via `Flask-Login`.
- Upload restrictions in `app_pkg/routes/core.py`:
  - allowed file types: PDF/JPG/PNG
  - max size: 8MB
  - unique secure stored filename
- Sensitive fields are encrypted using `app_pkg/services/security.py`.

### Step 3 (started): Module scaffolding

- `schema.sql` now includes starter tables for:
  - family profiles
  - timeline events
  - prescriptions
  - doctors + appointments
  - vaccinations
  - allergy registry
  - reminders
  - vitals logs
- `app_pkg/routes/modules.py` + `templates/modules_roadmap.html` provide a roadmap page.

## How the emergency QR works

### 1) Generating a QR code in Python

- A QR code is just an image encoding some text (here: a URL).
- In `app.py`, we do:
  - Build the full emergency URL (example: `http://127.0.0.1:5000/emergency/<token>`)
  - `qrcode.make(url)` creates a QR image
  - Save it as a PNG so the browser can show it

### 2) Creating a secure token

- We generate a random, hard-to-guess string using `secrets.token_urlsafe(32)`.
- That token is stored in SQLite in the `emergency_tokens` table with:
  - `user_id` (who it belongs to)
  - `expiry_time` (when it stops working)

### 3) Validating tokens (when a doctor scans it)

When someone opens `/emergency/<token>`:

- The server looks up the token in SQLite.
- If it does not exist, or it is expired, we show an **expired** page (HTTP 410).
- If it is valid, we show **only**:
  - Patient name
  - Blood group
  - Allergies
  - Current medications
  - Important conditions

### 4) Expiry logic (10 minutes)

- On token creation, we do:
  - `expiry = created_at + (10 * 60)`
- On every request to generate/view emergency links, we also delete old tokens:
  - `DELETE FROM emergency_tokens WHERE expiry_time <= now`

### 5) Connecting the frontend button to the backend

- The button in `templates/dashboard.html` calls JS in `static/app.js`.
- JS sends:
  - `fetch("/generate_emergency_qr", { method: "POST" })`
- Flask returns JSON containing:
  - `qr_image_url` (PNG)
  - `emergency_url` (the link encoded in the QR)
  - `expires_at`

## Auto-categorization feature

- On upload, backend checks filename keywords and predicts medical file type.
- Saved in database fields:
  - `category` (e.g., `Prescription`, `Blood Report`)
  - `category_confidence` (0.0 to 1.0 heuristic score)
- This is an MVP approach; later you can upgrade to OCR + LLM classification.

## Project structure

- `app.py`: Flask routes (auth, upload, QR token generation, emergency view)
- `db.py`: SQLite connection helpers
- `schema.sql`: database tables
- `templates/`: HTML pages
- `static/`: CSS + JS
- `instance/`: created automatically at runtime
  - `app.db` (SQLite database)
  - `uploads/` (uploaded files)
  - `qr/` (generated QR PNGs)

## Notes (MVP security)

- Passwords are stored as a **hash** (not plain text).
- Emergency token links expire quickly and only show a summary.
- For a real production app you would also add HTTPS, rate limiting, CSRF protection, and stricter access controls.

