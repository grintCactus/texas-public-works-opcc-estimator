# Deploying the public site

The public demo runs at https://129-146-109-44.sslip.io: one small cloud VM, two containers (the app and Caddy for HTTPS). This folder holds everything needed to rebuild the package and update the site.

## What is in this folder

| Path | What it is |
|---|---|
| `package/` | The files that turn a package into an image: `Dockerfile`, `requirements.txt` (the server's pinned packages), `.dockerignore`, and the `README.md` and `.gitattributes` a Hugging Face Space needs. |
| `oracle/` | For the VM: `docker-compose.yml` (app + Caddy), `Caddyfile` (HTTPS), `setup.sh` (installs or updates everything on the VM), `.env.example`. |
| `tools/` | Scripts to fetch or build the data bundle, assemble the package, check it and guard commits (table below). |
| `bundle.lock.json` | Which public data bundle this checkout uses: release tag, asset name, SHA-256. |
| `redeploy.sh` | Sends the package to the VM and restarts the site. |
| `space/` | **Built, not committed.** The assembled package: `src/`, `ui/`, `data/clean/` and the package files. It is the Docker build context and the folder sent to the VM. |
| `bundle/` | **Built, not committed.** On the owner's working copy only: the public bundle cut from the full tables. |

| Tool | Use |
|---|---|
| `tools/fetch_bundle.py` | Fresh checkout: download the public data bundle, verify it, unpack it into `data/`. |
| `tools/assemble_space.py` | Build `deploy/space` from this checkout and a bundle; refuses data that is not public; scans the result. |
| `tools/check_package.sh` | Price 400 public lettings in the checkout and in the package; every number must be equal. |
| `tools/identity_sweep.py` | The engine behind `check_package.sh`. |
| `tools/build_bundle.py` | Owner's working copy: cut the public bundle from the full `data/clean` (drops test rows, scans every cell). |
| `tools/trace_reads.py` | Owner's working copy: record which `data/clean` files the server opens (the bundle holds exactly those). |
| `tools/release_bundle.py` | Owner's working copy: pack `deploy/bundle`, write `bundle.lock.json`, and with `--publish` upload the archive as a release asset. |
| `tools/scan_public.py` | Scan a folder for private patterns, home paths and e-mail addresses. Counts only. |
| `tools/guard_commit.py`, `tools/install_hooks.sh` | The pre-commit guard of this repository. |

## The public mode

The server on the VM runs with `BIDEST_PUBLIC=1` (`ui/public_mode.py`):

- no server-side writes: corrections and saved worksheets stay in the visitor's browser;
- uploads are processed in memory (a RAM disk) and deleted after the request; nothing is stored;
- no reading by an AI model, no upload inbox, no links to files on the machine: each reference links to its public source page and to its copy in the public data repository;
- a 25 MB upload cap, rate limits per address, security headers, no program detail in error messages;
- an optional access code (`DEMO_ACCESS_CODE`), off by default.

Settings (environment variables): `BIDEST_PUBLIC`, `PORT` (7860), `DEMO_ACCESS_CODE`, `BIDEST_TRUSTED_PROXIES` (how many reverse proxies sit in front: 1 behind Caddy), `BIDEST_TMPDIR`, `BIDEST_FRAME_ANCESTORS` (only when the site is shown inside another page), `BIDEST_RATE_SCALE` (loosen or tighten the rate limits).

## A. Update the site from a fresh checkout

Use this when the code changed and the data did not.

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/python deploy/tools/fetch_bundle.py        # data/ <- the public bundle (21 MB download, 350 MB unpacked)
.venv/bin/python ui/run.py                           # try it: http://127.0.0.1:8017/
.venv/bin/python deploy/tools/assemble_space.py      # deploy/space; must end with "scan CLEAN"
bash deploy/tools/check_package.sh                   # must end with "RESULT: IDENTICAL in 400 schedules"
DEPLOY_HOST=<public IP> DEPLOY_KEY=<ssh private key> bash deploy/redeploy.sh
```

`redeploy.sh` needs SSH access to the VM. The key is held by the project owner and is not in this repository; ask the owner for it or for a session on the machine that has it. Nothing else is secret: the site has no database and no stored uploads.

To try the image without a server (any machine with Docker):

```bash
docker build -t opcc-estimator deploy/space
docker run --rm -p 7860:7860 opcc-estimator          # http://localhost:7860
```

The image is built on the VM by `setup.sh`, so Docker is not needed on the machine that deploys.

## B. Update the site after a data rebuild (owner's working copy)

A working copy that ran `src/rebuild_all.sh` has the full tables in `data/clean`, including test rows that must never leave the machine. The public bundle is cut from them:

```bash
# 1. only when the code started reading a new data file: record what the server opens
.venv/bin/python deploy/tools/trace_reads.py . deploy/tools/trace_production.json --samples <folder with a few public csv/xlsx/pdf samples>
# 2. the public bundle: rows with source_set == "external" only, every cell scanned (about 2.5 minutes)
.venv/bin/python deploy/tools/build_bundle.py deploy/tools/trace_production.json
# 3. the package, from that bundle (about 2 minutes)
.venv/bin/python deploy/tools/assemble_space.py
# 4. the working copy and the package must price 400 public lettings identically
bash deploy/tools/check_package.sh
# 5. publish the bundle so that fresh checkouts get the same data, then commit bundle.lock.json
.venv/bin/python deploy/tools/release_bundle.py --publish
# 6. deploy
DEPLOY_HOST=<public IP> DEPLOY_KEY=<ssh private key> bash deploy/redeploy.sh
```

`build_bundle.py` and `assemble_space.py` exit non-zero when a scan is not clean. Do not publish or deploy then.

What `build_bundle.py` guarantees:

- a table with a `source_set` column keeps its `external` rows only (private test rows and the locked public test set are dropped);
- `project_metadata.csv` keeps the rows of shipped lettings only;
- every other table is derived from external rows only and is copied whole, with the reason recorded in the manifest;
- every cell is scanned against the machine's private patterns; a hit is replaced by a neutral stand-in, the same one everywhere, so keys stay keys. The manifest records counts, never the text.

## The server

- **What it is.** A VM with 2 ARM cores and 12 GB of memory (Oracle Cloud "Always Free" Ampere A1, Ubuntu). The app needs about 1.1 GB once loaded and about 1.4 GB while loading; a 512 MB free instance elsewhere is too small.
- **Address.** `https://<public IP with dashes>.sslip.io`. Caddy gets and renews the certificate and redirects HTTP to HTTPS.
- **First-time setup** (done once by the account owner, in the cloud console): create the VM with a public IPv4 address and the deploy key's public half; open TCP 80 and 443 (and UDP 443) in the subnet's security list; then run section A's last command. `setup.sh` installs Docker, opens the VM's own firewall, writes `.env`, builds and starts both containers and waits for HTTPS. It is safe to run again.
- **Containers.** Read-only root file system, `/tmp` in memory, all Linux capabilities dropped, an unprivileged user, a 4 GB memory limit. The app port is reachable only through Caddy. Both restart after a reboot.
- **Access code** (optional, letters and digits): `ssh ... "cd ~/opcc/oracle && sudo DEMO_ACCESS_CODE='<code>' ./setup.sh"`; remove it with an empty value. Never write a real code into a file or the repository.
- **Logs:** `ssh ... "cd ~/opcc/oracle && sudo docker compose logs -f app"`. **Stop:** `... sudo docker compose down`.
- **Cost.** The VM is inside the provider's free allowance. Keep it that way: create nothing that is not marked free, and keep the budget alert the owner set. Idle free instances can be reclaimed on a free-tier account; the owner's account is pay-as-you-go to avoid that.

## After a deploy

1. `curl https://<site>/api/health` answers.
2. Open the site, upload `ui/fixtures/takeoff_public_creek_bank.csv` and estimate; the numbers must match a local run of the same file (`ui/integration_check.py`, see `docs/HANDOFF.md`).
3. Note the deploy (date, commit, bundle tag) in `docs/HANDOFF.md` under "Deploy log".

## Other hosts

The same package runs anywhere that builds a Dockerfile and gives the container about 2 GB of memory. A Hugging Face Space (Docker SDK) takes the `deploy/space` folder as its repository; set `BIDEST_FRAME_ANCESTORS=https://huggingface.co` and `BIDEST_TRUSTED_PROXIES=1` there. Free 512 MB instances (Render, Koyeb) are too small.
