# Flexible reservation update

User now accepts standing, standing+seat, and waitlist in addition to either
seated class. Date, time, stations, one-adult scope, and manual payment remain
unchanged. Production watcher stays running from ../korail-watch while this
isolated worktree is developed and reviewed.

## Behavior

1. Prefer a confirmed travel entitlement (general, special, supported standing
   or standing+seat) whenever returned. No seat-location preference.
2. Collect waitlist candidates during a complete search pass; only attempt a
   waitlist after no immediate reservation succeeded. Do not join a waitlist on
   an incomplete/error scan. Exactly one active booking or queue entry.
3. A confirmed waitlist entry is not a confirmed ticket or unpaid seat hold.
   Persist it distinctly, send an accurate Telegram message, and monitor that
   same reservation for allocation. Do not create another booking while queued.
4. On allocation, atomically transition to held and send a fresh message with
   the actual payment deadline. Payment stays manual. On missing or uncertain
   queue record, stop with durable evidence; never silently rebook.
5. Preserve restart protection, uncertain-write reconciliation, process lock,
   HTTP throttling, credential redaction, and existing SQLite data. A legacy
   stored hold without a kind means seated, not waitlisted.

## Contracts

- Append `allow_waitlist`, `allow_standing`, `allow_mixed` bools to Trip (default
  false for backward compatibility). Active config and example enable accepted
  capabilities after verification.
- Append `waitlist`, `standing`, `mixed` bools to Train (default false), retaining
  existing positional raw argument compatibility.
- Append Hold.kind (default `seated`), validated values `seated`, `standing`,
  `mixed`, `waitlist`. Waitlist has no payable deadline; notifications must never
  instruct payment or imply a guaranteed seat before allocation.
- Provider.reserve accepts existing `general`/`special`, plus supported
  `waitlist`/`standing`/`mixed`. Validate intent kind against returned kind.
- Preserve actual provider status metadata from HTTP responses. `1102` is a
  queue request. `1202` may return a default mixed allocation on one PNR with
  two journeys; changing its connection station is a separate replacement flow.
  Confirm the default only when returned account records prove contiguous,
  same-train coverage of the entire requested route for one adult. A response
  code or partial leg alone is not proof of a whole-trip entitlement.
- Capabilities must be evidence-based. Do not invent request flags or label
  unsupported modes as enabled. If a requested mode needs unsupported/multi-step
  API behavior, surface it clearly and provide the official app handoff while
  continuing supported booking modes. Do not add speculative partial bookings.
- Standing-only is restricted to the app-evidenced general-code `13` and
  standing-code `11` combination, job `1101`, and `txtStndFlg=Y`. Confirm the
  returned whole-route reservation and one standing passenger; do not label a
  seated or partial response as standing. Mixed remains unsupported.
- `supported_modes` lists additional capabilities; general and special seated
  booking remain the baseline. Unsupported requests never create an intent.
- Waitlist creation includes the required options follow-up on the same PNR.
  If a crash or uncertain follow-up leaves an intent, history code `8` alone
  cannot prove completion: retain ambiguity for manual recovery rather than
  replaying either mutation. A persisted completed queue can be monitored.

## Ownership and verification

Sol core: domain.py, engine.py, core tests. Sol adapter: korail.py and adapter
tests; research exact standing/mixed and standby follow-up wire behavior from
primary source before implementing. Sol ops: CLI/config/docs/tests, commits,
PR, CI and merge after independent exact-head reviews. Root: design/issue,
live deployment and verification. All changes stay in this worktree until
reviewed; never edit the running deployment in place during development.

Use offline regression tests for seated-vs-waitlist identity, complete-scan
priority, persistent queue state/restarts, allocation notification, missing
queue ambiguity, one-booking invariant, old state compatibility, and provider
request/response normalization. No speculative live test bookings/cancellations.
After merge, root stops the old worker, re-reads actual state, deploys using the
same state directory, enables accepted supported modes, then restarts once.

## Sources

- Pinned korail2 implementation: reserve(..., try_waiting=True), txtJobId=1102,
  has_general_waiting_list() uses h_wait_rsv_flg=9. Standing is hardcoded off.
- https://github.com/yakisoba0728/korail-mobile-api/blob/main/docs/MUTATION_HANDOFF.md
  documents standby follow-up reservationWait and mixed booking's second stage.
- https://smart.letskorail.com/ebizmw/mwQna.do documents queue allocation and
  subsequent payment. Always use provider deadline rather than hardcoded FAQ
  windows, especially during holidays.
