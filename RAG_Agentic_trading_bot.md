# RAG + Agentic Trading Bot — Architecture & Implementation Plan
**Date: 2026-05-01 | Session: TRD-20260416 | Project: Dhan Paper Trading System**

> **Status:** Phases 1, 2, and 3 are LIVE. See section 5 for what shipped.

---

## 1. The Learning Goal

The objective is to use the existing 10-strategy trading system as a vehicle to learn two modern AI engineering concepts:

1. **Agentic Architecture** — decompose a monolithic loop into independent, communicating agents
2. **RAG (Retrieval-Augmented Generation)** — use a vector knowledge base + LLM to make context-aware decisions

The trading system is an ideal lab for this because it already has:
- 10 distinct strategy modules (natural agent boundaries)
- A clear data pipeline (NSE → candles → signals → orders)
- Real historical trade outcomes to learn from
- A regime detection layer (ADX) that provides context

---

## 2. Current Architecture (Monolithic Loop)

```
DhanOmniEngine.run()  ← single thread, every 10 seconds
│
├── sync_data()                    # fetch ALL candles (sequential)
│
└── for idx in [5 indices]:
        check A: EMA_9_21          # sequential — waits for A to finish
        check B: OptionScalper     # before starting B
        check C: Supertrend_MACD
        check D: EMA_VWAP_SR
        check E: ORB_VWAP
        check F: TriplePattern
        check G: IndexMomentum
        check H: BB_MeanReversion
        check I: VWAPReclaim
        check J: CPRBreakout
        → if non-NEUTRAL: execute()
```

**Limitations of this design:**
- All 50 strategy calls (5 indices × 10 strategies) are sequential
- Adding a new strategy modifies the central engine file
- Strategies cannot react at different frequencies (all locked to 10s)
- No memory of past performance — same signal fires regardless of historical win rate
- No intelligence in choosing which signal to act on when multiple fire simultaneously

---

## 3. What "Agentic" Actually Means Here

An agent is an independent unit that:
- Has a **single responsibility**
- Communicates via **messages** (not direct function calls)
- Can run **concurrently** with other agents
- Has its own **state** and **lifecycle**

Applied to this system:

| Agent | Responsibility | Input | Output |
|---|---|---|---|
| **DataAgent** | Fetch & publish market data | Kite / NSE API | Candle snapshots on queue |
| **StrategyAgent × 10** | Evaluate one strategy | Candle snapshot | Signal event |
| **ExecutionAgent** | Place orders | Signal events | Dhan sandbox order |
| **SLAgent** | Monitor open positions | LTP feed | Modify / close orders |
| **MetaAgent** *(Phase 2)* | Filter signals using history + LLM | All signals + vector DB | Approved signals only |

---

## 4. What RAG Actually Means (and Where It Fits)

### RAG Definition
**Retrieval-Augmented Generation** is an LLM pattern:
```
Query → Search vector DB for top-K similar past examples
      → Inject retrieved examples into LLM prompt
      → LLM generates response WITH that historical context
```

### Why RAG Does NOT Apply to Rule-Based Strategies
EMA crossover, Supertrend, ORB breakout — these are deterministic formulas. Their output
is always `BULLISH / BEARISH / NEUTRAL`. There is no "generation" step, no LLM involved.
Calling this RAG would be incorrect.

### Where RAG DOES Apply in This System

The gap in the current system: **every signal is treated equally**.
- EMA_9_21 BULLISH on NIFTY fires whether ADX=15 (ranging, historically bad) or ADX=30 (trending, historically good)
- No signal knows its own historical win rate in the current market context

**RAG closes this gap via a Meta-Agent:**

```
Signal fires (e.g. VWAPReclaim BULLISH on BANKNIFTY, regime=TRENDING)
        │
        ▼
   Vector DB query:
   "Find past trades where VWAPReclaim fired BULLISH on BANKNIFTY in TRENDING regime"
        │
        ▼
   Retrieved: top-5 similar historical setups
   [BANKNIFTY VWAP Reclaim BULLISH, ADX=28, RSI=55 → +6.2% in 12 min]
   [BANKNIFTY VWAP Reclaim BULLISH, ADX=31, RSI=48 → +3.1% in 8 min]
   [BANKNIFTY VWAP Reclaim BULLISH, ADX=26, RSI=62 → -2.0% stopped out]
   ...
        │
        ▼
   LLM Meta-Agent prompt:
   "Current: VWAPReclaim BULLISH on BANKNIFTY, ADX=27, RSI=52, CPR_width=0.18%
    Historical similar setups: [retrieved above]
    Should I execute this trade? What is the confidence?"
        │
        ▼
   LLM response: "EXECUTE — 4/5 similar setups profitable, ADX aligns, RSI neutral.
                  Confidence: HIGH. Suggested position: 1 lot."
```

This is genuine RAG: retrieve → augment → generate → act.

---

## 5. Three-Phase Implementation Plan

### Phase 1 — Agentic Refactor (No LLM)
**Goal:** Restructure DhanOmniEngine into independent concurrent agents  
**Technology:** Python `asyncio` or `threading` + `queue.Queue`  
**Outcome:** Strategies run in parallel; data fetched once; new strategies plug in with zero engine changes

```
Architecture:
┌──────────────────────────────────────────────────────────────┐
│                      DATA AGENT                              │
│  Thread: polls NSE + Kite every 10s                         │
│  Publishes: MarketSnapshot(idx, df_1m, df_5m, regime, spot) │
└───────────────────────────┬──────────────────────────────────┘
                            │  queue.put(snapshot)
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
       ┌──────────┐  ┌──────────┐  ┌──────────┐
       │Strategy  │  │Strategy  │  │Strategy  │  × 10 worker threads
       │Agent: A  │  │Agent: I  │  │Agent: J  │
       └────┬─────┘  └────┬─────┘  └────┬─────┘
            └─────────────┼─────────────┘
                          │  signal_queue.put(SignalEvent)
                          ▼
               ┌──────────────────────┐
               │   EXECUTION AGENT    │
               │  Thread: consumes    │
               │  signal queue →      │
               │  Dhan sandbox order  │
               └──────────────────────┘
```

**Key data structures:**
```python
@dataclass
class MarketSnapshot:
    index_name: str
    df_1m:      pd.DataFrame
    df_5m:      pd.DataFrame
    df_15m:     pd.DataFrame
    regime:     str           # TRENDING / RANGING / TRANSITION
    adx:        float
    spot:       float
    timestamp:  datetime

@dataclass
class SignalEvent:
    index_name:    str
    strategy_name: str
    signal:        str        # BULLISH / BEARISH
    spot:          float
    adx:           float
    rsi:           float
    timestamp:     datetime
```

**Files to create:**
- `agents/data_agent.py` — DataAgent class
- `agents/strategy_worker.py` — StrategyWorker base class
- `agents/execution_agent.py` — ExecutionAgent class
- `agents/base.py` — shared dataclasses + queue definitions
- `DhanOmniEngine_v2.py` — new orchestrator (replaces DhanOmniEngine.py)

**Files unchanged:** All `strategies/*.py` files — zero changes to strategy logic

---

### Phase 2 — RAG Meta-Agent (LLM + Vector DB)
**Goal:** Add a Meta-Agent that filters signals using historical performance  
**Technology:** Claude API (claude-sonnet-4-6) + ChromaDB (local vector store)  
**Outcome:** Only signals with good historical context get executed

```
Architecture addition (sits between strategy workers and execution agent):

  SignalEvent
      │
      ▼
┌─────────────────────────────────────────────────────────┐
│                   META-AGENT                            │
│                                                         │
│  1. Embed signal context                                │
│     (strategy, index, regime, ADX, RSI, CPR, time)     │
│                                                         │
│  2. Query ChromaDB                                      │
│     → retrieve top-5 similar past trades                │
│                                                         │
│  3. Build LLM prompt                                    │
│     [current signal] + [retrieved historical setups]    │
│                                                         │
│  4. Claude API call                                     │
│     → EXECUTE / SKIP + reasoning + confidence          │
│                                                         │
│  5. If EXECUTE → forward to ExecutionAgent              │
│     If SKIP → log reason, discard                       │
└─────────────────────────────────────────────────────────┘
```

**Vector DB schema (each document = one past trade):**
```
Document text:
  "Strategy: VWAPReclaim | Index: BANKNIFTY | Signal: BULLISH
   Regime: TRENDING | ADX: 28.3 | RSI: 54.2 | CPR_width: 0.19%
   Time: 10:23 AM | Result: +6.2% profit in 12 min | Exit: TRAILING_AM"

Metadata:
  strategy, index, signal, regime, adx_bucket, rsi_bucket,
  result_pct, exit_reason, date, duration_min
```

**Embedding approach:**
- Use sentence-transformers (free, local) OR Claude's embeddings API
- Each trade outcome from `whatif_trades` table → embedded + stored in ChromaDB
- Updated nightly after EOD backtest runs

**LLM prompt template:**
```
You are a trading risk manager for NSE index options (paper trading).
A strategy has fired a signal. Review the historical context and decide.

CURRENT SIGNAL:
  Strategy: {strategy_name}
  Index: {index}, Direction: {signal}
  Market regime: {regime} (ADX={adx:.1f})
  RSI: {rsi:.1f}, Spot: {spot:.0f}
  Time: {time}, CPR: {cpr_context}

SIMILAR HISTORICAL SETUPS (retrieved from past trades):
{retrieved_docs}

DECISION:
  Should this signal be executed?
  Reply in JSON: {"action": "EXECUTE" or "SKIP", "confidence": "HIGH/MED/LOW", "reason": "..."}
```

---

### Phase 3 — Learning Loop (SHIPPED 2026-05-01)
**Goal:** System improves itself from its own trade history
**Status:** ✅ Live (session TRD-20260416, Phase 13 in build log)

#### Files
| File | Role |
|---|---|
| `rag/nightly_learn.py` | Orchestrator — runs whatif backtest, then incremental KB embed |
| `rag/audit_kb_growth.py` | Periodic audit — diffs KB state every 2 trading sessions, writes markdown report |
| `rag/audit_state.json` | Persists previous KB snapshot for diffing |
| `nightly_learn.bat`, `audit_kb_growth.bat` | Windows wrappers for Task Scheduler |

#### Cadence
| Job | Trigger | Cadence |
|---|---|---|
| Nightly Learn | `DhanNightlyLearn` (schtasks / cron) | Every weekday at 16:00 IST |
| KB Audit | `DhanKBAudit` (schtasks / cron) | Daily at 16:30 IST → script self-gates to every 2nd trading session, starting 2026-05-15 |

#### Scheduler commands (cross-platform)

**Windows — Task Scheduler:**
```cmd
schtasks /create /tn "DhanNightlyLearn" /tr "C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI\nightly_learn.bat"   /sc weekly /d MON,TUE,WED,THU,FRI /st 16:00 /f
schtasks /create /tn "DhanKBAudit"      /tr "C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI\audit_kb_growth.bat" /sc daily  /st 16:30 /f
```
Inspect: `schtasks /query /tn "DhanKBAudit" /v /fo LIST` · Run now: `schtasks /run /tn "DhanKBAudit"` · Remove: `schtasks /delete /tn "DhanKBAudit" /f`

**Linux / macOS — cron** (`crontab -e`):
```cron
TZ=Asia/Kolkata
0  16 * * 1-5  /home/USER/DhanAPI/nightly_learn.sh   >> /var/log/dhan_nightly_learn.log 2>&1
30 16 * * *    /home/USER/DhanAPI/audit_kb_growth.sh >> /var/log/dhan_kb_audit.log      2>&1
```

**Linux — systemd timers** (alternative, persistent + journalctl-friendly): create `dhan-nightly-learn.{service,timer}` with `OnCalendar=Mon..Fri 16:00 Asia/Kolkata`, and `dhan-kb-audit.{service,timer}` with `OnCalendar=*-*-* 16:30 Asia/Kolkata`. See `SYSTEM_REFERENCE.md §8 → Scheduled Background Tasks` for the full unit-file template.

Companion `.sh` wrappers (mirror of the `.bat` files): activate conda, `cd` to project, `exec python -m rag.nightly_learn` (or `rag.audit_kb_growth`), then `chmod +x`.

#### Daily flow
```
16:00 IST  DhanNightlyLearn.bat
              ├── eod_whatif_backtest.run_backtest(today)
              │     → fills whatif_trades (PROFIT / LOSS / BREAKEVEN per signal)
              ├── trade_embedder.embed_trades(incremental=True)
              │     → upserts new docs into ChromaDB
              └── log per-strategy win-rate snapshot

16:30 IST  DhanKBAudit.bat (every 2nd trading session, from 2026-05-15)
              ├── KnowledgeBase().count() + .win_rate() + .win_rate(strategy=...)
              ├── diff vs rag/audit_state.json snapshot
              └── write MasterConfiguration/reports/kb_audit_<DATE>.md
```

#### Dashboard
`/api/agent_status` returns `kb_count`, `kb_overall`, `last_learn`, and per-worker `kb_win_rate` / `kb_total`. The Agents tab shows a 📚 Learning Loop panel + adds two columns to the worker table.

#### Baseline (2026-05-01)
- ChromaDB: 94 trade-outcome documents (Apr 16 – Apr 24)
- Overall win-rate: 45.9% (39W / 46L of 85 non-breakeven)
- First real audit fires 2026-05-15 → diffs against this baseline

#### Long-game (after 30 days)
```
ChromaDB ~300–500 trade outcomes
Meta-Agent can distinguish:
    "EMA_9_21 BULLISH in RANGING regime → 70% loss rate → SKIP"
    "CPR Breakout BEARISH, narrow CPR, TRENDING → 65% win rate → EXECUTE"
```

#### Open follow-ups (Phase 3b candidates)
- MetaAgent decision feedback loop: today MetaAgent reads the KB but its own EXECUTE/SKIP outcomes are not written back as a separate document type. Closing this loop would let MetaAgent learn whether its filter calls were correct.
- Per-strategy regime-stratified retrieval (current query is by strategy + signal + session — does not filter by regime in the vector lookup).
- Cleanup of `kb_audit_*.md` reports after N days (currently they accumulate).

---

## 6. Technology Stack

| Component | Technology | Why |
|---|---|---|
| Agent communication | `queue.Queue` (threading) | Simple, no external dependencies, thread-safe |
| Concurrency | `threading.Thread` | Strategies are CPU-light; GIL not a bottleneck here |
| Vector DB | ChromaDB (local) | Free, runs on-device, no API key, persists to disk |
| Embeddings | `sentence-transformers` | Free, local, good quality for short structured text |
| LLM (Meta-Agent) | Claude API (`claude-sonnet-4-6`) | Best reasoning, structured JSON output, Anthropic SDK |
| Existing strategies | Unchanged `strategies/*.py` | No regression risk |

---

## 7. Learning Outcomes by Phase

### Phase 1 teaches:
- Agent design patterns (single responsibility, message passing)
- Thread-safe queues in Python
- Producer-Consumer architecture
- How to decouple components without breaking existing logic

### Phase 2 teaches:
- What RAG actually is (retrieve → augment → generate)
- Vector embeddings and similarity search
- Prompt engineering for structured decisions
- Claude API / Anthropic SDK usage
- How LLMs can augment deterministic systems (not replace them)

### Phase 3 teaches:
- Feedback loops and continuous learning
- Vector DB lifecycle management (upsert, versioning)
- How AI systems improve from their own history

---

## 8. What This Is NOT

| Common misconception | Reality |
|---|---|
| "RAG makes the strategies smarter" | No — the strategies stay deterministic. RAG only affects the Meta-Agent filter layer |
| "Agents need LLMs" | Phase 1 has zero LLM. Agents are just concurrent workers |
| "This replaces the existing system" | No — it refactors the engine. All 10 strategies, all SL logic, all filters stay intact |
| "ChromaDB stores candle data" | No — it stores trade OUTCOMES (structured text). Candles stay in SQLite |

---

## 9. Implementation Order

```
Step 1  Create agents/base.py          — dataclasses, queue definitions
Step 2  Create agents/data_agent.py    — moves sync_data() into a thread
Step 3  Create agents/strategy_worker.py — base worker class
Step 4  Create 10 strategy worker instances (one per strategy)
Step 5  Create agents/execution_agent.py — moves execute() into a thread
Step 6  Create DhanOmniEngine_v2.py    — orchestrates all agents
Step 7  Test: run v2 alongside v1, compare signal counts
Step 8  (Phase 2) Install ChromaDB + sentence-transformers
Step 9  Build trade embedder — reads whatif_trades → ChromaDB
Step 10 Build MetaAgent — Claude API call with retrieved context
Step 11 Wire MetaAgent between strategy workers and execution agent
Step 12 (Phase 3) Nightly ChromaDB update job
```

---

## 10. Files to be Created (Phase 1)

```
DhanAPI/
├── agents/
│   ├── __init__.py
│   ├── base.py              ← MarketSnapshot, SignalEvent dataclasses + queues
│   ├── data_agent.py        ← DataAgent (producer thread)
│   ├── strategy_worker.py   ← StrategyWorker base + 10 concrete workers
│   └── execution_agent.py   ← ExecutionAgent (consumer thread)
├── DhanOmniEngine_v2.py     ← new orchestrator
└── RAG_Agentic_trading_bot.md  ← this document
```

**Existing files — zero changes required:**
```
strategies/ema_9_21.py
strategies/option_scalper.py
strategies/supertrend_macd.py
strategies/advanced_ema_orb.py
strategies/index_momentum.py
strategies/triple_pattern.py
strategies/bollinger_mean_reversion.py
strategies/vwap_reclaim.py
strategies/cpr_breakout.py
strategies/pair_leadership.py
strategies/indicators.py
DhanOmniEngine.py            ← kept as-is until v2 is validated
```
