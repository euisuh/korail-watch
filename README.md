# Korail Watch

Korail Watch is a small macOS-first Python CLI that searches every configured
station pair for one adult. It prefers an immediate travel entitlement and joins
one waitlist only after a complete pass finds no immediate option. It never pays,
cancels, or creates concurrent unpaid bookings.

This project uses Korail's unofficial mobile interface through a pinned
[`korail2` fork](https://github.com/dhfhfk/korail2/tree/4b134266fff097ea0fd54e9f760cb128b6c8f878).
It is not affiliated with or supported by Korail. The inherited DynaPath
headers provide compatibility, not a guarantee of access. Korail can change or
block the interface at any time.

## Install on macOS

Python 3.11 or newer and Git are required. With
[`uv`](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/euisuh/korail-watch.git
cd korail-watch
uv venv --python 3.11
uv pip install -e .
cp trip.example.toml trip.toml
source .venv/bin/activate
korail-watch configure
```

Before any network check or armed run, edit `trip.toml` for your own future
travel date, exact allowed stations, and time window. The watcher supports one
adult per booking; example dates and routes are illustrative, not defaults for
every user.

`configure` prompts without echo and writes credentials with mode `0600` to
`~/Library/Application Support/korail-watch/credentials.toml`. Credentials are
never accepted as command-line arguments. Do not copy that file into the
repository or share it in logs, issues, or screenshots. Telegram values may be
left blank for `check`, but both are required for `notify-test` and armed use.

The configuration shape is intentionally nonsecret:

```toml
[trip]
date = "2099-12-31" # replace with your future travel date
start = "09:00"
end = "12:00"
departures = ["서울"]
arrivals = ["대전"]
adults = 1
allow_waitlist = true
allow_standing = true
# Unsupported by the pinned provider; true emits an official-app handoff warning.
allow_mixed = false

[provider]
interval = 5.0
```

Departure and arrival lists are exact allowlists. If the unofficial API cannot
expose a configured service, that search returns no trains; the watcher never
silently substitutes another station.

The pinned provider supports general and special seats plus waitlist and
standing-only. Standing+seat is an accepted preference, but its full-route
multi-stage handling is not yet verified; requesting it prints an explicit
warning and directs you to the official Korail app while supported modes
continue. No request flag or partial booking is invented for an unsupported mode.
Standing-only is attempted only for the provider's exact standing availability
markers, and authoritative account readback must confirm exactly one passenger
on the exact train. A standing result stays standing; an exact one-seat result
is recorded as seated and is never relabeled. The request path is offline-tested
but has not produced a live reservation in this release.

## Use

Start with the offline demo, then make one credentialed read-only check:

```sh
korail-watch demo
korail-watch check --config trip.toml
korail-watch notify-test
```

`check` covers every configured station pair and hourly search window at no less
than five seconds between requests, plus login and reconciliation. Target count
and runtime therefore depend on your configuration. Its final line is the
termination outcome; `not-found` means the full configured window was checked,
not that the command hung.

No live reservation is attempted until `--arm` is present. The default remains
single-result mode and stops after the first hold or ticket; a waitlist is
monitored for allocation:

```sh
caffeinate -i korail-watch watch --arm --config trip.toml
```

Continuous mode keeps watching until the trip cutoff, including after payment or
a safely verified missed payment deadline:

```sh
caffeinate -i korail-watch watch --arm --continuous --config trip.toml
```

It allows at most one active unpaid hold or waitlist at a time. Paid tickets are
preserved and excluded while the watcher looks for a different train; there is
no departure-time ranking. An unpaid hold is archived and searching resumes only
after its provider deadline plus a 60-second grace and two complete account
snapshots, at least 30 seconds apart, prove the known PNR absent with no
conflicting unpaid record. Missing deadlines, read errors, identity conflicts,
or ambiguous state stop new writes rather than guessing.

A paid journey may contain distinct validated segments that share one provider
sale reference. Continuous mode retains every paid segment on the requested date
and will not create a booking that exactly matches one or overlaps it on the same
validated train service. This is duplicate protection only: it does not book
transfers, mark another hold paid, clear durable state, pay, or cancel anything.
Malformed, conflicting, duplicate, truncated, or incomplete paid records still
stop safely for review.

The one-unpaid limit is enforced from refreshed account snapshots, not a
server-wide lock against simultaneous actions in the official app. Pause the
watcher before creating reservations manually or changing trips; paying the
currently monitored hold is supported. Unexpected additional unpaid records,
authentication blocks, or unknown state stop the watcher for manual review.
Continuous mode never turns these safety stops into blind restarts.

The armed command logs in, sends a Telegram preflight message, and then starts
watching. `caffeinate -i` prevents idle system sleep while the terminal process
runs; closing the terminal or sleeping/restarting the Mac still stops it. Press
Ctrl-C to stop a foreground watcher. A finite process is deliberate: do not
configure launchd `KeepAlive` or an unconditional restart, because security and
ambiguous-state stops must remain stopped.

After `check` and `notify-test` work, the included launchd helper can start the
watcher at login. Installation repeats the read-only check before loading
anything; it refuses blocked, ambiguous, or failed results. It uses
`KeepAlive=false`, so any process exit is not automatically restarted. Omit the
third argument for legacy single-result behavior, or install continuous mode
explicitly:

```sh
# Choose one installation mode:
./scripts/launchd.sh install "$PWD/trip.toml"
# or:
./scripts/launchd.sh install "$PWD/trip.toml" --continuous
```

Manage that saved installation later:

```sh
./scripts/launchd.sh pause
korail-watch status
./scripts/launchd.sh resume
./scripts/launchd.sh uninstall
```

`pause` persistently disables the installed per-user service before unloading it,
so it remains disabled across logins and reboots. It keeps the saved plist,
configuration, credentials, durable state, logs, and archives. It cannot stop a
separate foreground watcher; press Ctrl-C in that process. Repeating `pause`
when the service is already unloaded is safe.

`resume` requires the saved installation, enables it, and requests startup with
the same configuration path and single-result or continuous mode. Before
resuming, inspect `korail-watch status` and the official app, and confirm no
foreground watcher is running; never launch a second process. It does not kill
or replay an already active attempt, and a startup request is not a guarantee of
a healthy run. Keep the Mac awake and online, then inspect status and the private
log again. After changing `trip.toml`, run `install` again instead of `resume` so
the read-only preflight runs before the service is enabled. `uninstall` stops the
launchd watcher and removes only its generated plist.

Inspect durable local state at any time:

```sh
korail-watch status
```

`status` is read-only and remains available while a watcher holds the process
lock. A queue entry appears separately from a confirmed hold. While queued, the
watcher monitors that same reservation and never starts another booking. Missing
or uncertain queue state is treated as ambiguous and stops new attempts.

Sanitized diagnostics are written to the selected state directory as
`diagnostics.jsonl`, rotated at about 1 MiB with three backups. The directory is
mode `0700` and log files are mode `0600`, including after rollover. Records use
only bounded fields such as operation, stage, outcome, HTTP status, provider
code, exception class, and a random attempt correlation value. They never include
raw responses, exception messages, URLs, headers, credentials, passenger data,
PNRs, paid sale/refund references, or Telegram secrets. Do not publish the files.

Diagnostics initialize before provider login. Startup stops with a sanitized
error if the private file cannot be created; later write or rotation failures
emit at most a fixed warning and cannot undo a successful reservation. To inspect
locally while troubleshooting:

```sh
tail -f "$HOME/Library/Application Support/korail-watch/state/diagnostics.jsonl"
```

In continuous mode, status also retains paid-ticket exclusions and the archive
of expired holds with their verification evidence. These records prevent the
same journey or an uncertain expiry from creating a duplicate. Do not edit or
delete them to force a retry.

A waitlist notification does not mean a seat is allocated and never asks for
payment. When Korail allocates the queue entry, the watcher records a hold and
sends a new notification. Only then should you open the official Korail app and
pay before the provider's actual deadline. If the allocated hold has no deadline,
check the app immediately. The program never invents a payment window and never
pays for you.

## Recovery and safety

State is stored under
`~/Library/Application Support/korail-watch/state`. A crash or uncertain
reservation response leaves an unresolved intent and stops new reservations.
Run `korail-watch status`, then inspect reservations and tickets in the official
Korail app before taking further action. An empty app view alone is not proof
that an uncertain write failed. Pause before manual booking or changing the trip,
and keep the old state: do not delete the database or select a fresh state
directory to bypass a guard. A notification failure is also not permission to
reserve again. Transient Telegram failures retry with bounded backoff while
booking remains frozen. A non-retryable Telegram error stops the process but
preserves both the hold and pending notification for operator recovery.

Legacy state remains compatible: a stored hold without a `kind` is treated as a
seated hold. Continuous mode does not use expiry to resolve an ambiguous write or
unproven waitlist follow-up. Do not delete queue, hold, paid-exclusion, or archive
state to force another attempt.

Before attempting a deferred waitlist candidate, the watcher refreshes that exact
train and uses the new provider object. A stale or no-longer-eligible candidate is
skipped; newly immediate inventory follows the existing seat/standing path. A
transient read-only refresh backs off without writing an intent. Once a mutation
may have been dispatched, uncertainty or incomplete follow-up still stops safely.
Diagnostics cannot reconstruct the cause of incidents that occurred before this
logging existed.

If a top-level search, reservation-list read, or complete ticket read returns the
exact P058 expired-session code, the provider logs in again and retries that read
once with the new client. A second P058 or rejected login remains a safety stop.
Temporary login transport failures keep the existing read-only backoff behavior.
Session renewal never replays a reservation submission, waitlist follow-up, or
readback inside a reservation attempt, and it never clears an ambiguous durable
intent. Renewal events use the same sanitized private diagnostics fields
described above.

That routine read-session renewal is distinct from an authentication or security
block, malformed account state, or unresolved reservation write. Those conditions
still require review. Pausing and resuming neither repairs them nor clears their
durable guards; a resumed process may safely stop again for the same reason.

Safe automatic recovery at the mutation boundary is deliberately narrow: only an
initial connection-establishment timeout can be classified as not dispatched,
with redirects disabled and the transport's default retries set to zero. Generic
connection, read, or HTTP errors after possible dispatch—and any incomplete
waitlist follow-up—remain uncertain and stop for review. An unconfirmed outcome
is not proof that Korail rejected the request.

The watcher uses one authenticated session, at least five seconds between
requests, bounded retry behavior, and no parallel accounts, proxies, queue
bypass, or automatic retries of uncertain reservation writes. It cannot
book transfers, guarantee availability, outcompete other users, or promise
uninterrupted operation or API access. Offline tests pass no credentials and
contact neither Korail nor Telegram. Credentialed login, a read-only full-window
search, and Telegram were verified on 2026-09-17. Waitlist mutation/allocation
and standing or mixed booking have not been live-tested.

## Development

```sh
python -m unittest discover -v
```

CI runs the offline suite on Python 3.11 and 3.13. See [DESIGN.md](DESIGN.md)
for invariants and research, including the
[original `korail2` project](https://github.com/carpedm20/korail2), the
[mobile mutation handoff notes](https://github.com/yakisoba0728/korail-mobile-api/blob/main/docs/MUTATION_HANDOFF.md),
[Korail Chuseok notice](https://info.korail.com/info/selectBbsNttView.do?bbsNo=199&key=911&nttNo=27180),
and the [Telegram `sendMessage` API](https://core.telegram.org/bots/api#sendmessage).

## License

[MIT](LICENSE)
