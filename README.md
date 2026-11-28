# SGLang CI runner kit

[中文版](README.zh-CN.md)

sglang runs vendor CI lanes on vendor hardware. A vendor host never holds a
GitHub token. Instead, for each job it asks the sglang runner broker for a
**just-in-time (JIT) runner config**. That config registers one runner in the
vendor's runner group, and the runner deregisters itself after one job.

This repo is everything a vendor needs:

| Path | What it is |
|---|---|
| `client/jit_client.py` | generates a host key pair; signs a request and fetches one JIT config |
| `client/runner_loop.sh` | the loop on each host: fetch a config, run one job, clean up, repeat |
| `examples/systemd/` | a systemd unit and env files for both modes |
| `keys/<vendor>/<kid>.pub` | the registered public key of each vendor host |
| `vendors/<vendor>/submitters.txt` | GitHub accounts allowed to submit keys for that vendor |
| `vendors/<vendor>/labels.txt` | the runner labels that vendor's keys may register |
| `docs/design.md` | how the broker and the runners fit together, and why |

## How it works

1. Each host has its own Ed25519 key pair. The private key stays on the host;
   the public key is submitted here by PR.
2. sglang reviews the PR, then syncs the key into the broker with a second
   reviewed change and deploys it. Only then does the key work.
3. The host signs a short-lived token (60 s) with its key and calls
   `<BROKER_URL>/v1/jit/<vendor>`. The broker checks the key, the host name,
   the labels, the rate limits and how many runners the host and your vendor
   already have, and returns a config for one runner.
4. The runner runs one job and is gone. The loop asks again.

The broker decides what each key may do: its labels (your
`vendors/<vendor>/labels.txt`), its host name, how many runners its host may
have at once, its rate, and when it expires. sglang can narrow these for a
single key. Nothing in your request or token can widen them.
[docs/design.md](docs/design.md) explains the design.

## Rules for your setup

Whatever runs your runners (systemd, Kubernetes, Slurm, Jenkins), it must
follow these three rules:

1. **Job code can never reach the private key.** CI runs code from public pull
   requests; treat it as hostile. Keep the key outside the job's container
   (mode A below) or on a machine that runs no jobs (mode B).
2. **One config per job, in a fresh, isolated environment.** Never reuse a
   runner directory or container across jobs.
3. **Back off with jitter on failure.** The broker's request budget is shared
   by all vendors. `runner_loop.sh` already does this.

`runner_loop.sh` implements two common setups (below). Other setups are fine
if they keep the three rules. For example, on **Slurm**:
- keep the key off shared NFS and `/home` that compute nodes can see;
- pass the JIT config to the job as a `0600` file, not with
  `sbatch --export` (that shows up in `scontrol show job`);
- run each job in a fresh enroot/pyxis or Apptainer container, since nodes
  are reused.

## Onboarding

### 1. Get your GitHub accounts listed

Tell sglang which GitHub accounts will submit keys for your company. sglang
confirms them with you once, outside GitHub, and adds them to
`vendors/<vendor>/submitters.txt`. From then on, every key change is a PR
here; never send key files by chat or email.

### 2. Generate a key on each host

```bash
pip install -r client/requirements.txt
```

```bash
python3 client/jit_client.py keygen --out /etc/sglang-ci/<kid>
```

This writes `<kid>.key` (mode 0600, stays on the host) and `<kid>.pub`.
Generate on the host itself, one pair per host, never copied between hosts.

**Choosing the kid.** `<vendor>-<host>-<yyyy>-<mm>`, at most 64 characters,
for example `mlu-host01-2026-11`:
- `<host>` is the host name this key may register runners for
  (`[A-Za-z0-9][A-Za-z0-9.-]{0,39}`, no underscores). Set `RUNNER_HOST` to
  exactly this value.
- `<yyyy>-<mm>` is the month you made the key, not later (CI rejects a future
  month). A kid is never reused: a new key always gets a new kid.
- **A key works for 13 months at most:** until the 1st of the 13th month after
  its `<yyyy>-<mm>` (UTC). `mlu-host01-2026-11` stops on 2027-12-01. sglang may
  set an earlier date for a key. sglang tells you about 30 days before a key
  expires; rotate it then (below).

### 3. Open a PR

Add the public key, unchanged, as `keys/<vendor>/<kid>.pub`. The PR must:
- be opened by an account in `vendors/<vendor>/submitters.txt`;
- change nothing outside `keys/` and `vendors/<vendor>/labels.txt`;
- not modify an existing key file.

CI checks all of this, plus the kid format and date and the key format, and
prints the expiry date and the SHA-256 of each `.pub` file. Compare it with `sha256sum <kid>.pub` on your
host.

In the PR description, fill in the template's row for each kid: the host's
card count, the labels it will run, and the most runners it will have at once
(for a rotation, also the kid it replaces). sglang uses it to set the labels and
runner limit each key gets in the broker. These are ceilings only: which label
each runner takes is set by `RUNNER_LABEL` on the host. If a host needs a lane
that isn't in your `labels.txt` yet, add it in the same PR (see
[Changing your labels](#changing-your-labels)). An sglang code owner then
approves and merges.

**Never commit a `.key` file.** If you do, the key is exposed even after you
delete it: close the PR, tell sglang, and generate a new pair under a new kid.

### 4. Wait for "live", then start the loop

After merging, sglang syncs the key to the broker and deploys it. sglang will
tell you when it's live and give you the broker's base URL (`BROKER_URL`, e.g.
`https://<broker>`, no path) and your labels. Requests before that return
`401 unknown kid`.
Once it's live, `key_labels` in [Checking the queue](#checking-the-queue)
shows the labels each key may register.

## Changing your labels

`vendors/<vendor>/labels.txt` lists the runner labels your keys may register,
one per line. Name a lane by what a job needs (`mlu-1card`, `mlu-8card`), not
by PR vs nightly, unless nightly needs machines of its own. CI checks that each
label starts with `<vendor>-`, is lowercase `[a-z0-9._-]`, and isn't one of
GitHub's own labels (`self-hosted`, `linux`, `x64`, ...).

Change it by PR here. Adding a label takes an account in your
`submitters.txt`, as adding a key does; anyone may remove one. An sglang code
owner approves it, and the change works once sglang syncs it into the broker
and deploys. To move
jobs to a new lane without leaving any queued:

1. Add the new label here and wait until sglang says it's live.
2. Point the sglang workflow at it, by PR to sglang. Always name the group:
   `runs-on: {group: <vendor>-ci-runners, labels: <label>}`, so only your
   runners can take the job.
3. Switch `RUNNER_LABEL` on your hosts. To avoid a gap, run some slots on each
   label until the workflow change has merged.
4. Remove the old label here.

## Running the loop

`client/runner_loop.sh` runs one runner slot: fetch a config, run one job,
clean up, repeat. Run one instance per slot, e.g. with the systemd template in
`examples/systemd/`. Hosts need NTP: tokens live 60 s and the broker allows
30 s of clock skew.

### Two modes

| | Mode A: `RUNNER_MODE=container` (default) | Mode B: `RUNNER_MODE=host` |
|---|---|---|
| Where the job runs | on this host, in a fresh `docker run --rm` container each round | elsewhere: the runner is a thin client that hands the job to your scheduler (e.g. Jenkins) |
| PR code on this host | yes, inside the container | no; the workflow never checks out PR code here |
| Isolation | the runner process itself runs in the container, the key stays on the host. The loop refuses to start if `DOCKER_ARGS` mounts the key (or a directory above it), uses `--privileged`, or mounts the docker socket | each round copies a clean runner install to a new temp dir and runs it as `RUNNER_USER`, then deletes it. `RUNNER_USER` must differ from the loop's user and must not be able to read the key; the loop checks both |

### Environment

| Variable | Required | Meaning |
|---|---|---|
| `BROKER_URL` | yes | the broker's base URL from sglang, e.g. `https://<broker>`; the client adds `/v1/jit/<VENDOR>` |
| `VENDOR` | yes | your vendor name, e.g. `mlu` |
| `KID` | yes | this host's kid |
| `KEY_PATH` | yes | this host's private key (`<kid>.key`) |
| `RUNNER_LABEL` | yes | e.g. `mlu-1card`; each instance requests this one label |
| `RUNNER_HOST` | no | defaults to `hostname -s`; must equal the `<host>` in the kid |
| `RUNNER_MODE` | no | `container` (default) or `host` |
| `CLIENT` | no | path to `jit_client.py`; defaults to the script's directory |
| `RUNNER_IMAGE` | mode A | image with actions/runner and `./run.sh` in its workdir |
| `DOCKER_ARGS` | mode A, optional | extra `docker run` args: devices, read-only cache mounts |
| `RUNNER_DIR` | mode B | a clean actions/runner install, readable by `RUNNER_USER` |
| `RUNNER_USER` | mode B | the user the runner runs as; the loop's user needs passwordless `sudo -u $RUNNER_USER` |
| `MIN_JOB_SECONDS` | no | default 30; a runner that exits sooner counts as a failure |
| `BACKOFF_BASE_SECONDS`, `BACKOFF_MAX_SECONDS` | no | defaults 5 and 600 |

Several slots on one host can share its key and `RUNNER_HOST`; the broker adds
a random suffix to each runner name.

```bash
sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now sglang-jit-runner@slot1
```

## Checking the queue

To size your pool from demand, ask the broker how many of your jobs are
waiting for a runner. Any of your host keys works:

```bash
client/jit_client.py queue --url "$BROKER_URL" --vendor mlu \
  --kid mlu-host01-2026-11 --key /etc/sglang-ci/mlu-host01-2026-11.key
```

It prints the broker's JSON: `queued` (total), `by_label` (waiting jobs per
label), `oldest_queued_at` (UTC, or `null`), `key_labels` (the labels this key
may register, so the only ones this host should start runners for) and `jobs`
(the oldest 100, with repo, workflow, job name and run URL).

- The broker builds this from GitHub's job events, so treat it as a guide, not
  an exact count: a lost event can hide a job, or keep a finished one listed
  until it is 24 hours old.
- Each key may read it 30 times a minute. That budget is separate from
  requesting runners, so polling never slows the loop down.

## Rotating or removing a key

- **Rotate** (at the latest when sglang says a key expires within 30 days):
  generate a new pair under a new kid, PR it, wait for "live", switch
  `KID`/`KEY_PATH` on the host and restart, then PR deleting the old `.pub`.
- **Retire a host:** PR deleting its `.pub`. Anyone may open a deletion PR.
- **Key leaked or host compromised:** tell sglang right away. sglang revokes
  the kid on the broker within minutes, without waiting for a PR. A revoked kid
  never works again.

## Troubleshooting

| Response | Usually means |
|---|---|
| `401 unknown kid` | key not live yet, removed, or `KID` misspelled |
| `401 invalid token: ...` | the key doesn't match the registered `.pub`, or `VENDOR` is wrong |
| `401 token issued in the future` | host clock is ahead; check NTP |
| `401 key <kid> is revoked` | the kid was revoked; generate a new key under a new kid |
| `401 key <kid> expired on ...` | the key is past its 13 months (or the date sglang set); rotate it |
| `403 key <kid> does not belong to vendor ...` | `VENDOR` differs from the vendor in the kid |
| `403 source ip not allowed` | your egress IP isn't on your vendor's allowlist |
| `403 labels not allowed for key ...` | `RUNNER_LABEL` isn't one of this key's labels |
| `403 key <kid> may only register host ...` | `RUNNER_HOST` differs from the `<host>` in the kid |
| `404` | wrong `BROKER_URL`, or not this repo's client |
| `429 rate limit exceeded ...` | over this key's or your vendor's rate; the loop backs off by itself |
| `429 queue read limit exceeded for key <kid>` | more than 30 queue reads a minute from this key; poll less often |
| `429 host <host> already has N runners ...` | the host is at its runner limit; it clears as runners finish, and runners that never came online are removed within about 25 minutes |
| `429 vendor <vendor> already has N runners ...` | all your hosts together are at your vendor's runner limit; it clears the same way |
| `5xx` | broker or GitHub side; the loop retries, tell sglang if it persists |
