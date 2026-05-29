# IB Gateway auto-login (IBC + launchd + Keychain)

Keeps IB Gateway logged in and supervised so the live loop can connect on
port 4002 unattended, **without ever storing a password in plaintext on disk**.

Chain: `launchd` → `gatewaystart-keychain.sh` → IBC → IB Gateway → Python loop.

- **launchd** (`~/Library/LaunchAgents/com.trend.ibgateway.plist`) — `RunAtLoad`
  + `KeepAlive`; relaunches IBC if it dies. Runs in the GUI session (Gateway is
  a Swing app and needs a logged-in desktop; the login keychain is unlocked
  there too).
- **`~/ibc/gatewaystart-keychain.sh`** — reads the IB username + password from
  the macOS login Keychain and exports them as `TWSUSERID`/`TWSPASSWORD`, then
  `exec`s `~/ibc/gatewaystartmacos.sh -inline`. If the Keychain item is missing,
  it falls through with blank creds so IBC shows the manual login window instead
  of crash-looping.
- **Two patches to IBC's shipped scripts** (re-apply after any IBC upgrade — an
  upgrade overwrites them):
  1. `~/ibc/gatewaystartmacos.sh` lines ~29-30 changed from `TWSUSERID=` /
     `TWSPASSWORD=` (which unconditionally blank out the env) to
     `TWSUSERID="${TWSUSERID:-}"` / `TWSPASSWORD="${TWSPASSWORD:-}"` so the
     wrapper's exported creds survive. **Without this, IBC launches on its
     no-credentials path and sits at the login form with empty fields.**
  2. The wrapper does `cd "$HOME"` + sets `TMPDIR` because launchd starts it with
     `CWD=/` (read-only), and `ibcstart.sh`'s `mktemp -u XXXXXXXX` generates its
     session-id name relative to CWD (otherwise: `mktemp: ... Read-only file
     system` and an empty `ibcsessionid`).
- **IBC 3.23.0** (`~/ibc`, config `~/ibc/config.ini`) — types the creds into the
  Gateway login form. `config.ini` keeps `IbLoginId`/`IbPassword` **blank**
  (no secret on disk), `TradingMode=paper`, API port `4002`,
  `AcceptIncomingConnectionAction=accept`, `AutoRestartTime=03:00 AM`.
- **Auto-restart** — daily 03:00 CT (= 04:00 ET, Gateway's `jts.ini` timezone is
  `America/Chicago`). Restarts in-process reusing the session token: no password,
  no 2FA, fully unattended.

## One-time setup: store the paper credentials in the Keychain

The password is entered interactively so it never lands in shell history or any
file. Run in **Terminal** (replace `YOUR_PAPER_USERNAME` with the IB login name):

```bash
security add-generic-password -U -a "YOUR_PAPER_USERNAME" -s ib-gateway-paper -T /usr/bin/security -w
```

It prompts twice (`password data for new item:` / `retype...`); type the paper
password — nothing is echoed.

Flags:
- `-a` — IB username, stored as the item's *account* (the wrapper reads it back; not secret)
- `-s ib-gateway-paper` — service name the wrapper looks up
- `-T /usr/bin/security` — lets the `security` tool read the item with **no GUI
  approval prompt** when launchd runs the wrapper
- `-w` — prompt for the password interactively (keeps it off the command line)
- `-U` — update in place if the item already exists (safe to re-run)

After storing, reload the agent (see below). The Gateway should auto-log-in with
no manual window.

## Verify / inspect the stored item

```bash
# show attributes (account/service); password is NOT printed
security find-generic-password -s ib-gateway-paper

# print ONLY the password (sanity check; avoid in shared terminals)
security find-generic-password -s ib-gateway-paper -w

# delete it (e.g. to re-create)
security delete-generic-password -s ib-gateway-paper
```

## Manage the LaunchAgent

```bash
# start / reload (reload = bootout then bootstrap; restarts Gateway)
launchctl bootout    gui/$(id -u)/com.trend.ibgateway 2>/dev/null
launchctl bootstrap  gui/$(id -u) ~/Library/LaunchAgents/com.trend.ibgateway.plist

# status
launchctl print gui/$(id -u)/com.trend.ibgateway | grep -E 'state|pid'

# stop (and keep it stopped)
launchctl bootout gui/$(id -u)/com.trend.ibgateway
```

Logs: `~/ibc/logs/launchd.log` (wrapper output) and
`~/ibc/logs/ibc-*_GATEWAY-*.txt` (IBC diagnostics, login state, auto-restart).

## Paper vs. live

- **Paper** — no 2FA, so Keychain auto-login is 100% unattended, including the
  weekly forced re-auth.
- **Live** — username/password still auto-fill from the Keychain (store under a
  separate service, e.g. `ib-gateway-live`, and set `TradingMode=live`), but
  IBKR requires an **IBKR Mobile 2FA tap** on each fresh/weekly login that cannot
  be scripted away. The daily 04:00 token-reuse restart stays hands-off.

> Note: IBC ultimately passes the password as a java command-line argument, so it
> is briefly visible in the local process table (`ps`) to other users on the box.
> Low risk on a single-user Mac; worth knowing before this carries to live.
