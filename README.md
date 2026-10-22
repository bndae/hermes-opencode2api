# hermes-opencode2api

Hermes Agent plugin that runs the [opencode2api](https://github.com/6Kmfi6HP/opencode2api)
relay as a **child of the Hermes process** — so you can use OpenCode Zen free
models (`deepseek-v4.1-flash`, `glm-5.3-flash`, `mimo-v2.6-flash`, and more)
with **nothing installed outside Hermes**.

- No systemd service, nothing written outside the Hermes tree, no `~/.local/bin`
  script.
- The relay binary is downloaded at first start from the pinned upstream
  release and verified against the release's `checksums.txt` (SHA-256).
- The relay starts with Hermes and is killed with Hermes (graceful `atexit`
  plus a kernel-side `PR_SET_PDEATHSIG` coupling on Linux, `CREATE_NO_WINDOW`
  process handling on Windows) — a hard kill of Hermes cannot orphan it.
- The gateway, cron, kanban and `hermes send` services never spawn or own the
  relay, so it cannot outlive your session.

## Models exposed

The relay exposes whatever the upstream OpenCode Zen free tier serves on the
day of the pinned release (verified list for v0.14.38):

`deepseek-v4.1-flash`, `glm-5.3-flash`, `big-pickle`, `mimo-v2.6-flash`,
`nemotron-3.5-super-vl`, `nemotron-3.5-lightning`, `longcat-2.5-preview`,
`minimax-m3.1-flash`, `jev-1.13`, `beam`.

## Requirements

- Hermes Agent >= 0.22.0
- Outbound network (first start downloads the relay binary; later starts use
  the cached copy)

## Install

The plugin lives in this repository and installs straight from it:

```bash
hermes plugins install bndae/hermes-opencode2api
hermes plugins enable opencode2api
```

Enable takes effect on the next Hermes start (restart the desktop app or start
a new `hermes chat` session). That is the only install step.

## Updating

```bash
hermes plugins update opencode2api
```

That pulls the newest plugin revision. The relay binary is pinned per plugin
revision: on the next Hermes start after an update the plugin sees the new pin,
downloads that upstream release into
`$HERMES_HOME/plugin-data/opencode2api/runtime/<version>/` (SHA-256 verified
against the release's `checksums.txt`) and starts it. The previous
`runtime/<old-version>/` directory is left untouched and can be deleted.

## Zero-config model route

On first load the plugin itself adds the `custom:opencode2api` route to your
`~/.hermes/config.yaml` (a `custom_providers` entry pointing at the local
relay, `api_key: public`, the verified model list, `models_discovered: true`).
It writes only this one entry — idempotently, never touching anything else, and
never from the gateway/cron services.

So after the restart just pick the model:

```bash
hermes chat --provider custom:opencode2api -m deepseek-v4.1-flash
hermes chat --provider custom:opencode2api -m glm-5.3-flash
```

Or switch inside a session: `/model custom:opencode2api:glm-5.3-flash`.

Prefer configuring by hand? The equivalent block it writes to
`~/.hermes/config.yaml` (any existing route with the same name is left alone):

```yaml
custom_providers:
  - name: opencode2api
    base_url: http://127.0.0.1:8000/v1
    api_key: public
    model: deepseek-v4.1-flash
```

## What "running with Hermes" looks like

- `hermes` starts → the plugin spawns the relay binary from the Hermes tree;
  it does not block the start.
- Every request routed to `custom:opencode2api` is guarded: if the relay is
  down it is re-spawned in place (~1–2 s), so a turn never dies to a dead
  endpoint.
- You close Hermes → the relay closes with it (graceful `atexit` plus the
  kernel-side `PR_SET_PDEATHSIG` coupling; a SIGKILL of Hermes cannot orphan
  the relay).

## Installed state

The plugin never touches anything outside the Hermes tree:

| File | Purpose |
|---|---|
| `$HERMES_HOME/plugins/opencode2api/` | The plugin itself |
| `$HERMES_HOME/plugin-data/opencode2api/runtime/<version>/opencode2api` | Pinned relay binary (SHA-256 verified) |
| `$HERMES_HOME/plugin-data/opencode2api/config.json` | Relay config |
| `$HERMES_HOME/plugin-data/opencode2api/logs/opencode2api.log` | Relay logs, rotated |

Set `OPENCODE2API_PLUGIN_DATA` to relocate the data directory.

## Security notes

- The relay listens on **all interfaces** — the upstream binary ships no
  bind-address flag. If your machine is reachable from an untrusted LAN, block
  external access with a firewall rule, e.g. (Fedora):

```bash
for port in 8000 8001; do
  firewall-cmd --permanent --zone=<your-zone> \
    --add-rich-rule="rule family=\\\"ipv4\\\" source address=\\\"127.0.0.0/8\\\" invert=\\\"true\\\" port port=\\\"$port\\\" protocol=\\\"tcp\\\" reject"
  firewall-cmd --permanent --zone=<your-zone> \
    --add-rich-rule="rule family=\\\"ipv6\\\" source address=\\\"::1\\\" invert=\\\"true\\\" port port=\\\"$port\\\" protocol=\\\"tcp\\\" reject"
done
firewall-cmd --reload
```

The free tier is authenticated by the literal `public` bearer inside your own
config — it is not a secret, just a tier selector. Treat the relay as
unauthenticated on any network it can be reached from.

## Disclosure

- At first start the plugin performs one HTTPS GET to `github.com` (release
  checksums + archive for the pinned version); afterwards everything is
  served from the Hermes tree.
- Model usage sends traffic through the relay to the upstream `opencode.ai`
  service; respect the terms of service upstream publishes.
- On first load the plugin adds the `custom:opencode2api` route entry to
  `~/.hermes/config.yaml` when it is missing (idempotent; no other setting is
  ever read or written).
- The plugin runs a long-lived local background process (the relay on port
  8000) as a child of Hermes.
- Nothing is stored outside `$HERMES_HOME` and there is no telemetry.

## Troubleshooting

- **"bind: address already in use"** in the relay log — the relay is already
  running (adopted) or an older proxy copy still occupies port 8000; stop that
  copy first.
- **First request takes ~10–20 s** — cold start: the relay registers an
  upstream session and pulls the model catalog once; later starts are fast
  (the catalog is cached in the plugin data dir).
- Relay log: `$HERMES_HOME/plugin-data/opencode2api/logs/opencode2api.log`.

## Developing

```bash
cd hermes-opencode2api
python3 -m unittest discover -s tests   # contract tests, stdlib only
hermes plugins validate /path/to/hermes-opencode2api
```

The layout follows the [plugin developer guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins).
It also passes the [catalog submission](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission)
validation, so if you want to see it in the plugin catalog, open a PR adding
`plugin-catalog/opencode2api.yaml` to `NousResearch/hermes-agent`, pinned to a
full commit SHA.

## Attribution

Plugin code: MIT (see [LICENSE](LICENSE)).
The relay itself is the upstream project [6Kmfi6HP/opencode2api](https://github.com/6Kmfi6HP/opencode2api)
— this plugin does **not** redistribute it: the binary is downloaded from the
official upstream release at runtime and verified against the upstream
`checksums.txt`. Respect the upstream terms of service; only use it where you
are authorized. See [NOTICE](NOTICE).
