# Korail Watch

Korail Watch is a small macOS-first Python CLI that searches every configured
station pair and holds the first eligible seated train for one adult. It never
pays, cancels, books standing room, or creates more than one hold.

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

`configure` prompts without echo and writes credentials with mode `0600` to
`~/Library/Application Support/korail-watch/credentials.toml`. Credentials are
never accepted as command-line arguments. Do not copy that file into the
repository or share it in logs, issues, or screenshots. Telegram values may be
left blank for `check`, but both are required for `notify-test` and armed use.

The committed example is intentionally nonsecret:

```toml
[trip]
date = "2026-09-24"
start = "12:00"
end = "18:00"
departures = ["서울", "용산", "수서"]
arrivals = ["대전", "서대전"]
adults = 1

[provider]
interval = 5.0
```

Do not substitute another station silently. In particular, 광명 and 수원 are
not part of this trip. If the unofficial API cannot expose 수서 service, those
exact searches return no trains; the watcher never substitutes another station.

## Use

Start with the offline demo, then make one credentialed read-only check:

```sh
korail-watch demo
korail-watch check --config trip.toml
korail-watch notify-test
```

`check` covers 42 route/hour targets at no less than five seconds between
requests, plus login and reconciliation, so a complete run takes several
minutes. Its final line is the termination outcome; `not-found` means the full
configured window was checked, not that the command hung.

No live reservation is attempted until `--arm` is present:

```sh
caffeinate -i korail-watch watch --arm --config trip.toml
```

The armed command logs in, sends a Telegram preflight message, and then starts
watching. `caffeinate -i` prevents idle system sleep while the terminal process
runs; closing the terminal or sleeping/restarting the Mac still stops it. A
finite foreground process is deliberate: do not configure launchd `KeepAlive`
or an unconditional restart, because success and security blocks must remain
stopped.

After `check` and `notify-test` work, the included launchd helper can start the
watcher at login. Installation repeats the read-only check before loading
anything; it refuses blocked, ambiguous, or failed results. It uses
`KeepAlive=false`, so a successful hold or security stop is not restarted.

```sh
./scripts/launchd.sh install "$PWD/trip.toml"
./scripts/launchd.sh uninstall
```

Inspect durable local state at any time:

```sh
korail-watch status
```

When a hold succeeds, open the official Korail app immediately and pay before
the provider's actual deadline shown in the notification. If the deadline is
unavailable, check the app immediately. The program never invents a payment
window and never pays for you.

## Recovery and safety

State is stored under
`~/Library/Application Support/korail-watch/state`. A crash or uncertain
reservation response leaves an unresolved intent and stops new reservations.
Run `korail-watch status`, then inspect reservations and tickets in the official
Korail app before taking further action. Do not delete state merely to restart:
an ambiguous request may already have created a hold. A notification failure is
also not permission to reserve again.

The watcher uses one authenticated session, at least five seconds between
requests, bounded retry behavior, and no parallel accounts, proxies, queue
bypass, or automatic retries of uncertain reservation writes. It cannot
guarantee availability, outcompete other users, or promise uninterrupted API
access. Offline tests pass no credentials and contact neither Korail nor
Telegram. Credentialed live acceptance has not yet been verified.

## Development

```sh
python -m unittest discover -v
```

CI runs the offline suite on Python 3.11 and 3.13. See [DESIGN.md](DESIGN.md)
for invariants and research, including the
[original `korail2` project](https://github.com/carpedm20/korail2), the
[Korail Chuseok notice](https://info.korail.com/info/selectBbsNttView.do?bbsNo=199&key=911&nttNo=27180),
and the [Telegram `sendMessage` API](https://core.telegram.org/bots/api#sendmessage).

## License

[MIT](LICENSE)
