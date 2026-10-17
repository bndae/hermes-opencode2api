# hermes-opencode2api

Hermes Agent plugin that runs the [opencode2api](https://github.com/6Kmfi6HP/opencode2api)
relay as a **child of the Hermes process** — so you can use OpenCode Zen free
models (`deepseek-v4.1-flash`, `glm-5.3-flash`, `mimo-v2.6-flash`, and more)
from any OpenAI-compatible client with **nothing installed outside Hermes**.

- No systemd service, no `/usr/local` writable, no `~/.local/bin` script.
- The relay binary is downloaded at first start from the pinned upstream
  release and verified against the release's `checksums.txt` (SHA-256).
- The relay starts with Hermes and is killed with Hermes (graceful `atexit`
  plus a kernel-side `PR_SET_PDEATHSIG` coupling on Linux, `CREATE_NO_WINDOW`
  process handling on Windows) — a hard kill of Hermes cannot orphan it.
- The gateway, cron, kanban and `hermes send` services never spawn or own the
  relay, so it cannot outlive your session.

## Models exposed

The relay exposes whatever the upstream OpenCode Zen free tier serves on the
day of the pinned release (verified list for v0.14.30):

`deepseek-v4.1-flash`, `glm-5.3-flash`, `big-pickle`, `mimo-v2.6-flash`,
`nemotron-3.5-super-vl`, `nemotron-3.5-lightning`, `longcat-2.5-preview`,
`space-bunny`, `beam`, `jev-1.13`.

## Requirements

- Hermes Agent >= 0.22.0
- Outbound network (first start downloads the relay binary; later starts use
  the cached copy)

## Install

```bash
hermes plugins install <your-name>/hermes-opencode2api
hermes plugins enable opencode2api
```

Then register the model route once in `~/.hermes/config.yaml`:

```yaml
custom_providers:
  - name: opencode2api
    base_url: http://127.0.0.1:8000/v1
    api_key: public
    model: deepseek-v4.1-flash
```

> If you already have a `custom_providers:` block, just add this one entry to
> the existing list instead of a second block.

Use the models:

```bash
hermes chat --provider custom:opencode2api -m deepseek-v4.1-flash
hermes chat --provider custom:opencode2api -m glm-5.3-flash
```

`/model custom:opencode2api:glm-5.3-flash` works in CLI, TUI and Desktop
sessions the same way.

## Installed state

The plugin does not touch anything outside the Hermes tree:

| File | Purpose |
|---|---|
| `$HERMES_HOME/plugins/opencode2api/` | The plugin itself |
| `$HERMES_HOME/plugin-data/opencode2api/runtime/<version>/opencode2api` | Pinned relay binary (SHA-256 verified) |
| `$HERMES_HOME/plugin-data/opencode2api/config.json` | Relay config |
| `$HERMES_HOME/plugin-data/opencode2api/logs/opencode2api.log` | Relay logs, rotated |

Set `OPENCODE2API_PLUGIN_DATA` to relocate the data directory.

## How the lifecycle works

- On plugin load (an interactive Hermes process only — the gateway and cron
  services are excluded by design) the plugin spawns the relay in a keeper
  thread as a child of the Hermes process.
- The spawn is non-blocking; a `pre_api_request` hook guarantees the relay is
  ready right before any request routed to `custom:opencode2api` fires, and
  revives the relay if it died mid-session.
- When the Hermes process exits, `atexit` terminates the owned relay. On
  Linux the child is additionally coupled with `PR_SET_PDEATHSIG(SIGTERM)`,
  so a SIGKILL of Hermes still takes the relay down.

## Security notes

- The relay listens on **all interfaces** — the upstream binary has no
  bind-address flag. If your machine is reachable in an untrusted LAN, block
external access with a firewall rule, e.g. (Fedora):

```bash
for port in 8000 8001; do
  firewall-cmd --permanent --zone=<your-zone> \
    --add-rich-rule="rule family=\"ipv4\" source address=\"127.0.0.0/8\" invert=\"true\" port port=\"$port\" protocol=\"tcp\" reject"
  firewall-cmd --permanent --zone=<your-zone> \
    --add-rich-rule="rule family=\"ipv6\" source address=\"::1\" invert=\"true\" port port=\"$port\" protocol=\"tcp\" reject"
done
firewall-cmd --reload
```

The free tier is authenticated by the literal `public` bearer inside your own
config — it is not a secret, just a tier selector. Treat the relay as
unauthenticated on any network it can be reached from.

## Disclosure

- At first start the plugin performs one HTTPS GET to `github.com` (release
  checksums + archive for the pinned version) — afterwards everything is
  served from the Hermes tree.
- Model usage send network traffic through the relay to the upstream
  `opencode.ai` service; rely on any free-tier terms of service upstream
  publishes.
- The plugin runs a long-lived local background process (the relay, on port
  8000) as a child of Hermes.
- The plugin stores nothing outside `$HERMES_HOME` and has no telemetry.

## Troubleshooting

- **"bind: address already in use"** in the relay log — the relay is already
  running (adopted) or an older copy of the proxy still occupies port 8000:
  stop that copy first.
- **First request hangs ~10-20 s** — cold start: the relay registers an
  upstream session and downloads the model catalog once; later starts are
  fast (catalog is cached in the plugin data dir).
- Relay log: `$HERMES_HOME/plugin-data/opencode2api/logs/opencode2api.log`.

## Developing

```bash
cd hermes-opencode2api
python3 -m unittest discover -s tests   # contract tests, stdlib only
hermes plugins validate /path/to/hermes-opencode2api
```

The layout matches the [plugin developer guide](https://hermes-agent.nousresearch.com/docs/developer-guide/model-provider-plugin).
To submit to the Hermes plugin catalog, follow the
[submission rules](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission):
publish this repository, then open a PR adding `plugin-catalog/opencode2api.yaml`
to `NousResearch/hermes-agent`, pinned to a full commit SHA.

## Attribution

Plugin code: MIT (see [LICENSE](LICENSE)).
The relay itself is the upstream project [6Kmfi6HP/opencode2api](https://github.com/6Kmfi6HP/opencode2api)
— this plugin does **not** redistribute it: the binary is downloaded from the
official upstream release at runtime and verified against the upstream
`checksums.txt`. Respect the upstream terms of service; only use it where you
are authorized.
