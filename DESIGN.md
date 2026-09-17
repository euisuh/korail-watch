# Korail Watch design

## Goal

Acquire one unpaid reservation for one adult on Thursday 2026-09-24, departing
12:00 through 18:00 inclusive, Asia/Seoul. Allowed departures: 서울, 용산, 수서.
Allowed arrivals: 대전, 서대전. Any direct train, general or special seated class,
plus evidence-backed waitlist, standing, or standing+seat modes explicitly enabled
in configuration. No transfers, payment, cancellation, multiple accounts, or
speculative extra bookings. The first valid entitlement or queue entry ends new
reservation attempts. Run locally on macOS.

## Approach

Small Python 3.11+ CLI. Reuse the korail2 fork currently used by the user's
reference project, pinned to commit
`4b134266fff097ea0fd54e9f760cb128b6c8f878` in `dhfhfk/korail2`.
The upstream fork supplies mobile API compatibility including DynaPath request
headers; this is an unofficial API, not a Korail-supported integration. Do not
invent additional evasion, proxy rotation, queue bypass, or parallel accounts.
Stop on authentication/security blocks; back off on transient failures and 429.
Credentialed login and a read-only full-window search were verified on
2026-09-17; no reservation mutation was used to test flexible modes. If the API
does not expose 수서, report unsupported/no results explicitly; never silently
substitute 광명.

Reliability advantage comes from covering all six allowed station pairs and the
whole time range, retaining the authenticated session, and acting immediately
when an eligible seat is returned. No claim to beat competing bots or guarantee
availability. Rotate routes and hourly search windows fairly so pagination does
not starve late trains. Apply one global request budget (default >= 5 seconds,
bounded jitter) at the HTTP session boundary, including library internal calls.
Honor Retry-After; no automatic HTTP mutation retries. Explicit sold-out allows
trying the other available class. Complete every read-only search pass before
joining a waitlist so an immediately bookable entitlement always wins. Transport
or unknown failure after reserve is an ambiguous write: reconcile reservations
and tickets, never blindly retry.

## Durable state

Stdlib SQLite plus local OS file lock, private data directory permissions.
Persist intent before reservation; persist confirmed hold before notification.
Crash/timeout with unresolved intent stops fresh reservations until account
reconciliation or deliberate operator recovery. An empty list is insufficient
proof that an ambiguous request failed. Existing matching paid ticket, unpaid
hold, or waitlist entry also stops new booking attempts. Persist waitlists
distinctly, monitor only that PNR for allocation, and atomically transition it to
a hold when allocated. A waitlist has no payable deadline; only an allocated hold
may instruct payment. Default mode stops at the first result. Explicit continuous
mode may archive a missed-deadline hold and resume only after deadline plus grace
and two complete, separated account snapshots prove its PNR absent. It preserves
paid tickets as exclusions and permits no more than one active unpaid hold or
waitlist while seeking alternatives through the trip cutoff.
The one-unpaid policy relies on refreshed account snapshots rather than a
provider-wide lock against simultaneous manual reservations. Unexpected extra
unpaid records, authentication blocks, or unknown state stop for operator review.
Telegram failures must not trigger another hold. Keep notification pending and
retry independently with bounded backoff. Include actual provider payment
deadline, or clearly say unavailable and request immediate app check. Never
invent a 10/20 minute deadline. No credentials in logs, CLI arguments, source,
issues, artifacts, or Git history.

## Module contract

`korail_watch/domain.py` (core owner):
- `Trip(date: str, start: str, end: str, departures: tuple[str,...],
  arrivals: tuple[str,...], adults: int=1, allow_waitlist: bool=False,
  allow_standing: bool=False, allow_mixed: bool=False)` ISO date and HH:MM
  times, strict validation, KST current-time filtering, helper `matches(train)`.
- `Train(key: str, date: str, departure: str, arrival: str, dep_time: str,
  arr_time: str, general: bool, special: bool, raw: object=None,
  waitlist: bool=False, standing: bool=False, mixed: bool=False)`.
- `Hold(reference: str, train: Train, deadline: str|None, price: int|None,
  paid: bool=False, kind: str="seated")`; kind is seated, standing, mixed, or
  waitlist; deadline is ISO-aware KST when known and absent for waitlist.
- Errors: `SoldOut`, `TransientError(retry_after: float|None=None)`,
  `BlockedError`, `AmbiguousReservation`.

`korail_watch/korail.py` (adapter owner):
- `KorailProvider(interval=5.0, timeout=15.0)` reads KORAIL_ID/KORAIL_PASSWORD
  from environment; lazy SDK import, `login()`.
- `search(trip, departure, arrival, after: str) -> list[Train]`; one page, HH:MM:SS
  cursor; all train types, sold-out included, exact original train cached in raw.
- `reserve(train, seat_class: str, adults: int) -> Hold`; classes include only
  evidence-backed general/special/waitlist/standing/mixed provider capabilities.
- The supported extras are waitlist and narrow standing-only. Standing requires
  the exact provider availability markers and authoritative one-passenger
  standing readback. Mixed remains unsupported and warns with an app handoff.
- `reservations() -> list[Hold]` and `tickets() -> list[Hold]` for reconciliation.
- Separate per-instance requests Session with timeout, throttling and HTTP error
  translation. Internal SDK prints suppressed. Preserve security-block signals.
- An exact P058 session-expiry response may renew login and retry a top-level
  search, reservation-list, or complete ticket read once with the fresh client.
  Never renew or replay a reservation submission, waitlist follow-up, or
  mutation readback; repeated P058 or rejected login remains terminal.

`korail_watch/notifier.py` (adapter owner):
- `TelegramNotifier()` reads TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID from env.
- `send(text: str) -> None`; urllib HTTPS POST, no token-bearing errors, timeout,
  429 Retry-After interpreted into TransientError. Telegram optional only for
  offline demo/read-only search; required/preflight for armed booking.

`korail_watch/diagnostics.py` (ops owner):
- `configure(state_dir: Path)` creates a private rotating
  `diagnostics.jsonl` before provider login (about 1 MiB, three backups).
- `event(event: str, **safe_fields)` accepts only bounded structured fields:
  operation, stage, outcome, HTTP status, provider code, exception class, and
  random attempt ID. It rejects raw text, URLs, payloads, headers, and identifiers.
- Startup configuration failure is sanitized and fatal before mutation; later
  logging failures never interrupt a successful provider mutation or state write.
- The diagnostics logger has no root propagation or third-party HTTP handlers.

`korail_watch/engine.py` (core owner):
- `run(trip, provider, notifier, state_dir: Path, *, armed=False,
  once=False, max_cycles=None, continuous=False) -> str` and
  `status(state_dir) -> dict`.
- Engine handles lock, SQLite intent/hold/outbox, reconciliation, fair cursors,
  sold-out alternatives, stop at trip cutoff. Injection of sleep/clock accepted
  if useful for deterministic tests. Provider owns all network throttling.
- Dry-run never calls reserve; --once performs bounded read-only coverage.
- Session renewal stays inside the provider's top-level read boundary; the
  engine adds no restart loop and never clears durable ambiguity state.
- Deferred waitlist candidates are refreshed exactly before intent persistence.
  Stale candidates skip safely and transient read-only refresh failures back off;
  possible mutation dispatch or incomplete follow-up remains ambiguous.
- Only an initial connection-establishment timeout with redirects disabled and
  default transport retries set to zero is proven not dispatched. Generic
  connection/read/HTTP errors after possible dispatch remain ambiguous.
- Define CLI-facing demo provider or expose `demo(state_dir)` if convenient.

`korail_watch/cli.py` (ops owner): argparse commands `check`, `watch --arm`,
`status`, `notify-test`, `configure`, `demo`; safe read-only default.
`watch --continuous` is explicit and watch-only; omitting it preserves legacy
single-result behavior. The launchd helper accepts the same optional third flag.
`configure` uses getpass and saves local protected credentials file outside
repo under ~/Library/Application Support/korail-watch, loaded by CLI only.
Configuration from committed trip.example.toml with nonsecret capability flags.
Requested unsupported modes warn explicitly and hand off to the official app;
they never trigger invented or partial provider calls.
Mac launchd install/uninstall helper optional if simple, avoid auto-running until
credentials verified. Prefer launchd finite process with no automatic restart
after success/security block; caffeinate covers idle sleep while running.

## Verification and delivery

unittest with fake provider covering time/station boundaries, whole-window
coverage, class fallback, rate limits, crash/timeout reconciliation, notification
failure, waitlist priority/allocation, legacy state, duplicate process lock,
past-date cutoff, and credential redaction. Offline
demo cannot contact Korail or Telegram. CI Python 3.11/3.13. Credentialed `check`
is read-only; no speculative test reservations. Public README explains setup,
state recovery, manual payment, macOS sleep caveat, limitations, references.
Sol agents implement, test, open PRs, independently review exact PR head, fix
findings, merge once green. Root owns design, public repo, issues, coordination.

## Research (2026-09-17)

- https://github.com/GeunSam2/korail_KTX_macro_telegrambot (reference, current
  dependency uses the pinned fork; no source copied without license review).
- https://github.com/carpedm20/korail2 (original API, last source commit 2024).
- https://github.com/lapis42/srtgo (support discontinued).
- https://info.korail.com/info/selectBbsNttView.do?bbsNo=199&key=911&nttNo=27180
  (Chuseok Sep 23-27; original booking payment deadline Sep 15, concessions Sep
  18; these are not deadlines for a newly obtained cancellation seat).
- https://core.telegram.org/bots/api#sendmessage
- https://github.com/yakisoba0728/korail-mobile-api/blob/main/docs/MUTATION_HANDOFF.md
  (standby follow-up and mixed reservation handoff; unsupported multi-step flows
  remain disabled until implemented and verified).
- https://smart.letskorail.com/ebizmw/mwQna.do (waitlist allocation and payment
  guidance; provider deadlines remain authoritative).

Credentialed login, a read-only full-window API search, and Telegram delivery
were verified on 2026-09-17. Waitlist mutation/allocation and standing or mixed
booking remain unverified live; tests must not create speculative reservations.
