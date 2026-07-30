# Luna HSM Monitor 0.1

One deliberately boring architecture:

- `app/luna/`: HTTP only (`session.py` auth/session handling, `client.py` the actual REST calls,
  `provisioning.py` the one-time "create the monitor REST API user" flow)
- `app/monitor.py`: pure polling logic - `poll_once()`/`poll_all()`, config-vs-actual CU/CO role
  diffing, no I/O beyond the Luna calls themselves
- `app/poller.py`: one background thread per configured HSM, calling `poll_once()` on a timer
  (~60s). Stops itself on a fatal (connection/auth) failure instead of retrying a struggling
  appliance forever - resume via the UI's "check now" (which doubles as "resume")
- `app/state.py`: the one shared cache (in-memory, lock-guarded) poller threads write into and
  Flask routes read from - no second polling path for the web UI vs. anything else that reads it
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
