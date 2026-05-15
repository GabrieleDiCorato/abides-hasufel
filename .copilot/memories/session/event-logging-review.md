# Event-logging refactor review — session plan

User asked me to (a) update the plan with my review findings then (b) start
implementation. User then made themselves unavailable; instructed me to work
autonomously.

## Decisions made (locked in plan doc)

1. **Snapshot L1 cache (§3.5)** — loosest-mode-wins. If any registered
   `OrderBookSnapshotMemorySink` requests `every_update`, the publisher-side
   L1 cache is disabled. Bus computes effective mode at `start()`. Preserves
   §2 "no silent drops".
2. **Order schema (§3.9)** — single wide `ORDER_EVENT` schema with
   `order_kind: int8` discriminator (LIMIT=1, MARKET=2, STOP=3). All
   subclass-specific fields nullable.
3. **Bare-agent `bus` attribute** — `Agent.__init__` sets
   `self.bus = _NULL_BUS` (a module-level singleton with no-op publish
   methods). Kernel overrides at construction.
4. **`bus.drain()` placement** — Kernel main loop calls `bus.drain()` after
   every `wakeup`/`receive_message` return, before popping the next event
   from `event_queue`.

## Blockers from review (must fix in plan doc)

- §2 no-op signature has 6 args including `seq` — should be 5 (producer call
  signature). §3.4 is correct. Fix §2.
- §3.2 wire-tuple block presented as call signature — clarify split.
- §3.10 `bus_format_version` classified internal but consumers persisting
  Parquet need it — promote to public.
- Drain placement (above) — pin in §3.1 / §4.

## Implementation plan (this session)

### Phase 1 — quick wins (no architecture)

a. Vectorize `parse_logs_df` (target ≥1.3× — revised down from plan's 1.5×
   per review finding §1).
b. Add try/except + temp-file-then-rename around terminal `LogWriter` calls.
c. Honour `skip_log` in `Kernel.write_summary_log` (one-line bugfix).

Each is independent. Commit separately. Run pre-commit before each commit.

### Out of scope for this session

- Phase 2+ (bus, schema registry, agent migration) — needs explicit user
  approval on the four locked-in decisions above before coding.
