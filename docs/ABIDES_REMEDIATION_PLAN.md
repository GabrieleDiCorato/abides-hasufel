# ABIDES Performance Remediation Plan

## Overview
This plan addresses validated performance and architectural bottlenecks in ABIDES, prioritized by impact and effort. Deep code review confirmed 6 of 9 originally proposed issues, uncovered additional issues, and reclassified priorities based on actual impact.

---

## Status Summary

| # | Issue | Status | Commit |
|---|-------|--------|--------|
| 1.1 | Latency matrix row aliasing | **DONE** | `5612701` |
| 1.2 | MessageBatch computation delay applied N times | **DONE** | `5612701` |
| 1.3 | `get_l1_bid_data()` returns wrong price level | **DONE** | `eeef5a6` |
| 2.1 | Order book O(N) insertion → O(log N) bisect | **DONE** | `6f5fed4` |
| 2.2 | Order cancellation O(N) scan → O(log N) bisect | **DONE** | `6f5fed4` |
| 2.3 | Subscription publishing iterates all symbols | **DONE** | `a74c4c8` |
| 3.1 | Redundant deepcopy in exchange agent order processing | **DONE** | `604b6f4` |
| 3.2 | `filter+lambda` → list comprehension in L2 data | **DONE** | `7cf2320` |
| 3.3 | `logEvent` deepcopy default `True` → `False` | **DONE** | `1377e72` |
| 4.1 | Subscription cancel mutates list during iteration | **DONE** | `f1a296a` |
| 4.2 | Subscription data structure (O(N) cancel lookup) | SKIPPED | Low impact; mitigated by 4.1 `break` fix |
| 4.3 | Global `pd.set_option` at module scope | **DONE** | `f1a296a` |
| 4.4 | `queue.PriorityQueue` → `heapq` (remove mutex) | **DONE** | `f1a296a` |
| 4.5 | Message ID counter thread safety | SKIPPED | `itertools.count` is GIL-safe; ABIDES is single-threaded |
| — | Oracle RNG policy | VERIFIED OK | Oracles use injected `random_state`, not global `np.random` |

---

## Phase 1: Correctness Bugs (P0)

### 1.1 Latency matrix row aliasing
- **File:** `abides-core/abides_core/kernel.py`
- **Bug:** `[[val]*N] * N` creates N references to the same inner list.
- **Fix:** List comprehension: `[[default_latency]*N for _ in range(N)]`

### 1.2 MessageBatch computation delay applied N times
- **File:** `abides-core/abides_core/kernel.py`
- **Bug:** Computation delay applied per message in a MessageBatch instead of once per batch.
- **Fix:** Moved delay computation outside the per-message loop.

### 1.3 `get_l1_bid_data()` returns wrong price level
- **File:** `abides-markets/abides_markets/order_book.py`
- **Bug:** Return used `self.bids[0]` (hardcoded) instead of `self.bids[index]` after skipping zero-qty levels.
- **Fix:** Return `self.bids[index]` and added bounds check.

---

## Phase 2: Critical Performance (P1)

### 2.1 & 2.2 Order book O(N) → O(log N) with bisect
- **File:** `abides-markets/abides_markets/order_book.py`
- **Problem:** Linear scan through price levels for insertion, cancellation, modification, and partial cancellation.
- **Fix:** Added `_sort_key()`, `_book_keys()`, `_find_price_level()` helpers using `bisect.bisect_left`. Refactored `enter_order`, `cancel_order`, `modify_order`, `partial_cancel_order` to use binary search.

### 2.3 Subscription publishing scoped to affected symbol
- **File:** `abides-markets/abides_markets/agents/exchange_agent.py`
- **Problem:** `publish_order_book_data()` iterated all symbols on every order event.
- **Fix:** Added `symbol` parameter; all 6 call sites pass the affected symbol.

---

## Phase 3: Memory & CPU Reduction (P2)

### 3.1 Redundant deepcopy elimination
- **File:** `abides-markets/abides_markets/agents/exchange_agent.py`
- **Removed:** 6 unnecessary `deepcopy()` calls on orders passed to `cancel_order`, `handle_market_order`, `partial_cancel_order`, `modify_order`, and `replace_order` (old order). The order book's internal copies or read-only access make these redundant.
- **Kept:** `deepcopy` for `handle_limit_order` (quantity mutated by `execute_order`), `replace_order` new order (mutated by `handle_limit_order`).

### 3.2 Book logging filter optimization
- **File:** `abides-markets/abides_markets/order_book.py`
- **Fix:** Replaced `list(filter(lambda x: x[1] > 0, [...]))` with list comprehension in `get_l2_bid_data` and `get_l2_ask_data`.

### 3.3 `logEvent` deepcopy default changed to False
- **Files:** `abides-core/abides_core/agent.py`, `abides-markets/abides_markets/agents/trading_agent.py`
- **Fix:** Changed `deepcopy_event` default from `True` to `False`. Added explicit `deepcopy_event=True` to the 5 `HOLDINGS_UPDATED` log calls (the only callers logging a mutable dict that's modified later).

---

## Phase 4: Thread Safety & Code Quality (P3)

### 4.1 Subscription cancel: mutation during iteration
- **File:** `abides-markets/abides_markets/agents/exchange_agent.py`
- **Bug:** `list.remove()` called inside `for ... in list` loop.
- **Fix:** Added `break` after `remove()` (at most one match expected).

### 4.3 Global `pd.set_option` removed
- **File:** `abides-markets/abides_markets/agents/exchange_agent.py`
- **Fix:** Removed `pd.set_option("display.max_rows", 500)` from module scope.

### 4.4 `queue.PriorityQueue` → `heapq`
- **File:** `abides-core/abides_core/kernel.py`
- **Fix:** Replaced thread-safe `queue.PriorityQueue` with plain `list` + `heapq.heappush/heappop`. Eliminates mutex acquire/release on every message operation in the single-threaded kernel.

---

## Verification
- 165 tests passing (159 original + 6 new regression tests)
- Test files added: `abides-core/tests/test_kernel.py`, updated `abides-markets/tests/orderbook/test_data_methods.py`
