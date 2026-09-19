# mExchange — Architecture Learning Guide

> 中文版： [`ARCHITECTURE_GUIDE.zh-CN.md`](ARCHITECTURE_GUIDE.zh-CN.md)

A guided path through this repository for someone **who does not know Rust** but wants to
understand **how the system is put together**.

You do not need to read Rust line-by-line. You need to be able to answer, for any component:
*what does it own, what does it talk to, over what protocol, and what happens when it fails?*

---

## How to use this guide

Three passes, in order. Do not skip Pass 1 — most confusion comes from reading code before
having the map.

| Pass | Goal | Sections | Time |
|------|------|----------|------|
| **1. Map** | Hold the whole system in your head | Part 1, Part 4, Part 6 | ~45 min |
| **2. Trace** | Follow one order end-to-end through real code | Part 5 (Trace A, B, C) | ~90 min |
| **3. Judge** | Form opinions about the design | Part 3, Part 7 | ~2 h |

Part 2 (Rust survival kit) is a reference — skim it once, come back when a symbol confuses you.
Parts 8–11 are a study plan, glossary and self-test.

---

# Part 1 — Orientation

## 1.1 What this system is

A **spot cryptocurrency exchange**: users deposit assets, place buy/sell orders, and the system
matches them. The only configured trading pair is `KCN/EUR` (`SYMBOL` env var, default in
`matching_engine_service/src/main.rs:113`).

It is a **microservice** system: 7 Rust components + 1 React app + a PostgreSQL database.
Each Rust component is a separate build unit ("crate") with its own `Cargo.toml` and its own
binary — the equivalent of a separate `package.json` + service per folder.

**Not implemented** (mentioned in `README.md` as the target design): risk engine, dedicated
settlement service, admin dashboard, Redis, market registry, event log/replay, redundancy.

## 1.2 The 60-second mental model

Think in **three planes**. Almost every architectural question is answered by asking
"which plane is this?".

```
CLIENT PLANE            EDGE                    HOT PATH (latency-critical)         SYSTEM OF RECORD
─────────────           ────                    ───────────────────────────         ────────────────

  Browser  ──HTTP/WS──► Gateway ──UDP+JSON────► matching_engine_service ──┐
  (React)               :3000    (orders)                │                │
                            │                           │ in-process     │
                            │ HTTP/JSON                 ▼                │
                            │                     matching_engine (lib)   │
                            │                     • BTreeMap book         │
                            │                     • price-time priority  │
                            │                           │                │
                            │◄──UDP+FlatBuffers─────────┘                │
                            │   (fills, book deltas)                     │
                            │                                            │
                            │ HTTP/JSON (settle, lock, cancel) ──────────┤
                            ▼                                            ▼
                        accounts :3001  ◄───── HTTP/JSON (settle) ──── Accounts PostgreSQL
                        • users, OTP/JWT                                     (users, balances,
                        • balances (available)                               ledger, orders,
                        • append-only ledger                                 trades, ohlcv)
                        • orders, trades
                            ▲
                            │ HTTP/JSON (proxy)
                            │
                        market_data :3002 ◄──WS (subscribes to Gateway) ──┘
                        • OHLCV candles
```

**The single most important idea:** the matching engine keeps the live order book **in RAM**,
while the accounts service keeps the **durable** record in PostgreSQL. There are therefore
**two sources of truth that must be kept consistent by an explicit protocol** (settle-before-
publish). Understanding that protocol is understanding the architecture — see Trace A and §7.2.

## 1.3 The components at a glance

| Component | Kind | Listens on | Talks to | Owns |
|---|---|---|---|---|
| `matching_engine/` | **Pure library** (no I/O, no async) | — | — | Order book data structures + matching algorithm |
| `udp_proto/` | **Pure library** | — | — | Wire format: framing, batching, heartbeats, FlatBuffers codec |
| `matching_engine_service/` | Service | TCP `:8080` (health), UDP `:9100` (orders in) | UDP → gateway `:9101`; HTTP → accounts | The mutable `OrderBook`, order lifecycle orchestration, book publishing |
| `gateway/` | Service (edge) | TCP `:3000` (HTTP + WebSocket), UDP `:9101` (events in) | UDP → ME `:9100`; HTTP → accounts, market_data | Client sessions, subscriptions, per-client event fan-out, proxying, auth delegation |
| `accounts/` | Service + library | TCP `:3001` | PostgreSQL | Users, OTP/JWT, balances, ledger, orders, trades — **the system of record** |
| `market_data/` | Service | TCP `:3002` | WS → gateway; PostgreSQL | OHLCV candle aggregation |
| `trading_bot/` | Service (client) | — | WS → gateway (`:3000/ws`) | Market-making strategies, liquidity generation |
| `frontend/` | SPA | `:80` (container), `:5173` (host) | Gateway HTTP + WS | Trading UI |
| `e2e-tests/` | Test harness (TypeScript) | — | Gateway, accounts, PostgreSQL | Full-flow integration tests |

## 1.4 Two transport planes — the core design decision

This is the repo's most distinctive choice, explained in `README.md:76-99`.

**Cold path — HTTP/JSON** for anything not latency-critical:
auth, balances, order *creation and fund locking*, settlement, OHLCV history.
Easy to debug, easy to retry, idempotent.

**Hot path — UDP + FlatBuffers** for order submission and market events:
`gateway → matching_engine_service` (orders) and back (fills, book updates).

Why UDP is defensible *here*: both processes are in the same datacenter/container network, the
payloads are small and independent, and the 3-way TCP handshake + head-of-line blocking add
latency the design is explicitly trying to avoid.

Why FlatBuffers: zero-copy reads, no per-message allocation, compile-time-checked schema
(`udp_proto/src/market_data.fbs`).

**The trade you are accepting:** UDP drops and reorders packets. The code does not implement
retransmission — it implements *detection* (sequence numbers, gap counters, stream state) and
leaves recovery to reconciliation. See `udp_proto/src/receiver.rs:60-108` (`StreamState`,
`GapInfo`, `ReceiverStats`).

> **Architecture takeaway:** the hot path is *lossy*, so everything that must not be lost
> (money movement) is deliberately done on the *cold path* over HTTP. That is why settlement is
> an HTTP call and not a UDP message.

## 1.5 Five patterns you will see everywhere

1. **Pure core, imperative shell.** `matching_engine` and `udp_proto` are libraries with no
   network, no database, no `async`. All I/O lives in the service crates. This is why the core
   is unit-testable and benchmarkable in isolation.
2. **The library/state split.** `matching_engine_service` owns one `Arc<RwLock<OrderBook>>`
   and is the *only* writer.
3. **Fan-out via broadcast channels.** One producer (UDP event receiver) → many consumers
   (WebSocket clients). See `gateway/src/state.rs` (`broadcast::Sender<MarketEvent>`).
4. **Shared-nothing services, shared database.** `accounts` and `market_data` both connect to
   the same PostgreSQL database. There is no database-per-service.
5. **Envelope + tagged union.** Every wire message is `{type: "...", ...}` (JSON) or a
   FlatBuffers `union` — one message shape, many event kinds.

---

# Part 2 — Rust survival kit for architects

You only need ~12 concepts. Each row: *what it is → why it matters architecturally → where to look*.

| Rust construct | Plain-English meaning | Architectural consequence | Example in repo |
|---|---|---|---|
| `Cargo.toml` | Package manifest (like `package.json`) | Declares dependencies; `path = "../matching_engine"` shows an **in-repo dependency** | `gateway/Cargo.toml` |
| `main.rs` | Program entry point | Presence of `main.rs` = "this is a runnable service" | all services |
| `lib.rs` | Library root | Presence of `lib.rs` **and** `main.rs` = "logic is testable without running the server" | `accounts/src/lib.rs` |
| `mod` / `pub` / `use` | Module system + visibility | `pub mod` = public API surface; the boundary of a component | `gateway/src/lib.rs` |
| `struct` | Record / data class | Domain objects: `Order`, `Fill`, `OrderBook` | `matching_engine/src/lib.rs:24` |
| `enum` | **Tagged union** (sum type), not just a constant list | This is how Rust models "one of N shapes" safely — you will see it for events, sides, statuses, errors | `MarketEvent` in `gateway/src/events.rs:39` |
| `Option<T>` | "maybe a value" (no `null`) | Forces handling of "market order has no price" | `price: Option<Price>` |
| `Result<T, E>` | "value or error" (no exceptions) | Every fallible call is visible in the signature — errors are values you must route | `fn new(...) -> anyhow::Result<Self>` |
| `async` / `await` + `tokio` | Cooperative concurrency runtime | Enables thousands of sockets on few threads; `tokio::spawn` = "run this concurrently" | `axum` handlers, `tokio::spawn` in `gateway/src/server.rs:169` |
| `Arc<T>` | Shared, reference-counted, immutable pointer | Lets many tasks share one object; combined with a lock for mutability | `Arc<UdpOrderSender>` |
| `RwLock` / `Mutex` | Reader-writer / exclusive lock | The **critical section**. `Arc<RwLock<OrderBook>>` = "one book, many possible readers, one writer" | `matching_engine_service/src/main.rs:23` |
| `serde` + `#[derive(Serialize, Deserialize)]` | Declarative JSON encode/decode | The contract between Rust structs and the JSON that crosses the wire, with no hand-written parser | `gateway/src/events.rs` |
| `trait` / `impl` | Interface + implementation | Enables pluggable behaviour | `MailService` in `accounts/src/mail/mod.rs` |
| `#[cfg(test)]` / `#[test]` | Conditional compilation + test marker | Unit tests live beside the code; `benches/` hold performance tests | `udp_proto/src/tests.rs`, `matching_engine/benches/` |

## 2.1 The two ideas that trip up non-Rust readers

**(a) `enum` is not a C-style enum.** In Rust an enum variant can carry data:

```rust
pub enum MarketEvent {
    Fill { price: Decimal, quantity: Decimal, .. },
    OrderBookSnapshot { bids: Vec<PriceLevel>, asks: Vec<PriceLevel> },
    OrderCancelled { order_id: OrderId, filled_quantity: Decimal },
}
```

Read this as: "a `MarketEvent` is exactly one of these shapes, and the compiler forces every
handler to consider all of them." That is why you see `match event { ... }` everywhere — it is
exhaustive pattern matching, the Rust analogue of a `switch` the compiler refuses to let you
under-specify. Every `match` you find is a *complete list of the cases the system believes
exist*. These enums are the best documentation in the repo. Start there.

**(b) Ownership, in two paragraphs.** Rust tracks which piece of code "owns" each value and
frees memory automatically when the owner goes away — no garbage collector, no manual `free`.
You cannot have two owners of the same value, so sharing requires explicit wrappers:
`Arc<T>` (shared read access, refcounted) and `RwLock<T>`/`Mutex<T>` (shared *mutable* access,
guarded). Architecturally this is a gift: **every `Arc<RwLock<...>>` in the codebase is a
declared shared-mutable state, i.e. a concurrency hotspot you can enumerate.** Grep for them and
you have found the system's coordination points.

Run these two greps — they are the fastest way to see the architecture:

```bash
grep -rn "Arc<RwLock\|Arc<Mutex\|tokio::sync::RwLock" --include=*.rs .
grep -rn "pub enum" --include=*.rs .
```

---

# Part 3 — Component deep dives

Read these in the order presented. Each ends with **"what to notice"**.

## 3.1 `matching_engine/` — the pure domain core

**Responsibility:** given an order, decide what it matches against, mutate the book, return the
resulting fills. Nothing else. No network, no database, no async, no clock, no logging.

**Public API** (`matching_engine/src/lib.rs`):

| Item | Meaning |
|---|---|
| `type OrderId = Uuid` | Orders are identified by UUID, generated *upstream* (by accounts), not here |
| `type Price = Decimal`, `type Quantity = Decimal` | Exact decimal arithmetic — floats are unacceptable for money (`lib.rs:7-9`) |
| `enum Side { Bid, Ask }` | Buy vs sell |
| `enum OrderType { Limit, Market }` | Limit = price-bound; Market = take whatever is there |
| `struct Order` | `id, side, order_type, price: Option<Price>, quantity, remaining_quantity` |
| `struct Fill` | `buy_order_id, sell_order_id, price, quantity` — a **match**, not yet a settled trade |
| `struct OrderResult` | `order_id, fills: Vec<Fill>, completed_orders: Vec<OrderId>` |
| `struct OrderBook` | The book itself |
| `add_limit_order(...) -> OrderResult` | Insert + match |
| `add_market_order(...) -> OrderResult` | Match only, never rests |
| `cancel_order(id) -> bool` | Remove |
| `restore_order(order)` | Put an order back — **used for rollback** |
| `best_bid / best_ask / spread / quantity_at_price` | Read-only views |
| `get_bids(n) / get_asks(n)` | Top-N aggregated price levels (for publishing) |

**Internal data structures** (`lib.rs:104-111`) — the heart of the design:

```rust
pub struct OrderBook {
    bids: BTreeMap<Price, PriceLevel>,   // sorted map, keyed by price
    asks: BTreeMap<Price, PriceLevel>,
    orders: HashMap<OrderId, Order>,     // O(1) lookup by id
}
```

* `BTreeMap` = a **sorted** map. Iterating gives prices in order, so "best price" is just
  `keys().next_back()` for bids and `keys().next()` for asks (`lib.rs:192-198`). No sorting pass.
* `PriceLevel { orders: VecDeque<Order>, total_quantity }` — a **FIFO queue per price**
  (`lib.rs:62-67`). `VecDeque` = double-ended queue; `push_back` for new orders, `pop_front` to
  consume. This *is* the "time" half of **price-time priority**: price is the BTreeMap key,
  time is the queue position.
* `HashMap<OrderId, Order>` = O(1) cancel/lookup, so cancelling doesn't require scanning the book.
* `total_quantity` is cached per level so publishing top-N levels is O(levels), not O(orders).

**Order lifecycle:** `add_limit_order` builds an `Order`, calls the private `match_order`
(which walks the *opposite* book from the best price, consuming FIFO queues and emitting `Fill`s,
mutating `remaining_quantity`), then — if anything is left — `add_order_to_book`.
If fully filled it is pushed into `completed_orders` instead (`lib.rs:122-142`).

Two details that matter commercially:
* **The trade executes at the resting order's price**, never the aggressor's limit price
  (`lib.rs` `match_order` reads the price from the level being consumed). A buyer bidding 105
  against a resting ask of 100 pays 100. That is standard price improvement and it is easy to
  get wrong.
* **`Fill` has no owner.** It carries only `buy_order_id` / `sell_order_id`, so the engine cannot
  detect self-trading — there is no self-trade prevention anywhere in this library. The test
  named `test_self_matching_prevented` only submits non-crossing orders, so it does not actually
  exercise self-matching.

**What to notice**
* `restore_order` exists specifically because the *service* may need to undo a match when
  settlement fails. The pure core exposes the primitive — **but nothing calls it.** A repo-wide
  grep for `restore_order` finds hits only inside `lib.rs` itself, so the rollback protocol is
  declared and unimplemented. Note also that if it *were* called it would re-queue at the **back**
  of the level, losing the order's original time priority.
* Market orders are never added to the book — an unfilled remainder is simply discarded by the
  core, and the *service* is responsible for informing accounts (`main.rs:316-333`). A partially
  filled market order therefore leaves no trace of the dropped remainder in `OrderResult`.
* **There is no `OrderStatus` type and no status field.** Status is implicit: resting = present in
  the `orders` map; fully filled or cancelled = absent. No timestamps, no sequence numbers, no
  account IDs on `Order` or `Fill`.
* **There is no validation** — no tick size, lot size, minimum notional, or negative/zero quantity
  check. `Decimal` is signed, so a negative quantity is accepted. The library has no error type at
  all: absence is `Option`, failure is `bool`.
* Despite the "high-performance" framing, this is **not allocation-free**: every submission
  allocates `Vec<Fill>`, `Vec<OrderId>` and a `Vec<Price>` snapshot of *all* opposite-side levels,
  and `add_order_to_book` clones the `Order`. The 5.2M orders/sec claim lives in
  `matching_engine/README.md` and is not reproduced by any artifact in this repo.
* The core has no notion of trading fees, user identity, or balance. It knows only order IDs.
  **Money is entirely outside its boundary.**

## 3.2 `udp_proto/` — the protocol library

**Responsibility:** turn messages into bytes and back, reliably enough to *detect* loss.

Two layers (`udp_proto/src/protocol.rs`):

**Layer 1 — framing.** A 24-byte `PacketHeader` (version, header length, message count,
`stream_id`, `packet_seq`, `first_msg_seq`) followed by N messages, each with a 4-byte
`MessageHeader` (`msg_type`, `flags`, `msg_len`). MTU is capped at 1400 bytes
(`MAX_MTU`, `protocol.rs:7`) to stay under typical Ethernet payload limits.

| `MessageType` | Value | Direction |
|---|---|---|
| `OrderNew` | `0x01` | gateway → ME |
| `OrderCancel` | `0x02` | gateway → ME |
| `OrderReplace` | `0x03` | *defined, unused* |
| `MatchEvent` | `0x10` | ME → gateway |
| `BookSnapshot` / `BookUpdate` | `0x11` / `0x12` | *defined; book data currently travels as `MatchEvent`* |
| `PositionUpdate` | `0x20` | *defined, unused (spot only)* |
| `Heartbeat` | `0x30` | both |
| `Control` | `0x40` | *defined, unused* |

**Layer 2 — payload.** FlatBuffers, schema in `udp_proto/src/market_data.fbs`, generated Rust in
`udp_proto/src/generated/market_data_generated.rs` (generated — do not read it; read the `.fbs`).

```fbs
union EventPayload { Fill, OrderBookSnapshot, OrderBookDelta, OrderCancelled, OrderFilled }
table MarketEvent { payload: EventPayload; }
root_type MarketEvent;
```

Note `struct Uuid { high: uint64; low: uint64; }` — UUIDs travel as two integers, not strings.
And note the schema comment (`binary.rs:80-83`): the **wire format uses `f64`** while the engine
uses exact `Decimal`. That conversion happens at the boundary
(`matching_engine_service/src/udp_transport.rs:200`). Market data is allowed to be approximate;
accounting is not.

Concurrency model (`udp_proto/src/sender.rs`, `receiver.rs`): a **dedicated OS thread** runs a
blocking socket loop and communicates with async code through `crossbeam-channel`. Batching:
messages accumulate for up to `max_batch_delay` (100 µs for orders) or until the MTU is full.
Stats are atomics (`SenderStats`, `ReceiverStats`) so they can be read without locking.

**What to notice**
* `stream_id` separates the two logical flows: `ORDER_STREAM_ID = 1`,
  `EVENT_STREAM_ID = 2` (`gateway/src/udp_transport.rs:53-54`).
* The receiver tracks `StreamState { Initializing, Active, Degraded, Down }` and counts
  `gaps_detected` / `total_gap_messages`. **Loss is measured, not repaired.**
* `MessageType` variants that are defined but unused are a map of the author's intent — cheap
  insight into where the design wants to go.

## 3.3 `matching_engine_service/` — the hot-path orchestrator

**Responsibility:** own the single live `OrderBook`, receive orders over UDP, run matching,
**settle synchronously**, publish events. This is where policy lives.

State (`main.rs:21-26`):

```rust
struct AppState {
    orderbook: Arc<RwLock<OrderBook>>,
    event_sender: Arc<UdpEventSender>,
    symbol: String,          // "KCN/EUR" — ONE symbol per process
}
```

Four concurrent activities (`main.rs:150-186`):
1. UDP order receiver task → `process_order_command`
2. Orderbook publisher task → every **100 ms** reads the book, computes deltas or a snapshot,
   sends over UDP
3. A settlement HTTP client (`settlement.rs`)
4. An axum server exposing only `GET /health` on `:8080`

**The critical sequence in `process_order_command` (`main.rs:223-383`)** — read this function
twice; it *is* the architecture:

```
1. write-lock the book, run add_limit_order / add_market_order, release the lock
2. for each fill: POST /internal/settle to accounts  (HTTP, up to 3 retries, 5 s timeout)
3. publish Fill events over UDP only for fills that settled
4. if a market order was only partially filled:
       POST /internal/cancel to accounts, then publish OrderCancelled
5. publish OrderFilled for each fully-filled order
6. if any settlement failed: log CRITICAL and continue  ← no rollback is actually performed
```

**What to notice (these are the interesting architectural findings)**
* **Settle-before-publish** is a deliberate ordering choice: clients and downstream consumers
  must never learn about a fill that isn't in the ledger yet. The comment at `main.rs:258`
  states it explicitly.
* The lock is dropped *before* the HTTP settlement call (`main.rs:252`). Good: the matching
  critical section is microseconds, not milliseconds.
* **Partial-failure policy is "log and continue."** `main.rs:283-295` logs
  `INCONSISTENCY: Orderbook shows fill but accounts not updated. Manual intervention required.`
  `restore_order` exists in the core library to support rollback, but nothing calls it in the
  live path. This is the single biggest correctness gap in the system — a great discussion topic.
* **Snapshot/delta publishing** (`OrderBookPublisher`, `main.rs:389-548`): a full snapshot every
  `SNAPSHOT_INTERVAL = 10` updates, deltas otherwise, top `MAX_LEVELS = 10` levels. The publisher
  diffs against its own previous view — so a dropped UDP packet desynchronises it only until the
  next snapshot (every 10 updates ≈ every second at 100 ms ticks). **This is the loss-recovery
  mechanism**, and it is intentionally cheap.
* `sequence` is incremented by the publisher but **not currently used by the gateway to detect
  gaps** — the field exists, the consumer ignores it. Worth verifying as an exercise.
* `OrderAccepted` is in the event enum but is **filtered out before transmission**
  (`udp_transport.rs:141-145`) because it isn't in the FlatBuffers union. The ordering in
  `main.rs` means it would be emitted *after* the fills anyway. Overlapping/vestigial concepts.

## 3.4 `gateway/` — the edge

**Responsibility:** be the only public entry point. Terminate client connections, authenticate
them (by delegating to accounts), fan out market data, and forward orders.

`GatewayServer` (`server.rs:26-40`) holds five pieces of shared state, which is a good summary
of everything the gateway does:

```rust
state: GatewayState,                                          // broadcast bus + client ids
order_sender: Arc<UdpOrderSender>,                            // hot path out
channel_manager: Arc<RwLock<ChannelManager>>,                 // subscriptions, users, bot control
orderbook_states: Arc<RwLock<HashMap<String, OrderBookState>>>, // per-symbol book mirror
proxy_state: ProxyState,                                      // HTTP clients to accounts/market_data
```

**Client protocol: JSON-RPC style over WebSocket** (`websocket.rs:17-24`):
`{"id": "...", "method": "...", "params": {...}}` with `{"id":..., "result":...}` or
`{"id":..., "error":{code, message}}` responses. Error codes are namespaced: standard JSON-RPC
(`-32601`, `-32602`) plus domain codes (`10000` unauthorized, `10001` order rejected,
`10002` cancel failed).

| Method | Auth | Effect |
|---|---|---|
| `public/auth` | — | Validates a JWT by calling accounts `GET /api/me`, binds `client_id → user_id` |
| `public/subscribe` | — | Subscribes to `book.<sym>`, `ticker.<sym>.<interval>`, `lwt.<sym>.<interval>` |
| `private/subscribe` | token | Subscribes to private channels |
| `unsubscribe` | — | Removes subscriptions |
| `private/place_order` | required | Creates the order in accounts (locks funds) → sends UDP order → replies with `order_id` |
| `private/cancel_order` | required | Cancels in accounts (unlocks funds) → sends UDP cancel |
| `private/register_bot` / `private/bot_status` | — | Bot control channel |

**Two distinct fan-out paths** — an important design detail:
* `GatewayServer::start_event_broadcaster` (`server.rs:181-283`) consumes the UDP event stream,
  folds it into per-symbol `OrderBookState`, and broadcasts `book.*`, `ticker.*`, `lwt.*`
  notifications to *subscribers of those channels*.
* Each connection spawns its own task (`websocket.rs:455-476`) that reads the same broadcast bus
  and forwards `OrderFilled` / `OrderCancelled` **only to the owning client** — private events
  must not leak to other users. Ownership is resolved through
  `ChannelManager::get_order_owner`, populated at `websocket.rs:778-780`.

**Order book replication** (`channel_updates.rs`): the gateway maintains its own
`OrderBookState` per symbol, applying snapshots (replace) and deltas (add/update/remove). This is
a **CQRS read model** — a denormalised projection built from the event stream, kept separate from
the authoritative book inside the matching engine.

**HTTP proxying** (`proxy.rs`, `server.rs:124-141`): the gateway forwards `/auth/*`, `/api/me`,
`/api/balances`, `/api/orders`, `/api/faucet/*`, `/api/ohlcv`, and — notably — `/internal/*`
to accounts. This keeps one origin for the browser (CORS is configured with explicit origins and
credentials, `server.rs:58-121`).

**What to notice**
* The gateway does **not** validate balances or creditworthiness; it forwards to accounts and
  lets accounts reject. The gateway is an adapter, not a policy owner.
* `validate_token` (`websocket.rs:1014-1030`) is an HTTP round-trip per auth event, not a local
  JWT verification. Correctness via single authority vs. latency/coupling trade-off.
* `ChannelManager` (`websocket.rs:310-427`) is one struct holding bot registration, auth mapping,
  order ownership and subscriptions — all guarded by one `RwLock`. That is a contention point and
  a candidate for splitting.
* `/internal/*path` being proxied publicly (`server.rs:134`) means unauthenticated internal
  endpoints — including `POST /internal/mint` — are reachable from the browser origin. See §7.2.

## 3.5 `accounts/` — the system of record

**Responsibility:** identity, money, orders, trades. If it isn't in this database, it didn't
happen. It is the only component that persists anything financial.

**Composition** (`accounts/src/lib.rs`, `main.rs`):
`AppState { pool: PgPool, jwt_secret: String, mail: MailService }` plus modules
`db`, `models`, `routes`, `mail`, `scheduler`.

**Route surface:**

| Prefix | Auth | Endpoints |
|---|---|---|
| `GET /health` | — | liveness |
| `/auth` | — | `POST /request-otp`, `/verify-otp`, `/signup`, `/refresh`, `/logout`, plus `/dev-login` when `ENVIRONMENT=development` |
| `/api/me` | Bearer JWT | current user |
| `/api/balances` | Bearer JWT | `GET /`, `POST /deposit`, `POST /withdraw` |
| `/api/orders` | Bearer JWT | `POST /` place, `GET /` list, `GET /:id`, `DELETE /:id` cancel, `GET /:id/fills`, `GET /trades` |
| `/api/faucet` | Bearer JWT | `POST /claim`, `GET /status` |
| `/api/ohlcv` | — | historical candles |
| `/internal` | **none** | `POST /settle`, `POST /cancel`, `POST /orders`, `DELETE /orders/:id`, `POST /mint` |

Auth middleware (`main.rs:180-221`): extracts `Authorization: Bearer <jwt>`, verifies with
`TokenService::verify_access_token`, loads the `User`, injects it into request extensions.
Passwordless: `POST /auth/request-otp` emails a 6-digit code; `POST /auth/verify-otp` exchanges it
for an access token + an httpOnly refresh cookie. Refresh tokens are stored **hashed** with
`revoked_at` for revocation (`001_create_users.sql`).

**Money model — the most important part, and the easiest to misread.**

The `balances` table has **two** money columns, `available` and `locked`, each with a
`CHECK (... >= 0)` constraint (`003_add_constraints.sql`). **Only `available` is ever written.**
`locked` is read by the balances API (`routes/balances.rs:77`) but no code path in `accounts/src/`
ever updates it — grep for `locked =` and you get nothing. It is a vestigial column kept alive only
by its CHECK constraint. Do not build a mental model on it.

The real mechanism is **the ledger**. "Locking" funds is implemented as a **negative
`entry_type = 'lock'` ledger entry that reduces `available`**, with `ledger.reference_id` pointing
at the order and `orders.lock_entry_id` pointing back at the entry. Unlocking appends a positive
`unlock` entry. So:

| Event | Ledger entries | Effect on `available` |
|---|---|---|
| Place a bid limit order | `lock` −(price × qty) of quote | decreases |
| Fill (buyer) | `unlock` +quote, `trade` −quote, `trade` +base | net: quote → base |
| Cancel | `unlock` +remaining | increases |
| Deposit / withdraw | `deposit` / `withdrawal` | ± |

The `ledger` table is the declared **source of truth for balances**, and it is **single-sided, not
double-entry**: each row is one user's delta with a running `balance_after`, with no chart of
accounts and **no exchange/system account**. Two consequences, both verifiable in `trade.rs`:
* When one side of a fill is a bot ("anonymous"), that side is **skipped entirely** — the trade
  therefore mints the base asset to the buyer and burns the quote debited from them. There is no
  fund-conservation invariant anywhere in the system.
* Fees are computed and stored on the `trades` row (`FEE_RATE = dec!(0.001)`) but **never posted to
  the ledger**. `EntryType::Fee` is defined and has a `Display` impl, yet no code in
  `accounts/src/` ever writes one.

Immutability *is* genuinely enforced by the database: migration 003 installs
`prevent_ledger_modification()` and a `BEFORE UPDATE OR DELETE` trigger on `ledger` that raises
`'Ledger entries cannot be modified or deleted'`. That is a strong invariant — **but read the
scheduler caveat below, which can disable it.**

`ledger.rs` exposes `append`, `append_in_tx`, `derive_balance`, `reconcile` — the balance is
*derivable* from the journal and reconcilable against the `balances` projection. Both helpers are
currently wired to nothing.

**Concurrency control:** every ledger mutation takes
`pg_advisory_xact_lock(compute_lock_key(user_id, asset))` before reading and updating the balance
(`ledger.rs:84-240`), and `Trade::settle` acquires locks for all distinct users in sorted order to
avoid deadlocks. That is how lost updates are prevented without `SERIALIZABLE` isolation.

**Settlement** (`internal.rs`, `models/trade.rs`, `models/order.rs`):
`POST /internal/settle` receives `{symbol, buy_order_id, sell_order_id, price, quantity,
timestamp}`. If an order ID is unknown, that side is treated as **anonymous** (a bot), which is
why migration `005` made trade participant columns nullable. Idempotency is achieved with
`exchange_fill_id` — a composite key from the matching engine — backed by a **non-partial unique
index** specifically so `ON CONFLICT` works (`006_exchange_fill_id_index.sql` explains that the
earlier partial index was insufficient). Error codes are semantic and consumed by the matching
engine's client: `PARTIAL_SETTLEMENT`, `ORDER_NOT_FOUND`, `ALREADY_SETTLED`
(`matching_engine_service/src/settlement.rs:151-177`).

`Trade::settle` runs in one transaction: begin → advisory locks → buyer side (`unlock` quote,
`trade` −quote, `trade` +base) → seller side (`unlock` base, `trade` −base, `trade` +quote) →
`Order::add_fill` per side → `INSERT INTO trades ... ON CONFLICT (exchange_fill_id) DO UPDATE
SET id = trades.id` → commit. On success the route also calls `OHLCV::update_from_trade`, whose
failure is only `warn!`-logged — candles are explicitly treated as secondary.

`Order::add_fill` is the single place order status changes, and it is one SQL `UPDATE` with a
`CASE`: `pending → partially_filled | filled`, with `cancelled` sticky. **`open`, `rejected` and
`expired` are permitted by the CHECK constraint but never set by any code path.**

`models/order.rs` provides `place`, `cancel`, `cancel_internal`, `add_fill`, `get`, `list_for_user`.

**Scheduler** (`scheduler.rs`): a periodic cleanup task, **disabled by default** behind
`ENABLE_DB_CLEANUP`, retaining 1 day.

**What to notice**
* **Migrations are hand-rolled**, not sqlx-migrate: `db::run_migrations` uses `include_str!` on
  an explicit array (`db/mod.rs:14-24`). Consequently `007_remove_exchange_order_id.sql` exists on
  disk but is **not in the array** — its change was folded into `004`. A file existing does not
  mean it runs. Always cross-check the array.
* **Settlement idempotency has a race.** The `get_by_fill_id` check happens *before* `pool.begin()`
  (`trade.rs:97-101`), while the in-transaction guarantee (`ON CONFLICT ... DO UPDATE SET id =
  trades.id`) executes *after* the ledger appends and `add_fill` have already run. A duplicate that
  loses the insert race still commits its ledger mutations, and the conflict branch is a no-op.
  There is no re-check of `fill_id` after the advisory locks are taken. Good discussion case for
  "what exactly does an idempotency key buy you?"
* **The cleanup scheduler is unsafe if enabled.** It issues `DELETE FROM trades WHERE created_at <
  $1` (the column is `settled_at`) and `DELETE FROM ohlcv WHERE bucket < $1` (the column is
  `open_time`), and — worst — it does `ALTER TABLE ledger DISABLE TRIGGER ledger_immutable` before
  deleting ledger rows, with `.ok()` swallowing the error. The failing `trades` delete returns
  early, so the trigger can be left **disabled**, silently voiding the append-only invariant that
  migration 003 establishes. This is the best example in the repo of why a "harmless background
  job" deserves the same review as a money path.
* The internal API has **no authentication**; it trusts network position and an `X-User-Id`
  header (`internal.rs:216-231, 275-290, 382-397`). Whoever can reach `:3001/internal/orders`
  can act as any user, and `/internal/mint` creates balance from nothing with no limit.
* `accounts` is both a library (`lib.rs`, used by tests) and a binary (`main.rs`). That split is
  what makes `accounts/tests/*.rs` possible without spinning up a server. Those tests are
  database-level integration tests requiring a live PostgreSQL; there is no in-process HTTP
  handler test harness.
* Access tokens live 15 minutes and cannot be revoked (no denylist); refresh tokens live 30 days,
  are stored **hashed**, rotate on use, and never leave an httpOnly cookie. `secure(false)` is
  hardcoded on that cookie, so it will be sent over plain HTTP — fine locally, wrong in production.
* `Otp::cleanup_expired` exists but is never called, so `otps` rows accumulate unbounded.

## 3.6 `market_data/` — the derived read model

**Responsibility:** turn the trade stream into OHLCV candles.

`market_data/src/main.rs`: connects to the database, spawns `start_websocket_consumer(gateway_ws_url)`
— it is a **WebSocket client of the gateway**, subscribing to the same public feed a browser would —
feeds trades into an `OhlcvAggregator` (`ohlcv.rs`), and exposes `GET /api/ohlcv?symbol=&interval=&limit=`
on `:3002`.

**What to notice**
* This service reconstructs history by *consuming events*, not by reading the matching engine, and
  it writes to the **same `ohlcv` table** as `accounts` — which also upserts candles synchronously
  inside `POST /internal/settle` (`accounts/src/routes/internal.rs:138`). So there are **two
  independent writers to the same time series**, using the same conflict key. Redundant, and it
  raises a question the code does not answer: which one is authoritative when they disagree?
* Because `accounts` also writes candles on the settle path, a `market_data` outage does not lose
  history outright — but the two paths can diverge in `volume`/`trade_count` accumulation, and
  neither has a backfill-from-`trades` routine.
* It is a second writer to the shared PostgreSQL database — shared-database coupling, not a
  service-owned datastore.
* Its HTTP surface (`GET /api/ohlcv` on `:3002`) is **not actually reachable through the gateway**:
  the gateway routes `/api/ohlcv` to accounts, and `proxy_market_data` is never wired to a route.

## 3.7 `trading_bot/` — a first-class client

**Responsibility:** generate liquidity. It is architecturally an **external client**: it connects
to the gateway WebSocket exactly like a user, authenticates, and places orders
(`gateway_client.rs`, 760 lines — the largest single file in the repo).

Strategies (`strategies/`): `market_maker`, `aggressive`, `random`, `mean_reversion`,
`random_walk`, `liquidity_provider`. Indicators in `indicators.rs`. CLI via `clap`:
`--gateway-host`, `--gateway-port`, `--symbol`, `--all`, etc. (`main.rs`).

**Why this is a good design signal:** because the bot is just a client, it exercises the public
API continuously and cannot reach into internals. It also means bot orders are "anonymous" from
the accounts perspective — hence the nullable trade columns.

## 3.8 `frontend/` — the SPA

React 19 + TypeScript + Vite + Tailwind + Zustand (state) + react-router-dom.
`src/hooks/useWebSocket.ts` implements the JSON-RPC client: auth with a token, subscription
management, request/response correlation by `id`, and channel-notification dispatch (a message
with `channel_name` + `notification` and no `id` is a push). `src/workers/marketDataWorker.ts`
and `timerWorker.ts` move book processing and timers off the main thread; `utils/hftOptimizations.ts`
holds hot-path helpers; `components/OrderBook.tsx` + `VirtualizedList.tsx` render the book.

**What to notice:** the frontend consumes *deltas*, so it must maintain its own book state and
recover from dropped deltas via the periodic snapshot — the same pattern the gateway uses.
State reconciliation is therefore an end-to-end concern, not a backend detail.

---

# Part 4 — Data model

Database: PostgreSQL 16 (TimescaleDB image, `docker-compose.yml:5`). Six tables.

```
users ──┬── refresh_tokens        (1:N, hashed tokens, revocable)
        ├── balances              (1:N per asset; UNIQUE(user_id, asset))
        │                          available / locked, both >= 0
        │                          (only `available` is ever written)
        ├── ledger                (1:N; APPEND-ONLY, DB-trigger enforced)
        ├── orders                (1:N)
        ├── trades (buyer/seller) (1:N, NULLABLE — anonymous bot sides)
        └── faucet_claims         (1:N, rate limiting)

orders ──┬── trades.buy_order_id  (NULLABLE)
         └── trades.sell_order_id (NULLABLE)

trades   ── exchange_fill_id      (UNIQUE, non-partial → idempotent settlement)

ohlcv    ── standalone time series, PK (symbol, interval, open_time)
```

| Table | Purpose | Key columns | Notes |
|---|---|---|---|
| `users` | identity | `id UUID PK`, `email UNIQUE` | no password column — passwordless by design |
| `otps` | login codes | `email, code, expires_at, used` | short-lived |
| `refresh_tokens` | sessions | `user_id, token_hash UNIQUE, expires_at, revoked_at` | hash, never plaintext |
| `balances` | current position | `user_id, asset, available, locked` | **`available` only** — `locked` is never written by any code path (vestigial, kept by a CHECK) |
| `ledger` | **journal of record** | `user_id, asset, amount, balance_after, entry_type, reference_id` | single-sided, immutable via trigger; no system account, no fee rows |
| `orders` | order lifecycle | `side ∈ {bid,ask}`, `order_type ∈ {limit,market}`, `status ∈ {pending, open, partially_filled, filled, cancelled, rejected, expired}`, `filled_quantity`, `lock_entry_id` | `open`/`rejected`/`expired` are declared but never set; `lock_entry_id` links to the ledger lock |
| `trades` | executed fills | `buy/sell_order_id`, `buyer/seller_id`, `price, quantity`, `buyer_fee, seller_fee`, `exchange_fill_id`, `settled_at` | the money-moving record |
| `faucet_claims` | testnet funding | `user_id, asset, amount, claimed_at` | |
| `ohlcv` | candles | `(symbol, interval, open_time)` PK, OHLC + `volume, trade_count` | served by `market_data` |

**Money handling convention:** `DECIMAL(20, 8)` in the database, `rust_decimal::Decimal` in Rust,
`f64` only on the UDP wire for market data. Never `float` for balances. Precision is per-asset:
`max_decimals_for_asset` allows 2 decimals for `EUR`/`USD`/`GBP` and 8 for everything else;
`validate_precision` rejects over-precise input and `round_to_precision` (`round_dp`) is applied
before writing lock, quote and fee amounts. API responses always send amounts as **strings**, never
as JSON numbers.

**Two invariants stated in the schema but not enforced by it:** nothing checks that a symbol's
assets exist (assets are free-form validated strings, symbols parsed with `split('/')`), and
nothing checks that the ledger balances across users. Those are application-level conventions only.

---

# Part 5 — End-to-end traces

Follow these in a debugger or by reading the listed files. This is where architecture stops being
a diagram and becomes a sequence you can defend in a design review.

## Trace A — place a limit order that partially crosses

| # | Where | What happens |
|---|---|---|
| 1 | Browser `useWebSocket.ts` | Sends `{id, method:"private/place_order", params:{symbol, side, order_type, price, quantity}}` |
| 2 | `gateway/src/websocket.rs:718` | Handler checks `ChannelManager::get_user_id(client_id)` — rejects with code `10000` if absent |
| 3 | `websocket.rs:1033` `create_order_in_accounts` | `POST {accounts}/internal/orders` with header `X-User-Id`. **Accounts validates and locks funds** |
| 4 | `accounts/src/models/order.rs:171` `Order::place` | Inserts the `orders` row (`status=pending`) and appends a negative `lock` ledger entry via `LedgerEntry::append_in_tx`, with `lock_entry_id` recorded on the order. The entry reduces `balances.available` (under a per-`(user, asset)` advisory lock), so an insufficient balance fails here |
| 5 | `websocket.rs:778` | `ChannelManager::register_order(order_id, user_id)` — records ownership so private events can be routed later |
| 6 | `websocket.rs:800-832` | Builds `OrderCommand::PlaceOrder` and `UdpOrderSender::send_order_command` → **JSON payload inside the UDP framing** to ME `:9100`. Replies `{result:{order_id}}` immediately — *before matching has happened* |
| 7 | ME `main.rs:205` | `order_rx.recv()` → `process_order_command` |
| 8 | ME `main.rs:245-252` | `orderbook.write().await`, `add_limit_order(...)`, lock released |
| 9 | `matching_engine/src/lib.rs:122` | `match_order` walks the opposite book best-price-first, FIFO within each level, emits `Fill`s at **the resting order's price** |
| 10 | ME `main.rs:262-296` | For each fill: `POST {accounts}/internal/settle`. Retries 3× with exponential backoff on 5xx/network errors. `ALREADY_SETTLED` → treated as success (idempotency) |
| 11 | `accounts/src/models/trade.rs:92` `Trade::settle` | One transaction: begin → advisory locks for both users in sorted order → for each side with a real order, append `unlock` + `trade` ledger entries (moving quote/base between `available` balances) → `Order::add_fill` (`pending → partially_filled/filled`) → `INSERT INTO trades ... ON CONFLICT (exchange_fill_id)`. An unknown order id means that side is an anonymous bot and is **skipped entirely**. Fees are written to the trade row but never to the ledger |
| 12 | ME `main.rs:299-309` | Only now publish `Fill` events over UDP as FlatBuffers to gateway `:9101` |
| 13 | ME `main.rs:316-333` | If a market order under-filled: `POST /internal/cancel` (unlock remainder) + publish `OrderCancelled` |
| 14 | ME `main.rs:350-356` | Publish `OrderFilled` for every order that reached 100% |
| 15 | Gateway `server.rs:169-176` | UDP receiver → `GatewayState::publish_event` → broadcast bus |
| 16 | Gateway `server.rs:186-282` | Broadcaster folds the event into `OrderBookState`, emits `book.*` / `ticker.*` / `lwt.*` notifications to subscribers |
| 17 | Gateway `websocket.rs:455-476` | Per-client task routes `OrderFilled`/`OrderCancelled` **only to the order's owner** |
| 18 | Browser | Renders the trade, updates the book, updates balances on next poll/refresh |
| 19 | ME `main.rs:166-176` | Independently, every 100 ms the publisher emits top-10 `OrderBookSnapshot` (every 10th tick) or `OrderBookDelta` |

**Points worth arguing about:** the client is told "accepted" at step 6, before matching (step 8)
— so `order_id` is an *intent* handle, not a confirmation. Fills are published only after HTTP
settlement (step 12), which makes the hot path's latency include a database transaction. And if
step 11 fails permanently, step 12 is skipped for that fill but the book has already moved
(step 8) — see §7.2.

## Trace B — cancel an order

1. `websocket.rs:850` `private/cancel_order` → `cancel_order_in_accounts`:
   `DELETE {accounts}/internal/orders/{id}` with `X-User-Id`.
2. Accounts `Order::cancel_internal` → mark `cancelled`, append an `unlock` ledger entry for the
   unfilled remainder (which raises `balances.available` back up).
3. Gateway sends `OrderCommand::CancelOrder` over UDP.
4. ME `main.rs:365-379` → `orderbook.cancel_order(id)`; if it returned true (the order was
   actually resting), publish `OrderCancelled`.

**Note the double bookkeeping:** the order is cancelled in accounts *first*. If the UDP cancel is
lost, the book keeps a phantom order whose funds have been unlocked. The gateway publishes nothing
that would correct it. This is a concrete consequence of a lossy transport plus two writers.
## Trace C — order book to a browser

`ME publisher (100 ms tick)` → UDP FlatBuffers `OrderBookDelta`/`Snapshot` → gateway UDP receiver
→ `GatewayState::publish_event` → `start_event_broadcaster` → `OrderBookState::apply_orderbook_update`
→ `ChannelNotification` JSON → `ChannelManager::get_subscribers("book.KCN/EUR")` → each client's
`broadcast::Sender<String>` → `Message::Text` on the WebSocket.

Sanity check for the learner: `OrderBookState` is the gateway's *replica*; the matching engine's
`OrderBook` is the *authority*; the browser's state is a *replica of a replica*. Snapshots reset
all three. Delta loss is survivable only because snapshots recur.

## Trace D — a candle appears (there are two writers)

Path 1 — `accounts`, synchronously on settlement: `POST /internal/settle` → `Trade::settle`
succeeds → `OHLCV::update_from_trade` (`internal.rs:138`) writes/updates the candle in the same
database. Failure here is only `warn!`-logged.

Path 2 — `market_data`, asynchronously from the feed: `Fill` event → gateway broadcasts it as a
trade notification → `market_data` (a WS *client* of the gateway) receives it →
`OhlcvAggregator` buckets by interval → `INSERT ... ON CONFLICT (symbol, interval, open_time)`
into the same `ohlcv` table.

Both paths upsert the same rows. That is redundant work and a consistency question worth asking:
which one wins when they disagree, and what happens to a candle when `market_data` is down but
`accounts` is up (or vice versa)?

Serving is a third wrinkle: `GET /api/ohlcv` on the gateway is routed to **`proxy_accounts`**, not
to `market_data` (`server.rs:137-141`). `proxy_market_data` is imported and `MARKET_DATA_URL` is
read at startup, but that function is **never attached to any route** — the market_data service
exposes its own `GET /api/ohlcv` on `:3002`, but the gateway does not use it. Dead wiring; verifying
it is a good exercise.

## Trace E — login

`POST /auth/request-otp {email}` → accounts generates a 6-digit code **without creating a user**,
stores an `otps` row (10-minute expiry), sends mail (console provider in dev, SMTP in prod —
`accounts/src/mail/`) → `POST /auth/signup` (new email) or `POST /auth/verify-otp` (existing email)
consumes the OTP atomically — `UPDATE otps SET used = TRUE WHERE email = $1 AND code = $2 AND
used = FALSE AND expires_at > NOW() RETURNING *` — then mints a 15-minute **access JWT** and a
30-day **refresh token** (only its SHA-256 hash is stored), setting the refresh token as an
httpOnly cookie while the access token is returned in the JSON body → the browser keeps the access
token in memory (`stores/authStore.ts`) and calls `WS public/auth {token}` so the gateway learns the
user for this connection. `signup` and `verify-otp` return the *same* generic
`"Invalid email or code"` error to avoid leaking which emails are registered.

---

# Part 6 — Ports, environment, topology

```
                    host port            container
frontend            5173    ──────────►  :80 (nginx)
gateway             3000    ──────────►  :3000   TCP  HTTP + WS
                    9101/udp ──────────►  :9101   UDP  events in  ◄── from ME :9103
                                       ( :9102 )  UDP  orders out ──► ME :9100
matching-engine-svc 8080    ──────────►  :8080   TCP  health only
                    9100/udp ──────────►  :9100   UDP  orders in
                                       ( :9103 )  UDP  events out ──► gateway :9101
accounts            3001    ──────────►  :3001   TCP  HTTP
market-data         3002    ──────────►  :3002   TCP  HTTP
postgres            5433    ──────────►  :5432
```

Environment variables that define the wiring:

| Variable | Service | Meaning |
|---|---|---|
| `BIND_ADDR` | gateway / accounts / market_data / ME | HTTP listen address |
| `SYMBOL` | ME | the single trading pair |
| `ORDER_RECEIVER_BIND` | ME | UDP address orders arrive on (`:9100`) |
| `GATEWAY_EVENT_ADDR` | ME | where to send events (`gateway:9101`) |
| `EVENT_SENDER_BIND` | ME | local UDP bind for the event sender (`:9103`) |
| `MATCHING_ENGINE_UDP_ADDR` | gateway | where to send orders (`matching-engine-service:9100`) |
| `ORDER_SENDER_BIND` | gateway | local UDP bind for the order sender (`:9102`) |
| `EVENT_RECEIVER_BIND` | gateway | UDP address events arrive on (`:9101`) |
| `ACCOUNTS_URL` | gateway, ME | accounts base URL |
| `MARKET_DATA_URL` | gateway | market_data base URL |
| `DATABASE_URL` | accounts, market_data | PostgreSQL DSN |
| `JWT_SECRET` | accounts | signing key |
| `ENVIRONMENT` | accounts | `development` enables `/auth/dev-login` |
| `MAIL_PROVIDER` | accounts | `console` or `smtp` |
| `CORS_ALLOWED_ORIGINS` | gateway, accounts | comma-separated allowlist (credentials mode) |

CI (`.github/workflows/ci.yml`): `cargo test` on `matching_engine` and `accounts`,
`cargo check` on `gateway` and `matching_engine_service`, plus frontend lint / typecheck / build.
Deployment (`.github/workflows/deploy.yml`): release builds, frontend build, EC2 deploy.

---

# Part 7 — How to judge this architecture

You now have enough to have opinions. Here is a starting position to argue with.

## 7.1 What is genuinely good

* **The pure core is separated properly.** `matching_engine` has zero I/O and zero async, so it
  can be unit-tested (30 in-file tests) without any infrastructure, and the crate boundary makes
  "the engine knows nothing about money" a fact rather than a promise.
* **Money never touches `f64`.** `Decimal` end-to-end for accounting; `f64` only for market data
  on the wire, with the conversion isolated at one boundary.
* **Settle-before-publish** gives clients a meaningful guarantee: an event you can see is an event
  that is in the ledger.
* **Idempotency was thought about:** `exchange_fill_id` unique index, `ALREADY_SETTLED` treated as
  success, and migration `006` fixing the partial-index/`ON CONFLICT` mismatch. (The remaining race
  is in §7.2 — the *intent* and the mechanism are both present, which is more than most systems
  manage.)
* **The ledger is immutable at the database level**, and balances are independently derivable and
  reconcilable in principle (`derive_balance`, `reconcile`).
* **Concurrency is handled explicitly** with per-`(user, asset)` advisory locks acquired in
  deterministic sorted order — a deliberate, deadlock-aware design rather than a global mutex.
* **Observability is built into the protocol layer** — sequence numbers, gap counters, stream
  state, sender/receiver stats.

## 7.2 Where the design is genuinely risky (with evidence)

1. **No compensation when settlement fails.** `main.rs:283-295` logs
   `INCONSISTENCY ... Manual intervention required` and continues. The book has moved; the ledger
   hasn't. `restore_order` is never called. Options: compensating sagas, an outbox/event log, or
   making the book derivable from the ledger. *(Also a great interview question.)*
2. **Two writers, no reconciliation loop.** Accounts owns durable order state; the matching engine
   owns the live book. A lost UDP order or cancel silently diverges them. There is no periodic
   reconciliation task. The README lists "No event replay" as a known limitation — this is its
   sharpest consequence.
3. **Lossy hot path with a weak recovery story.** Deltas are dropped silently; only the recurring
   snapshot (every 10 publisher ticks) repairs the gateway and browser. `sequence` is transmitted
   but not used for gap detection on the consumer side.
4. **In-process, unauthenticated internal API exposed through the edge.** Accounts' `/internal/*`
   trusts `X-User-Id` and has no auth (`internal.rs`), and the gateway proxies `/internal/*path`
   publicly (`server.rs:134`). Anyone who can reach the gateway origin can place orders as any
   user and call `/internal/mint`. Either bind the internal API to a separate listener/network
   or add service-to-service authentication (mTLS, signed service tokens).
5. **One `OrderBook` per process, one hardcoded symbol.** Vertical scaling only; no sharding by
   symbol, no standby, no failover. Acceptable for a portfolio project, disqualifying for
   production.
6. **`Arc<RwLock<ChannelManager>>`** (`server.rs:29`) is touched on every connect, auth, subscribe,
   order and disconnect — a single global lock on the connection hot path.
7. **Two writers to `ohlcv`.** `accounts` upserts candles inside the settlement path while
   `market_data` upserts them from a WebSocket feed. Neither is authoritative, neither has a
   backfill-from-`trades` routine, and the gateway cannot even reach `market_data`'s API
   (`proxy_market_data` is dead wiring). Pick one writer or make the projection explicitly
   rebuildable.
8. **Migration drift.** `007_remove_exchange_order_id.sql` exists but is not in the
   `run_migrations` array (`db/mod.rs`). Any change to migrations must be cross-checked there.
9. **Settlement idempotency can double-apply ledger entries.** The `fill_id` pre-check is a
   `SELECT` issued *outside* the transaction (`trade.rs:97-101`), while the `ON CONFLICT` guard
   runs *after* the ledger appends and `add_fill`. A concurrent duplicate that loses the insert
   race still commits its balance mutations. The unique index protects the `trades` row, not the
   money.
10. **Fees are calculated but never collected.** `FEE_RATE = dec!(0.001)` is stored on the trade
    row; `EntryType::Fee` is never written to the ledger by `accounts/src/`. The exchange has no
    revenue account.
11. **No fund conservation.** With one side anonymous (every bot fill), that side is skipped, so
    the trade creates base asset and destroys quote. There is no system account and no
    reconciliation job enforcing that the ledger nets to zero across users. `reconcile` exists in
    `ledger.rs` and is called by nothing.
12. **Enabling `ENABLE_DB_CLEANUP` is dangerous.** `scheduler.rs` disables the
    `ledger_immutable` trigger with `.ok()` (errors swallowed) and then deletes ledger rows; its
    `trades` delete references a column that does not exist (`created_at` vs `settled_at`), so it
    errors out and returns early — potentially leaving the trigger disabled. A configuration flag
    that silently voids the schema's strongest invariant.
13. **`balances.locked` is dead accounting.** No code path writes it, yet it carries CHECK
    constraints and is returned by the API. Anyone reading the schema will draw the wrong
    conclusion about how funds are reserved.
14. **Dead wiring / stale artifacts.** `proxy_market_data` and `MARKET_DATA_URL` are configured but
    never routed (`server.rs`); `OrderAccepted` is filtered before transmission because it is
    absent from the FlatBuffers union; and `matching_engine/benches/matching_engine_bench.rs` calls
    `add_limit_order(side, price, quantity)` — three arguments against a four-argument signature —
    so `cargo bench` cannot compile. `matching_engine/README.md` is stale in the same way and still
    claims `OrderId` is a `u64`. Treat README claims about the engine as unverified.

## 7.3 What is missing relative to the README's target architecture

| Missing piece | Why it matters | Cheapest meaningful first step |
|---|---|---|
| **Event log / replay** | Enables recovery, audit, and rebuilding `market_data` | Persist every `Fill`/book event to an append-only table or Kafka-style log before publishing |
| **Risk engine** | Pre-trade margin, position limits, fat-finger checks | Currently only balance checks, done inside accounts at order placement |
| **Dedicated settlement service** | Today settlement is a synchronous HTTP call inside the matching hot path | Own transaction log + async confirmation |
| **Redundancy / failover** | Single points of failure everywhere | Hot standby matching engine with deterministic replay of the order log |
| **Observability** | Logging only; no metrics/tracing | Prometheus counters already exist in `udp_proto` (`SenderStats`) — export them |
| **Market registry** | One hardcoded symbol | Table of markets + symbol routing in the gateway |
| **Backfill for OHLCV** | Recover from downtime | Rebuild candles from the `trades` table |

---

# Part 8 — Suggested reading order

Five sessions. Each ends with a checkpoint — if you can answer it, move on.

### Session 1 — The map (60 min)
1. `README.md` (whole file, including the limitations section at line 101)
2. `docker-compose.yml` — the runtime topology, ports and env wiring
3. This guide, Part 1 and Part 6
4. `grep -rn "pub enum" --include=*.rs .` and read each enum's variants

**Checkpoint:** draw the diagram from §1.2 from memory and label every arrow with protocol + port.

### Session 2 — The core (90 min)
1. `matching_engine/src/lib.rs` lines 1–240 (types, data structures, `add_limit_order`)
2. The five files in `matching_engine/examples/` — these are the most readable code in the repo
3. `matching_engine/src/lib.rs` lines 240–362 (`match_order`, `add_order_to_book`)

**Checkpoint:** explain out loud why `BTreeMap` + `VecDeque` + `HashMap` together give
price-time priority with O(log n) insert and O(1) cancel. Then run
`cargo test --manifest-path matching_engine/Cargo.toml`.

### Session 3 — The hot path (120 min)
1. `udp_proto/src/market_data.fbs` (the contract) — read the `.fbs`, not the generated file
2. `udp_proto/src/protocol.rs` (framing)
3. `matching_engine_service/src/main.rs:223-383` (`process_order_command`) then `389-548`
   (`OrderBookPublisher`)
4. `matching_engine_service/src/settlement.rs`

**Checkpoint:** write down the exact order of (match, settle, publish) and state what is
inconsistent if step N fails. Then `cargo test --manifest-path udp_proto/Cargo.toml`.

### Session 4 — The edge and the money (120 min)
1. `gateway/src/server.rs` (`router`, `start_event_broadcaster`)
2. `gateway/src/websocket.rs:310-560` (`ChannelManager`, `handle_websocket_connection`), then
   `718-950` (place/cancel handlers)
3. `accounts/src/main.rs` (composition + auth middleware)
4. `accounts/migrations/001..006` — read the SQL; it is the clearest statement of the data model
5. `accounts/src/models/order.rs:171` and `models/trade.rs:92` (place + settle)

**Checkpoint:** trace a €100 buy from browser to ledger entry, naming every function boundary.

### Session 5 — Judgment (90 min)
1. `gateway/src/channel_updates.rs` (the read model / CQRS projection)
2. `market_data/src/main.rs` + `ohlcv.rs`
3. `trading_bot/src/gateway_client.rs` (how a non-human client uses the same API)
4. Re-read Part 7 and write your own list of the top three changes you would make, with evidence

**Checkpoint:** defend or attack "settlement belongs on the hot path" in one paragraph.

---

# Part 9 — Hands-on exercises

Ordered by difficulty. All are safe and local.

1. **Read the examples.** `matching_engine/examples/*.rs` are ~25 lines each and demonstrate the
   whole domain: building a book, crossing a spread, market orders, partial fills, cancellation.
2. **Run the core tests.** `cargo test --manifest-path matching_engine/Cargo.toml` — the first
   build downloads dependencies and takes a few minutes.
3. **Run the protocol tests.** `cargo test --manifest-path udp_proto/Cargo.toml` — 624 lines of
   tests covering framing, gaps, heartbeats and FlatBuffers round-trips.
4. **Try the benchmark — and fix it.** `cargo bench --manifest-path matching_engine/Cargo.toml`
   currently **fails to compile**: `benches/matching_engine_bench.rs` calls
   `add_limit_order(side, price, quantity)` (3 args) against a real signature of
   `add_limit_order(order_id, side, price, quantity)` (4 args). Add an `OrderId`
   (`uuid::Uuid::new_v4()` — `Uuid` is re-exported by the crate) and get it building. This is a
   genuinely useful exercise: it forces you to read the public API, and it teaches you that a
   benchmark in the tree is not proof of anything until it compiles and runs.
5. **Bring it up.** `docker-compose up --build`, then open `http://localhost:5173`. Watch the
   logs: you will literally see `Sending Fill event via UDP` in the matching engine and
   `Received Fill event via UDP` in the gateway — Trace A in real time.
6. **Break it deliberately.** In `matching_engine_service/src/main.rs` set
   `SNAPSHOT_INTERVAL` to a very large number and observe how the browser book drifts as deltas
   are dropped. Then set it back to 1 and watch snapshots repair it. This makes the loss/recovery
   trade-off visceral.
7. **Trace the contract change.** Add a field to `Fill` in `udp_proto/src/market_data.fbs`,
   regenerate, and follow the compiler errors outward through `binary.rs`,
   `matching_engine_service/src/udp_transport.rs` and `gateway/src/udp_transport.rs`. This teaches
   you the cost of a schema change in a 5-hop pipeline.
8. **Fix a real inconsistency (design exercise, no code needed).** Write a one-page design for
   making the matching engine recoverable: an order-event log, deterministic replay, and a
   reconciliation job against `orders`. Compare it to the README's "No event replay" limitation.
9. **Close the trust boundary.** Add a shared-secret header (or mTLS) between gateway and accounts
   for `/internal/*`, and stop proxying `/internal/*path` from the public router. Note which tests
   break.
10. **Practise claim verification.** Every architectural claim in this guide is checkable with grep.
    Take three that surprise you — for example "the `locked` column is never written", "fees are
    never posted to the ledger", "`restore_order` has no callers" — and disprove or confirm each
    one yourself:

    ```bash
    grep -rn "locked *=" accounts/src --include=*.rs        # expect: no writes
    grep -rn "EntryType::Fee" accounts/src --include=*.rs   # expect: only the Display impl
    grep -rn "restore_order" . --include=*.rs               # expect: only matching_engine/src/lib.rs
    ```

    This is the habit that separates reading a codebase from believing its documentation — including
    this document and the repo's own `README.md`.
11. **Fix the fee hole (small, high-value).** Make `Trade::settle` post the buyer and seller fees as
    `EntryType::Fee` ledger entries to a system account, so the ledger nets to zero. Then explain
    why that change alone does not make the accounting double-entry as long as bot sides are skipped.

---

# Part 10 — Glossary

| Term | Meaning here |
|---|---|
| **Ask** | Sell side of the book |
| **Bid** | Buy side of the book |
| **Book / order book** | Resting limit orders, organised by price then arrival time |
| **Maker / taker** | The resting order vs the incoming order that matches it |
| **Crossing** | An incoming order whose price overlaps the opposite side, producing fills |
| **Crate** | A Rust compilation unit / package (≈ npm package) |
| **Delta** | Incremental book change (`Add`/`Update`/`Remove`), as opposed to a snapshot |
| **Fill** | A match produced by the engine — *not yet* a settled trade |
| **Trade** | A settled fill: money has moved, ledger written |
| **FlatBuffers** | Binary serialisation with zero-copy reads; schema in `.fbs` |
| **Lock** | Reserving balance for an open order (`balances.locked`) |
| **Ledger** | Append-only journal of every balance movement |
| **OHLCV** | Open/High/Low/Close/Volume candle |
| **Settlement** | Moving money for a fill: `POST /internal/settle` |
| **Snapshot** | Full book state, used to re-sync replicas |
| **Stream / stream_id** | One logical ordered flow of messages over UDP |
| **`Arc<T>`** | Shared, reference-counted ownership |
| **`RwLock<T>`** | Many readers *or* one writer |
| **`tokio`** | The async runtime; `tokio::spawn` starts a concurrent task |
| **`axum`** | The HTTP/WebSocket framework used by all services |
| **`sqlx`** | Async PostgreSQL client (queries are checked against the live schema at build time) |
| **`serde`** | Derive-based serialisation to/from JSON |

---

# Part 11 — Self-test questions

Answer these without looking. If you can do all sixteen, you understand the architecture.

1. Which two components hold order-book state, and which one is authoritative?
2. Why is settlement an HTTP call rather than a UDP message?
3. What exactly guarantees a client never sees a fill that isn't in the ledger?
4. Where does money get locked, where does it move, and where does it get unlocked?
5. What is `exchange_fill_id` for, and why did it need a *non-partial* unique index?
6. Name the three data structures inside `OrderBook` and the job each does.
7. What happens if the gateway's UDP cancel packet is lost after accounts has already cancelled?
8. How does the gateway's book replica recover from a dropped delta?
9. Which table is the source of truth for balances, and how is it protected from modification?
10. Why does `OrderAccepted` never travel over UDP?
11. Which service is a WebSocket *client* rather than a server, and who else writes the table it
    writes?
12. Where is the trust boundary between the public internet and the internal services, and where
    is it currently violated?
13. Which column in `balances` is never written, and what actually happens when an order "locks"
    funds?
14. Are trading fees collected? Where are they recorded, and where are they *not*?
15. Why is `ON CONFLICT (exchange_fill_id)` not sufficient to make settlement idempotent?
16. Which flag, if enabled, could silently disable a database-level invariant, and how?

---

## Appendix — Where to look for what

| You want to know about… | Read |
|---|---|
| The wire contract | `udp_proto/src/market_data.fbs` |
| Packet framing / MTU / heartbeats | `udp_proto/src/protocol.rs` |
| Loss detection | `udp_proto/src/receiver.rs` (`StreamState`, `GapInfo`, `ReceiverStats`) |
| Matching algorithm | `matching_engine/src/lib.rs:234+` (`match_order`) |
| Domain types | `matching_engine/src/lib.rs:1-102` |
| Order → match → settle → publish | `matching_engine_service/src/main.rs:223-383` |
| Snapshot/delta publishing | `matching_engine_service/src/main.rs:389-548` |
| Settlement retries + error codes | `matching_engine_service/src/settlement.rs:93-234` |
| Client protocol / subscriptions | `gateway/src/websocket.rs` |
| Event fan-out | `gateway/src/server.rs:181-283` |
| Gateway book replica | `gateway/src/channel_updates.rs` |
| HTTP proxying | `gateway/src/proxy.rs` |
| Auth + balances + orders API | `accounts/src/main.rs`, `accounts/src/routes/*` |
| Money movement | `accounts/src/models/ledger.rs`, `models/trade.rs`, `models/order.rs` |
| Schema | `accounts/migrations/*.sql` |
| Candle aggregation | `market_data/src/ohlcv.rs` |
| How a bot uses the public API | `trading_bot/src/gateway_client.rs` |
| Client-side delta handling | `frontend/src/hooks/useWebSocket.ts`, `workers/marketDataWorker.ts` |
| Known limitations | `README.md:101-113` |
| CI expectations | `.github/workflows/ci.yml` |

---

## Related documents

- [`ARCHITECTURE_GUIDE.zh-CN.md`](ARCHITECTURE_GUIDE.zh-CN.md) — this guide in Chinese
- [`EXCHANGE_DOMAIN_NOTES.md`](EXCHANGE_DOMAIN_NOTES.md) — the industry/business background behind the design:
  matching algorithms (FIFO vs pro-rata), order types and TIF, self-trade prevention, decimal vs integer money,
  audit and regulatory requirements, pre-trade risk, why market data and order flow use different transports,
  FIX, and clearing vs settlement
- [`ARCHITECTURE_V2.md`](ARCHITECTURE_V2.md) — the target architecture (event log, risk gateway, FIX gateway,
  async settlement, market data recovery) mapped against the verified problems listed in Part 7
