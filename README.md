# HSM Exporter 0.1

A live inventory viewer for Thales Luna HSM appliances, on its way to also being a
Prometheus exporter - meant to sit alongside the other exporters, not be a bespoke
one-off dashboard.

One deliberately boring architecture:

- `app/luna/`: HTTP only (`session.py` auth/session handling, `client.py` the actual REST calls,
  `provisioning.py` the one-time "create the monitor REST API user" flow)
- `app/monitor.py`: pure polling logic - `poll_once()`/`poll_all()` (config-vs-actual CU/CO role
  diffing) and `poll_clients_once()` (NTLS client-to-partition mapping) - no I/O beyond the Luna
  calls themselves
- `app/poller.py`: **two** independent background threads per configured HSM - `RolesPoller`
  (~60s, the login-state signal) and `ClientsPoller` (~10min, much larger fan-out, changes rarely).
  Each stops itself on a fatal (connection/auth) failure instead of retrying a struggling appliance
  forever; `HsmMonitor` gives the UI one start/stop/check-now surface that controls both while
  keeping them isolated from each other underneath (a stuck client fan-out never delays the
  roles poll, and vice versa)
- `app/state.py`: the one shared cache (in-memory, lock-guarded) both pollers write into and Flask
  routes read from. The two pollers write to separate keys (`role_problems`/`client_problems`,
  etc.) so one can never clobber the other's findings via `state.update()`'s dict-merge semantics -
  routes.py combines them into one `problems` view at read time
- `app/routes.py`: thin Flask layer - an HSM overview page, a per-HSM detail page, start/stop/
  check-now controls, and the setup flow
- `app/templates/`: HTML

No database, no message queue. Web requests always read the last poll result from memory and
return instantly, regardless of how long actually talking to the appliances takes.

## Configure

Edit `config/hsms.yml` (see the checked-in example) and set in the environment:

```bash
export LUNA_USERNAME='...'
export LUNA_PASSWORD='...'
```

Partition field names beyond `id` (e.g. serial, label) are not yet confirmed against a live
API v15 appliance - the raw JSON is always included alongside whatever's normalized.

## Local development with uv

```bash
uv sync  # creates uv.lock on first run
LUNA_CONFIG="$PWD/config/hsms.yml" uv run flask --app run:app run --debug
uv run pytest
uv run ruff check .
```

To just check that auth/session establishment and the 3-4 REST calls work against a real
appliance without running the web app:

```bash
LUNA_CONFIG="$PWD/config/hsms.yml" uv run python probe.py
```

## Container

```bash
./build.sh
./run.sh
```

Open `http://127.0.0.1:8080/`.

Endpoints:

- `/` - overview of all configured HSMs: serial, poller thread status, color-coded problem count
- `/hsms/<name>` - full partition/role breakdown, problems list, setup form for one HSM
- `/hsms/<name>/start`, `/hsms/<name>/stop`, `/hsms/<name>/check-now` - POST, control that HSM's poller
- `/hsms/<name>/setup` - POST, provisions/repairs the monitor REST API user (needs the appliance admin password)
- `/api/v1/hsms` - JSON, the same cache the overview page reads
- `/health`

The archive does not contain a fabricated lock file: run `uv lock` or `uv sync` once against your configured package index, then commit the generated `uv.lock`.
