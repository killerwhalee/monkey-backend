# Monkey Backend

Django + DRF + Celery backend for the **Monkey** project — a population of virtual "monkey"
traders that randomly buy/sell random stocks at random times against the **real** Korean stock
market via the 한국투자증권(KIS) Open API's virtual/paper-trading environment.

This guide covers setting up the backend from scratch on a fresh Linux machine: system
dependencies, Python environment, environment variables, database migrations, initial data, and
running the Django/Celery stack.

## 1. Requirements

- Linux (any distro with `systemd` or an init system that can run long-lived processes)
- Python 3.12+ (managed automatically by `uv` if not already installed)
- [`uv`](https://docs.astral.sh/uv/) for Python dependency management
- Redis (Celery broker)
- `git`

## 2. Install system dependencies

### Redis

Debian / Ubuntu:

```bash
sudo apt update
sudo apt install -y redis-server
sudo systemctl enable --now redis-server
```

Arch Linux:

```bash
sudo pacman -S redis
sudo systemctl enable --now redis
```

Verify Redis is reachable on the default port used by this project (`127.0.0.1:6379`):

```bash
redis-cli ping   # should print "PONG"
```

### uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Restart your shell (or `source ~/.bashrc` / `~/.zshrc`) so `uv` is on `PATH`.

## 3. Get the code

```bash
git clone <repo-url> monkey
cd monkey/01-backend
```

## 4. Install Python dependencies

```bash
uv sync
```

This creates a `.venv/` and installs everything pinned in `uv.lock` (Django, DRF, Celery,
django-celery-beat/results, etc.), including `dev` extras (pre-commit, isort).

## 5. Configure environment variables

Create a `.env` file in `01-backend/` (this directory). It is loaded by `core/settings.py` via
`django-environ` and is **never committed**.

```bash
touch .env
```

Populate it with the following keys:

| Variable | Required | Default | Notes |
|---|---|---|---|
| `DJANGO_DEBUG` | no | `False` | Set `True` for local development |
| `DJANGO_SECRET` | **yes** | — | Django `SECRET_KEY`. Generate one (see below) |
| `DJANGO_DATABASE` | **yes** | — | DB URL, e.g. `sqlite:///db.sqlite3` |
| `DJANGO_ALLOWED_HOSTS` | no | `127.0.0.1,localhost` | Comma-separated list |
| `CORS_ALLOWED_ORIGINS` | no | `http://localhost:5173` | Comma-separated list; should include the frontend dev/prod origin |
| `CORS_ALLOW_CREDENTIALS` | no | `True` | |
| `KIS_APP_KEY` | yes (for trading) | `""` | KIS Open API app key |
| `KIS_APP_SECRET` | yes (for trading) | `""` | KIS Open API app secret |
| `KIS_CANO` | yes (for trading) | `""` | KIS account number (8-digit prefix, no hyphens) |
| `KIS_API_BASE_URL` | no | `https://openapivts.koreainvestment.com:29443` | Default points to the **virtual/paper-trading** API |
| `KIS_ENVIRONMENT` | no | `virtual` | Used as the key for the cached `KisAccessToken` row |
| `KIS_ACNT_PRDT_CD` | no | `01` | KIS account product code (last 2 digits of account number) |
| `KIS_TOKEN_REFRESH_MARGIN_SECONDS` | no | `300` | Refresh the KIS token this many seconds before it expires |

Generate a secret key:

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(50))"
```

Example minimal `.env` for local development with SQLite:

```env
DJANGO_DEBUG=True
DJANGO_SECRET=<generated-secret>
DJANGO_DATABASE=sqlite:///db.sqlite3
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost
CORS_ALLOWED_ORIGINS=http://localhost:5173

KIS_APP_KEY=<your-kis-app-key>
KIS_APP_SECRET=<your-kis-app-secret>
KIS_CANO=<your-kis-virtual-account-number>
```

KIS Open API credentials and a virtual-trading account are obtained from
https://apiportal.koreainvestment.com — issue an app key/secret pair and enable the
virtual/paper-trading account for the issued app.

To use Postgres or another database instead of SQLite, set `DJANGO_DATABASE` to a URL such as
`postgres://user:password@127.0.0.1:5432/monkey` and ensure the corresponding driver is installed.

## 6. Run database migrations

```bash
uv run python manage.py migrate
```

This creates all tables, including those for `django_celery_beat` / `django_celery_results`, and
runs the data migrations that pre-register the following periodic tasks:

| Periodic task | Schedule | Purpose |
|---|---|---|
| `Snapshot monkey daily metrics` | every day at 00:00 KST | Records each monkey's daily P&L snapshot |
| `market.auto.open` | weekdays 09:00 KST | Turns the global trading switch **on** |
| `market.auto.close` | weekdays 15:30 KST | Turns the global trading switch **off** |

Per-monkey trading tasks (`monkey.run.<id>`) are created/removed automatically whenever a
`Monkey` row is created/deleted (see `Monkey.save()`/`Monkey.delete()` in `monkey/models.py`), so
no manual setup is needed for those.

## 7. Create a Django admin user

```bash
uv run python manage.py createsuperuser
```

This account is used both for the Django admin (`/admin/`) and for staff-only API endpoints
(JWT login via `/api/auth/token/`).

## 8. Load initial data

### 8.1 Stock master data (KOSPI/KOSDAQ)

Monkeys can only trade stocks that exist in the local `Stock` table. Populate it by running the
`update_market` task once. With Redis running, start a worker in one terminal:

```bash
uv run celery -A core worker -l info
```

...and trigger the task from another terminal:

```bash
uv run python manage.py shell -c "from market.tasks import update_market; update_market.delay()"
```

Watch the worker log for completion. This downloads and parses the KRX KOSPI/KOSDAQ `.mst` master
files and upserts every listed stock into the `Stock` table. Re-run periodically (e.g. via a
Django-admin-managed periodic task) to pick up newly listed/delisted tickers.

### 8.2 KIS access token

The KIS API requires an OAuth token, cached in the `KisAccessToken` table and shared across Celery
workers. It is fetched lazily the first time `KisClient` is used, but you can fetch it eagerly to
verify your `KIS_APP_KEY`/`KIS_APP_SECRET`/`KIS_CANO` credentials are correct:

```bash
uv run python manage.py shell -c "from monkey.tasks import update_token; update_token.delay()"
```

Check `KisAccessToken` in the Django admin (`/admin/monkey/kisaccesstoken/`) to confirm a token
was saved.

### 8.3 Global trading switch

`GlobalMonkeyControl` is a singleton (`pk=1`) that gates *all* automatic trading. It is created
automatically (with `enabled=False`) the first time it's accessed — e.g. on the first API request
to `/api/global-monkey-control/current/`, or the first scheduled task run. No manual step is
required, but trading stays **off** until you (or the `market.auto.open` schedule, on weekday
mornings) enable it via the admin or the API.

You can also tune `kill_threshold` (earning ratio below which a monkey is auto-deactivated) and
`order_interval_seconds` (how often each monkey trades) on this singleton via
`/admin/monkey/globalmonkeycontrol/`.

### 8.4 Create monkeys

Create monkeys via the admin (`/admin/monkey/monkey/`) or the API (`POST /api/monkeys/` or
`POST /api/monkeys/bulk-create/`, staff-only). Each created monkey automatically gets its own
periodic Celery task (`monkey.run.<id>`) that fires every `order_interval_seconds`, gated by the
global trading switch.

## 9. Running the stack

Three long-lived processes are required. Run each in its own terminal/session (or as systemd
units / a process manager in production):

```bash
# Django dev server
uv run python manage.py runserver

# Celery worker — executes tasks (KIS calls, order placement, etc.)
uv run celery -A core worker -l info

# Celery beat — triggers scheduled tasks (per-monkey trading, market open/close, daily snapshot)
uv run celery -A core beat -l info
```

All three depend on Redis being up (`CELERY_BROKER_URL = redis://127.0.0.1:6379/0`, hardcoded in
`core/settings.py`) and on the `.env`/migrations steps above being completed.

## 10. Verify the setup

- `http://127.0.0.1:8000/admin/` — log in with the superuser created in step 7; confirm
  `Stock` rows exist (step 8.1), `KisAccessToken` has a row (step 8.2), and
  `GlobalMonkeyControl` (pk=1) exists.
- `http://127.0.0.1:8000/api/dashboard-summary/` — should return JSON (public endpoint).
- With the worker + beat running and the global switch enabled, `Order` rows should start
  appearing for active monkeys at the configured interval.

## Development commands

```bash
# Run all tests
uv run python manage.py test

# Run a single app's tests / a single TestCase / a single test method
uv run python manage.py test monkey
uv run python manage.py test monkey.tests.MonkeyServiceTests
uv run python manage.py test monkey.tests.MonkeyServiceTests.test_successful_buy_updates_local_ledger

# Lint/format (also runs automatically via pre-commit)
uv run pre-commit run --all-files
```

## Notes

- `db.sqlite3` and `celerybeat-schedule.db` are runtime artifacts created automatically; they are
  not required to exist before setup.
- Never commit `.env` — it contains KIS API credentials and the Django secret key.
- This backend is configured for KIS's **virtual/paper-trading** environment by default
  (`KIS_API_BASE_URL`/`KIS_ENVIRONMENT`); no real funds are used.
