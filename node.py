"""
node.py — The blockchain authority node.

This container:
  - Maintains the single canonical Blockchain instance.
  - Accepts block submissions from miners  (POST /submit_block).
  - Pushes accepted blocks back to all miners (POST miner_url/new_block)
    so they abort their current PoW and begin mining the next block.
  - Streams a live event log to the dashboard via Server-Sent Events (SSE).
  - Serves the HTML dashboard at GET /.
  - Signals all miners to shut down when NUM_BLOCKS target is reached.

Environment variables:
  DIFFICULTY   Number of leading-zero hex chars required  (default: 4)
  NUM_BLOCKS   How many blocks to mine before signalling shutdown (default: 10)
"""

import asyncio
import json
import logging
import os
import random
import sqlite3
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from block import Block
from blockchain import Blockchain
from transaction import Transaction
from wallet import Wallet, verify_signature

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DIFFICULTY = int(os.environ.get("DIFFICULTY", 4))   # overridden by /start
NUM_BLOCKS = int(os.environ.get("NUM_BLOCKS", 10))

# ---------------------------------------------------------------------------
# SQLite persistence
# Stored at /app/data/cryptosim.db — mount a Docker volume there so it
# survives container restarts.  Portal wallets and blockchain blocks are
# written through immediately; in-memory state is always the authoritative
# source; SQLite is only used to restore state after a restart.
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "/app/data/cryptosim.db")


def _db_connect() -> sqlite3.Connection:
    """Return a connection with WAL mode for better concurrent reads."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables if they do not exist yet."""
    with _db_connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS portal_accounts (
                username        TEXT PRIMARY KEY,
                private_key_hex TEXT NOT NULL,
                public_key_hex  TEXT NOT NULL,
                address         TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS portal_usd (
                username    TEXT PRIMARY KEY,
                usd_balance REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blockchain_blocks (
                block_index INTEGER PRIMARY KEY,
                block_json  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
    logging.getLogger(__name__).info("SQLite database ready at %s", DB_PATH)


def load_state() -> None:
    """
    Restore in-memory state from SQLite after a container restart.
    Called once at startup before the FastAPI app starts serving requests.
    """
    from wallet import Wallet as _Wallet
    from block import Block as _Block
    from blockchain import Blockchain as _Blockchain

    global blockchain, started, DIFFICULTY, NUM_BLOCKS, _finalized_height
    global active_miner_ids, simulation_done, _treasury, _treasury_balance, _rnc_price

    with _db_connect() as conn:
        # ── Portal wallets ─────────────────────────────────────────────────
        rows = conn.execute(
            "SELECT username, private_key_hex, public_key_hex, address FROM portal_accounts"
        ).fetchall()
        for username, priv, pub, addr in rows:
            w = _Wallet(private_key_hex=priv)
            portal_wallets[username] = w
            portal_usd[username]     = 0.0
            addr_to_pubkey[addr]     = pub

        usd_rows = conn.execute(
            "SELECT username, usd_balance FROM portal_usd"
        ).fetchall()
        for username, bal in usd_rows:
            if username in portal_usd:
                portal_usd[username] = bal

        # ── Simulation meta ────────────────────────────────────────────────
        meta = dict(conn.execute("SELECT key, value FROM app_meta").fetchall())

        # Restore treasury wallet so buy/send still work after a restart
        if meta.get("treasury_private_key"):
            _treasury = _Wallet(private_key_hex=meta["treasury_private_key"])
            _treasury_balance = float(meta.get("treasury_balance", TREASURY_INITIAL_RNC))
            addr_to_pubkey[_treasury.address] = _treasury.public_key_hex
            logging.getLogger(__name__).info(
                "Treasury wallet restored: addr=%s  balance=%.4f RNC",
                _treasury.address, _treasury_balance,
            )

        if meta.get("rnc_price"):
            _rnc_price = float(meta["rnc_price"])

        if meta.get("started") == "true" and meta.get("difficulty"):
            DIFFICULTY = int(meta["difficulty"])
            if meta.get("num_blocks"):
                NUM_BLOCKS = int(meta["num_blocks"])
            _finalized_height = int(meta.get("finalized_height", "-1"))
            simulation_done   = meta.get("simulation_done") == "true"
            active_miner_ids  = json.loads(meta.get("active_miner_ids", "[]"))

            # ── Blockchain blocks ──────────────────────────────────────────
            block_rows = conn.execute(
                "SELECT block_json FROM blockchain_blocks ORDER BY block_index"
            ).fetchall()
            if block_rows:
                blockchain = _Blockchain(difficulty=DIFFICULTY)
                blockchain.chain = [
                    _Block.from_dict(json.loads(row[0])) for row in block_rows
                ]
                started = True
                logging.getLogger(__name__).info(
                    "Restored blockchain: %d blocks, difficulty=%d, finalized=%d",
                    len(blockchain.chain), DIFFICULTY, _finalized_height,
                )

        if portal_wallets:
            logging.getLogger(__name__).info(
                "Restored %d portal wallet(s) from SQLite", len(portal_wallets)
            )


def _db_save_portal_user(username: str, wallet, usd_balance: float) -> None:
    """Persist a portal user wallet and USD balance."""
    try:
        with _db_connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO portal_accounts "
                "(username, private_key_hex, public_key_hex, address) VALUES (?,?,?,?)",
                (username, wallet.private_key_hex, wallet.public_key_hex, wallet.address),
            )
            conn.execute(
                "INSERT OR REPLACE INTO portal_usd (username, usd_balance) VALUES (?,?)",
                (username, usd_balance),
            )
    except Exception as exc:
        logging.getLogger(__name__).warning("SQLite save portal user failed: %s", exc)


def _db_update_portal_usd(username: str, usd_balance: float) -> None:
    """Update the stored USD balance for a portal user."""
    try:
        with _db_connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO portal_usd (username, usd_balance) VALUES (?,?)",
                (username, usd_balance),
            )
    except Exception as exc:
        logging.getLogger(__name__).warning("SQLite update USD failed: %s", exc)


def _db_save_block(block) -> None:
    """Persist a newly accepted block."""
    try:
        with _db_connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO blockchain_blocks (block_index, block_json) VALUES (?,?)",
                (block.index, json.dumps(block.to_dict())),
            )
    except Exception as exc:
        logging.getLogger(__name__).warning("SQLite save block failed: %s", exc)


def _db_save_meta(**kwargs) -> None:
    """Upsert one or more key/value pairs into app_meta."""
    try:
        with _db_connect() as conn:
            for key, value in kwargs.items():
                conn.execute(
                    "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?,?)",
                    (key, str(value)),
                )
    except Exception as exc:
        logging.getLogger(__name__).warning("SQLite save meta failed: %s", exc)


def _db_clear_simulation() -> None:
    """Remove all blockchain blocks and simulation meta (keep portal wallets)."""
    try:
        with _db_connect() as conn:
            conn.execute("DELETE FROM blockchain_blocks")
            conn.execute("DELETE FROM app_meta WHERE key IN "
                         "('started','difficulty','num_blocks','finalized_height',"
                         "'simulation_done','active_miner_ids')")
    except Exception as exc:
        logging.getLogger(__name__).warning("SQLite clear simulation failed: %s", exc)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [NODE] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

# Blockchain is None until the user clicks Start on the dashboard.
blockchain: Blockchain | None = None

# Pre-start / startup control
started: bool = False
active_miner_ids: list[str] = []      # miners chosen to participate
_registered_order: list[str] = []     # insertion-order list of miner IDs

# miner_id → callback URL  (e.g. "miner_1" → "http://miner_1:8001")
miner_registry: dict[str, str] = {}

# miner_id → wallet address (populated when miners register with their address)
miner_addresses: dict[str, str] = {}

# address → public key hex  (populated from miner registration + user wallet creation)
addr_to_pubkey: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Treasury wallet — pre-funded, used to back the faucet / RNC purchases
# ---------------------------------------------------------------------------
_treasury = Wallet()          # fresh keypair each node start (fine for simulation)
TREASURY_INITIAL_RNC = 10_000.0   # virtual credit — injected at sim start
_treasury_balance: float = 0.0    # set when simulation starts

# ---------------------------------------------------------------------------
# Portal user wallets (server-side, custodial)
# username → Wallet   |   username → fake USD balance
# ---------------------------------------------------------------------------
portal_wallets: dict[str, Wallet] = {}
portal_usd: dict[str, float]      = {}
USER_STARTING_USD  = 1_000.0
BASE_RNC_PRICE_USD = 10.0        # floor / starting price per RNC
_rnc_price: float  = BASE_RNC_PRICE_USD   # live market price — mutates with every trade
_price_history: list[dict] = []           # [{"t": epoch_ms, "price": float}, ...] — full run history


# Pending RennCoin transaction pool (filled by background generator)
_mempool: list[dict] = []

# Each connected SSE browser client gets its own Queue
_sse_clients: list[asyncio.Queue] = []

# Ring-buffer of recent events so late-joining dashboards see history
_event_log: deque[str] = deque(maxlen=500)

# Protect chain mutations from concurrent miner submissions
_chain_lock = asyncio.Lock()

simulation_done = False

# Index of the highest confirmed block (-1 = none confirmed yet)
_finalized_height: int = -1

# Fork detection: height → (canonical_miner_id, accepted_unix_timestamp)
_recent_accepted: dict[int, tuple[str, float]] = {}

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialise SQLite and restore any persisted state before serving requests
    init_db()
    load_state()
    logger.info(
        "Node started — difficulty=%d, target_blocks=%d", DIFFICULTY, NUM_BLOCKS
    )
    mempool_task = asyncio.create_task(_mempool_generator())
    yield
    mempool_task.cancel()
    logger.info("Node shutting down")


app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# Event / logging helpers
# ---------------------------------------------------------------------------


async def log_event(msg: str, level: str = "info") -> None:
    """
    Central event logger for everything that happens on the node.

    Every significant event (miner registered, block accepted, block rejected,
    simulation done) flows through here so the Python log, the in-memory
    ring-buffer, and all connected SSE browser clients all see the same stream.
    """
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    _event_log.append(entry)

    if level == "error":
        logger.error(msg)
    else:
        logger.info(msg)

    # Push to every connected SSE client's individual queue
    for q in list(_sse_clients):
        try:
            q.put_nowait({"type": "log", "data": entry})
        except asyncio.QueueFull:
            pass  # Client is too slow; skip rather than block


async def broadcast_block_to_miners(block: Block) -> None:
    """
    Push the newly accepted block to every registered miner.

    This mirrors Bitcoin's block propagation: when the network accepts a
    block, all nodes (miners) receive it immediately so they can abandon
    their current nonce search and start working on the next block height.
    """
    payload = block.to_dict()
    async with httpx.AsyncClient(timeout=5.0) as client:
        for miner_id, url in list(miner_registry.items()):
            try:
                await client.post(f"{url}/new_block", json=payload)
                logger.debug("Broadcast block #%d → %s", block.index, miner_id)
            except Exception as exc:
                logger.warning(
                    "Failed to push block #%d to %s: %s", block.index, miner_id, exc
                )


async def _peer_validate_block(block: Block, winner_id: str) -> None:
    """
    Ask up to 2 non-winning active miners to independently validate the
    accepted block.  Simulates Bitcoin's decentralised peer validation where
    every full node re-verifies every block it receives from the network.
    """
    validators = [mid for mid in active_miner_ids if mid != winner_id][:2]
    if not validators:
        return
    async with httpx.AsyncClient(timeout=5.0) as client:
        for vid in validators:
            url = miner_registry.get(vid)
            if not url:
                continue
            try:
                resp = await client.post(
                    f"{url}/validate_block", json=block.to_dict()
                )
                result = resp.json()
                valid = result.get("valid", False)
                check = "\u2713" if valid else "\u2717"
                await log_event(
                    f"P2P validation: {vid} verified block #{block.index} → {check}"
                )
                for q in list(_sse_clients):
                    try:
                        q.put_nowait({
                            "type": "peer_validated",
                            "data": {
                                "block_index": block.index,
                                "validator":   vid,
                                "valid":       valid,
                            },
                        })
                    except asyncio.QueueFull:
                        pass
            except Exception as exc:
                logger.warning("Peer validation request to %s failed: %s", vid, exc)


async def _mempool_generator() -> None:
    """
    Background task: periodically create simulated RennCoin transfer
    transactions between random miner wallets and add them to the mempool.
    Mirrors a real blockchain's flow of user transactions waiting to be
    picked up and included by miners.
    Each internal trade also nudges _rnc_price up or down (smaller impact
    than portal buy/sell — market noise from miner-to-miner activity).
    """
    global _rnc_price

    while True:
        try:
            await asyncio.sleep(random.uniform(5, 12))
            if not started or len(miner_addresses) < 2:
                continue
            addrs = list(miner_addresses.items())          # [(miner_id, address), ...]
            from_id, from_addr = random.choice(addrs)
            to_candidates = [(mid, addr) for mid, addr in addrs if mid != from_id]
            if not to_candidates:
                continue
            _, to_addr = random.choice(to_candidates)
            amount = round(random.uniform(1.0, 10.0), 2)
            tx = Transaction.transfer(
                from_addr=from_addr,
                to_addr=to_addr,
                amount=amount,
                signature="simulated",
            ).to_dict()
            _mempool.append(tx)
            if len(_mempool) > 50:
                _mempool.pop(0)

            # Price impact: symmetric multiplicative move — background market noise.
            # Range 1–10% of current price (much smaller than portal buy/sell 1–50%).
            # Use up = P*(1+r) / down = P/(1+r) so a round-trip is exactly neutral,
            # eliminating the variance-drain downward bias of additive ± dollar moves.
            r         = random.uniform(0.01, 0.10)
            direction = random.choice([1, -1])
            if direction == 1:
                _rnc_price = max(1.0, round(_rnc_price * (1.0 + r), 2))
            else:
                _rnc_price = max(1.0, round(_rnc_price / (1.0 + r), 2))
            _db_save_meta(rnc_price=_rnc_price)
            _price_history.append({"t": int(time.time() * 1000), "price": _rnc_price})

            direction_word = "\u25b2" if direction == 1 else "\u25bc"
            await log_event(
                f"Mempool: +tx  {amount} RNC  {from_addr[:8]}\u2026 \u2192 {to_addr[:8]}\u2026"
                f"  |  RNC {direction_word} ${_rnc_price:.2f}"
            )

            # Push price and mempool count to all connected browsers immediately
            for q in list(_sse_clients):
                try:
                    q.put_nowait({"type": "mempool_update", "data": {"count": len(_mempool)}})
                except asyncio.QueueFull:
                    pass
            portal_addr_map = {w.address: u for u, w in list(portal_wallets.items())}
            for q in list(_sse_clients):
                try:
                    q.put_nowait({"type": "balance_update", "data": {
                        "balances": {},
                        "miner_addresses": dict(miner_addresses),
                        "portal_addresses": portal_addr_map,
                        "block_counts": {},
                        "rnc_price": _rnc_price,
                    }})
                except asyncio.QueueFull:
                    pass
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("_mempool_generator error (will retry): %s", exc)


async def signal_shutdown_to_miners() -> None:
    """
    Tell every registered miner to stop permanently.
    Called after the simulation reaches NUM_BLOCKS.
    """
    async with httpx.AsyncClient(timeout=5.0) as client:
        for miner_id, url in list(miner_registry.items()):
            try:
                await client.post(f"{url}/shutdown")
                logger.info("Sent shutdown signal → %s", miner_id)
            except Exception as exc:
                logger.warning("Failed to send shutdown to %s: %s", miner_id, exc)

    # Also close every SSE stream so browser clients know the sim is over
    for q in list(_sse_clients):
        try:
            q.put_nowait({"type": "shutdown", "data": "Simulation complete"})
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/register")
async def register_miner(payload: dict) -> dict:
    """
    Miners call this on startup to announce themselves.

    Before the simulation is started by the user:
      Returns {"status": "waiting"} so the miner polls /config.

    After the simulation starts:
      Returns {"status": "active", tip, difficulty} for chosen miners or
      {"status": "idle"} for miners that were not selected.

    Body: { "miner_id": "miner_1", "callback_url": "http://miner_1:8001" }
    """
    miner_id     = payload["miner_id"]
    callback_url = payload["callback_url"]
    address      = payload.get("address", "")
    public_key   = payload.get("public_key", "")

    miner_registry[miner_id] = callback_url
    if address:
        miner_addresses[miner_id] = address
    if address and public_key:
        addr_to_pubkey[address] = public_key
    if miner_id not in _registered_order:
        _registered_order.append(miner_id)

    reg_count = len(miner_registry)

    # Notify all SSE-connected browsers so the setup form counter updates
    for q in list(_sse_clients):
        try:
            q.put_nowait({
                "type": "miner_registered",
                "data": {"miner_id": miner_id, "count": reg_count},
            })
        except asyncio.QueueFull:
            pass

    if not started:
        await log_event(
            f"Miner registered: {miner_id} @ {callback_url} "
            f"(waiting for simulation start)"
        )
        return {"status": "waiting"}

    # Simulation already running — respond based on whether this miner is active
    if miner_id in active_miner_ids:
        tip = blockchain.get_tip()
        await log_event(
            f"Miner re-registered: {miner_id} @ {callback_url}  "
            f"(chain tip: block #{tip['index']})"
        )
        return {"status": "active", "tip": tip, "difficulty": DIFFICULTY}

    await log_event(f"Miner registered but not selected: {miner_id}")
    return {"status": "idle"}


@app.post("/submit_block")
async def submit_block(payload: dict) -> JSONResponse:
    """
    Miners POST a solved block here.

    The node:
      1. Acquires the chain lock to prevent two miners from both winning.
      2. Checks if the block is stale (another miner already solved this height).
      3. Verifies the block (index, previous_hash, hash integrity, PoW).
      4. Appends it to the chain.
      5. Broadcasts the new block to all miners.
      6. Checks whether the simulation target has been reached.

    Returns 200 on success, 409 if stale, 400 if invalid.
    """
    global simulation_done

    if not started or blockchain is None:
        return JSONResponse(
            status_code=503,
            content={"status": "not_started", "reason": "Simulation has not been started yet"},
        )

    block = Block.from_dict(payload)
    miner_id = payload.get("miner_id", "unknown")
    solve_time = payload.get("solve_time", 0.0)

    async with _chain_lock:
        # Stale check — another miner already won this block height
        if block.index != blockchain.last_block.index + 1:
            # Detect near-simultaneous forks (within 2 s of canonical acceptance)
            prior = _recent_accepted.get(block.index)
            if prior and (time.time() - prior[1]) < 2.0:
                canonical_miner = prior[0]
                fork_event = blockchain.record_fork(
                    block.index, canonical_miner, miner_id
                )
                await log_event(
                    f"\u26a1 FORK at block #{block.index}!  "
                    f"Canonical: {canonical_miner}  |  Orphaned: {miner_id}"
                )
                for q in list(_sse_clients):
                    try:
                        q.put_nowait({"type": "fork", "data": fork_event})
                    except asyncio.QueueFull:
                        pass
            else:
                await log_event(
                    f"Stale block #{block.index} from {miner_id} — "
                    f"chain already at #{blockchain.last_block.index}"
                )
            return JSONResponse(
                status_code=409,
                content={"status": "stale", "chain_tip": blockchain.last_block.index},
            )

        # Verification (hash integrity + PoW + chain linkage + transfer signatures)
        valid, reason = blockchain.verify_block(block, addr_to_pubkey)
        if not valid:
            await log_event(
                f"INVALID block #{block.index} from {miner_id}: {reason}",
                level="error",
            )
            return JSONResponse(
                status_code=400,
                content={"status": "invalid", "reason": reason},
            )

        # All checks passed — append to the canonical chain
        blockchain.append_block(block)
        _recent_accepted[block.index] = (miner_id, time.time())
        # Persist immediately so new blocks survive a node restart
        _db_save_block(block)

    # Remove transactions included in this block from the mempool
    block_tx_ids = {
        tx.get("tx_id") for tx in block.transactions if isinstance(tx, dict)
    }
    _mempool[:] = [tx for tx in _mempool if tx.get("tx_id") not in block_tx_ids]

    # Log the win (outside the lock — non-mutating)
    await log_event(
        f"Block #{block.index} ACCEPTED  |  miner={miner_id}  "
        f"nonce={block.nonce}  hash={block.hash[:16]}...  "
        f"time={solve_time:.2f}s"
    )

    # Push a structured block event to SSE clients for the chain panel
    block_event = {
        "index":  block.index,
        "miner":  miner_id,
        "hash":   block.hash[:16] + "...",
        "nonce":  block.nonce,
        "time":   round(solve_time, 2),
    }
    for q in list(_sse_clients):
        try:
            q.put_nowait({"type": "block", "data": block_event})
        except asyncio.QueueFull:
            pass

    # Check finality advancement
    global _finalized_height
    new_confirmed = blockchain.confirmed_height
    if new_confirmed > _finalized_height and new_confirmed >= 0:
        _finalized_height = new_confirmed
        _db_save_meta(finalized_height=_finalized_height)
        await log_event(f"\U0001f512 Block #{new_confirmed} FINALIZED (2-block confirmation depth)")
        for q in list(_sse_clients):
            try:
                q.put_nowait({"type": "finalized", "data": {"height": new_confirmed}})
            except asyncio.QueueFull:
                pass
        # Also send updated balances so leaderboard updates immediately
        balances = blockchain.compute_balances(confirmed_only=False)
        addr_to_miner = {addr: mid for mid, addr in miner_addresses.items()}
        bc: dict[str, int] = {}
        for blk in blockchain.chain[1:]:
            for tx in blk.transactions:
                if isinstance(tx, dict) and tx.get("from_addr") == "COINBASE":
                    mid = addr_to_miner.get(tx.get("to_addr", ""))
                    if mid:
                        bc[mid] = bc.get(mid, 0) + 1
        for q in list(_sse_clients):
            try:
                q.put_nowait({"type": "balance_update", "data": {
                    "balances": balances,
                    "miner_addresses": miner_addresses,
                    "portal_addresses": {w.address: u for u, w in portal_wallets.items()},
                    "block_counts": bc,
                    "rnc_price": _rnc_price,
                }})
            except asyncio.QueueFull:
                pass

    # Kick off P2P validation and broadcast in the background
    asyncio.create_task(_peer_validate_block(block, miner_id))

    # Check simulation target
    if blockchain.last_block.index >= NUM_BLOCKS:
        simulation_done = True
        _db_save_meta(simulation_done="true")
        await log_event(
            f"=== Simulation complete! {NUM_BLOCKS} blocks mined. "
            f"Shutting down all miners. ==="
        )
        # Broadcast the final block, then send shutdown
        await broadcast_block_to_miners(block)
        asyncio.create_task(signal_shutdown_to_miners())
    else:
        await broadcast_block_to_miners(block)

    return JSONResponse(status_code=200, content={"status": "accepted"})


@app.get("/chain")
async def get_chain() -> dict:
    """
    Return the full blockchain as JSON.
    Useful for debugging, auditing, or writing your own chain explorer.
    """
    if blockchain is None:
        return {"chain": [], "length": 0}
    return blockchain.to_dict()


@app.get("/mempool")
async def get_mempool() -> dict:
    """
    Return up to 10 pending transactions from the mempool.
    Miners call this before each PoW round to include real pending
    transactions in their candidate block.
    """
    return {"transactions": _mempool[:10], "count": len(_mempool)}


@app.post("/mining_progress")
async def mining_progress(payload: dict) -> JSONResponse:
    """
    Called by miners every 1 000 nonce iterations to stream live PoW
    progress to the dashboard.
    Body: { "miner_id": str, "block_index": int, "nonce": int,
            "hash_prefix": str, "rate": int }
    """
    event_data = {
        "miner_id":    payload.get("miner_id", "?"),
        "block_index": payload.get("block_index", 0),
        "nonce":       payload.get("nonce", 0),
        "hash_prefix": payload.get("hash_prefix", ""),
        "rate":        payload.get("rate", 0),
    }
    for q in list(_sse_clients):
        try:
            q.put_nowait({"type": "mining_progress", "data": event_data})
        except asyncio.QueueFull:
            pass
    return JSONResponse(status_code=200, content={"ok": True})


@app.get("/balances")
async def get_balances() -> dict:
    """
    Return RennCoin balances for all wallet addresses (full chain, not
    just finalized blocks) so portal and miner balances are always current.
    Also returns per-miner block counts for the leaderboard.
    """
    if blockchain is None:
        return {"balances": {}, "miner_addresses": {}, "portal_addresses": {}, "block_counts": {}}
    # Use the full chain so portal transfer blocks (which may never reach
    # 2-block finality after a simulation ends) are still counted.
    balances = blockchain.compute_balances(confirmed_only=False)
    # Count blocks won per miner by scanning coinbase transactions.
    addr_to_miner = {addr: mid for mid, addr in miner_addresses.items()}
    block_counts: dict[str, int] = {}
    for block in blockchain.chain[1:]:  # skip genesis
        for tx in block.transactions:
            if isinstance(tx, dict) and tx.get("from_addr") == "COINBASE":
                mid = addr_to_miner.get(tx.get("to_addr", ""))
                if mid:
                    block_counts[mid] = block_counts.get(mid, 0) + 1
    return {
        "balances":        balances,
        "miner_addresses": miner_addresses,
        "portal_addresses": {w.address: u for u, w in portal_wallets.items()},
        "block_counts":    block_counts,
        "rnc_price":       _rnc_price,
    }


@app.get("/config")
async def get_config() -> dict:
    """
    Return the current simulation configuration and state.
    Polled by miners waiting for the simulation to start, and by the
    browser on page-load to determine which screen to show.
    """
    tip = blockchain.get_tip() if blockchain is not None else None
    return {
        "started": started,
        "registered_count": len(miner_registry),
        "difficulty": DIFFICULTY if started else None,
        "active": active_miner_ids,
        "tip": tip,
        "num_blocks": NUM_BLOCKS,
    }


@app.post("/start")
async def start_simulation(payload: dict) -> JSONResponse:
    """
    Called by the dashboard when the user fills in the config form and
    clicks Start.  Initialises the blockchain, selects the active miners,
    and broadcasts a 'started' SSE event to all connected browsers.

    Body: { "num_miners": int, "difficulty": int }
    """
    global started, blockchain, DIFFICULTY, active_miner_ids

    if started:
        return JSONResponse(
            status_code=400, content={"error": "Simulation already started"}
        )

    num_miners = int(payload.get("num_miners", 1))
    difficulty = int(payload.get("difficulty", 4))

    if not (1 <= difficulty <= 8):
        return JSONResponse(
            status_code=422, content={"error": "difficulty must be between 1 and 8"}
        )
    if num_miners < 1 or num_miners > len(miner_registry):
        return JSONResponse(
            status_code=422,
            content={
                "error": f"num_miners must be between 1 and {len(miner_registry)} "
                         f"(currently registered)"
            },
        )

    DIFFICULTY = difficulty
    blockchain = Blockchain(difficulty=DIFFICULTY)
    active_miner_ids = _registered_order[:num_miners]
    started = True

    # Initialise treasury: register its pubkey and give it a virtual RNC balance
    global _treasury_balance
    _treasury_balance = TREASURY_INITIAL_RNC
    addr_to_pubkey[_treasury.address] = _treasury.public_key_hex
    # Seed the price history with the starting price
    _price_history.append({"t": int(time.time() * 1000), "price": _rnc_price})

    # Persist simulation start so state can be recovered after a node restart
    _db_save_block(blockchain.chain[0])   # persist genesis block
    _db_save_meta(
        started="true",
        difficulty=DIFFICULTY,
        num_blocks=NUM_BLOCKS,
        finalized_height=_finalized_height,
        simulation_done="false",
        active_miner_ids=json.dumps(active_miner_ids),
        treasury_private_key=_treasury.private_key_hex,
        treasury_balance=TREASURY_INITIAL_RNC,
    )

    start_data = {
        "difficulty": DIFFICULTY,
        "num_miners": num_miners,
        "active": active_miner_ids,
        "target": NUM_BLOCKS,
        "miner_addresses": miner_addresses,
    }

    for q in list(_sse_clients):
        try:
            q.put_nowait({"type": "started", "data": start_data})
        except asyncio.QueueFull:
            pass

    await log_event(
        f"=== Simulation started! difficulty={DIFFICULTY}, "
        f"active miners={active_miner_ids}, target={NUM_BLOCKS} blocks ==="
    )

    return JSONResponse(status_code=200, content={"status": "started", **start_data})


@app.post("/reset")
async def reset_simulation() -> JSONResponse:
    """
    Called by the dashboard 'Run Another Experiment' button.
    Resets all per-simulation state so a fresh run can be configured.
    Miners are notified via POST /reset so they re-enter the wait loop
    without needing a container restart.
    """
    global started, blockchain, simulation_done, active_miner_ids
    global _mempool, _finalized_height, _recent_accepted, _treasury_balance, _rnc_price
    global _price_history

    started           = False
    blockchain        = None
    simulation_done   = False
    active_miner_ids  = []
    _mempool          = []
    _finalized_height = -1
    _recent_accepted  = {}
    _treasury_balance = 0.0
    _rnc_price        = BASE_RNC_PRICE_USD
    _price_history    = []
    _event_log.clear()

    # Reset every portal user's USD back to the starting allowance
    for uname in list(portal_wallets.keys()):
        portal_usd[uname] = USER_STARTING_USD
        _db_update_portal_usd(uname, USER_STARTING_USD)

    # Clear simulation data from SQLite (portal wallets are preserved)
    _db_clear_simulation()
    _db_save_meta(rnc_price=BASE_RNC_PRICE_USD)

    # Tell every known miner to stop and re-enter the activation-wait loop
    async with httpx.AsyncClient(timeout=5.0) as client:
        for miner_id, url in list(miner_registry.items()):
            try:
                await client.post(f"{url}/reset")
            except Exception as exc:
                logger.warning("Failed to reset miner %s: %s", miner_id, exc)

    # Broadcast reset to all connected browsers
    for q in list(_sse_clients):
        try:
            q.put_nowait({"type": "reset", "data": "Simulation reset"})
        except asyncio.QueueFull:
            pass

    logger.info("=== Simulation reset — ready for new experiment ===")
    return JSONResponse(status_code=200, content={"status": "reset"})


# ---------------------------------------------------------------------------
# On-demand transfer block mining
# When a portal transfer lands in the mempool we mine a difficulty-0 block
# immediately so the trade confirms in real time without waiting for a miner.
# ---------------------------------------------------------------------------

async def _mine_transfer_block(tx_dict: dict) -> None:
    """
    Mine a special transfer block at difficulty 0 (nonce search trivial).
    The block goes through the same verify_block / finalization / SSE path
    as miner-submitted blocks so the chain, leaderboard, and portal all
    update automatically.
    """
    global _finalized_height

    if blockchain is None:
        return

    async with _chain_lock:
        tip     = blockchain.last_block
        block   = Block(
            index=tip.index + 1,
            timestamp=time.time(),
            transactions=[tx_dict],
            previous_hash=tip.hash,
            nonce=0,
        )
        # Difficulty-0 means any hash is valid — pass pow_target="" to skip PoW check
        block.hash = block.compute_hash()
        valid, reason = blockchain.verify_block(block, addr_to_pubkey, pow_target="")
        if not valid:
            logger.error("Transfer block rejected: %s", reason)
            await log_event(f"[PORTAL] Transfer FAILED: {reason}", level="error")
            return
        blockchain.append_block(block)
        _recent_accepted[block.index] = ("portal", time.time())
        # Persist so the transfer survives a node restart
        _db_save_block(block)

    # Remove tx from mempool
    _mempool[:] = [t for t in _mempool if t.get("tx_id") != tx_dict.get("tx_id")]

    await log_event(
        f"Transfer block #{block.index} mined  |  "
        f"{tx_dict['from_addr'][:10]}... \u2192 {tx_dict['to_addr'][:10]}...  "
        f"{tx_dict['amount']} RNC"
    )

    block_event = {
        "index": block.index,
        "miner": "portal",
        "hash":  block.hash[:16] + "...",
        "nonce": block.nonce,
        "time":  0.0,
        "tx": {
            "from": tx_dict["from_addr"],
            "to":   tx_dict["to_addr"],
            "amount": tx_dict["amount"],
        },
    }
    for q in list(_sse_clients):
        try:
            q.put_nowait({"type": "block", "data": block_event})
        except asyncio.QueueFull:
            pass

    # Finality check
    new_confirmed = blockchain.confirmed_height
    if new_confirmed > _finalized_height and new_confirmed >= 0:
        _finalized_height = new_confirmed
        _db_save_meta(finalized_height=_finalized_height)
        await log_event(f"\U0001f512 Block #{new_confirmed} FINALIZED (2-block confirmation depth)")
        for q in list(_sse_clients):
            try:
                q.put_nowait({"type": "finalized", "data": {"height": new_confirmed}})
            except asyncio.QueueFull:
                pass
        balances = blockchain.compute_balances(confirmed_only=False)
        for q in list(_sse_clients):
            try:
                q.put_nowait({"type": "balance_update", "data": {
                    "balances": balances,
                    "miner_addresses": miner_addresses,
                    "portal_addresses": {w.address: u for u, w in portal_wallets.items()},
                    "rnc_price": _rnc_price,
                }})
            except asyncio.QueueFull:
                pass

    # Push transfer_confirmed event so the portal UI can update live
    for q in list(_sse_clients):
        try:
            q.put_nowait({"type": "transfer_confirmed", "data": {
                "tx_id":       tx_dict["tx_id"],
                "from_addr":   tx_dict["from_addr"],
                "to_addr":     tx_dict["to_addr"],
                "amount":      tx_dict["amount"],
                "block_index": block.index,
            }})
        except asyncio.QueueFull:
            pass

    await broadcast_block_to_miners(block)


# ---------------------------------------------------------------------------
# Portal endpoints
# ---------------------------------------------------------------------------

def _get_confirmed_balance(address: str) -> float:
    """
    Return the on-chain balance for an address, including all blocks
    (not just finalized ones).  Used by the portal wallet display so
    users see their balance update immediately after a buy or transfer
    rather than waiting for the 2-block confirmation window.
    """
    if blockchain is None:
        return 0.0
    balances = blockchain.compute_balances(confirmed_only=False)
    return balances.get(address, 0.0)


def _all_known_addresses() -> list[dict]:
    """Return all known addresses: miners + portal users."""
    result = []
    # Miners
    for mid, addr in miner_addresses.items():
        result.append({"label": mid, "address": addr, "type": "miner"})
    # Portal users
    for username, wallet in portal_wallets.items():
        result.append({"label": username, "address": wallet.address, "type": "user"})
    return result


@app.post("/portal/create_wallet")
async def portal_create_wallet(payload: dict) -> JSONResponse:
    """
    Create a new portal user wallet.
    Body: { "username": str }
    Returns: { "address": str, "rnc_balance": float, "usd_balance": float }
    """
    username = payload.get("username", "").strip()
    if not username:
        return JSONResponse(status_code=400, content={"error": "username required"})
    if username in portal_wallets:
        return JSONResponse(status_code=409, content={"error": "username already taken"})

    w = Wallet()
    portal_wallets[username] = w
    portal_usd[username]     = USER_STARTING_USD
    addr_to_pubkey[w.address] = w.public_key_hex

    # Persist to SQLite so the wallet survives container restarts
    _db_save_portal_user(username, w, USER_STARTING_USD)

    logger.info("Portal wallet created: %s  addr=%s", username, w.address)

    # Notify all connected browsers so their recipient dropdowns update immediately
    for q in list(_sse_clients):
        try:
            q.put_nowait({"type": "user_registered", "data": {
                "username": username, "address": w.address
            }})
        except asyncio.QueueFull:
            pass

    return JSONResponse(status_code=200, content={
        "username":    username,
        "address":     w.address,
        "rnc_balance": 0.0,
        "usd_balance": USER_STARTING_USD,
    })


@app.post("/portal/login")
async def portal_login(payload: dict) -> JSONResponse:
    """
    Retrieve wallet info for an existing user.
    Body: { "username": str }
    """
    username = payload.get("username", "").strip()
    if username not in portal_wallets:
        return JSONResponse(status_code=404, content={"error": "user not found"})

    w   = portal_wallets[username]
    rnc = _get_confirmed_balance(w.address)
    return JSONResponse(status_code=200, content={
        "username":    username,
        "address":     w.address,
        "rnc_balance": rnc,
        "usd_balance": portal_usd.get(username, 0.0),
        "rnc_price":   _rnc_price,
    })


@app.post("/portal/buy_rnc")
async def portal_buy_rnc(payload: dict) -> JSONResponse:
    """
    User spends fake USD to buy RNC from the treasury.
    Body: { "username": str, "usd_amount": float }
    The treasury signs a transfer directly to the user's wallet address and
    an on-demand transfer block is mined immediately.
    """
    global _treasury_balance, _rnc_price

    username   = payload.get("username", "").strip()
    usd_amount = float(payload.get("usd_amount", 0))

    if username not in portal_wallets:
        return JSONResponse(status_code=404, content={"error": "user not found"})
    if usd_amount <= 0:
        return JSONResponse(status_code=400, content={"error": "usd_amount must be positive"})

    rnc_amount = round(usd_amount / _rnc_price, 4)
    if rnc_amount <= 0:
        return JSONResponse(status_code=400, content={"error": "amount too small"})
    if portal_usd[username] < usd_amount:
        return JSONResponse(status_code=400, content={"error": "Insufficient USD balance"})
    if _treasury_balance < rnc_amount:
        return JSONResponse(status_code=400, content={"error": "Treasury insufficient funds"})
    if blockchain is None:
        return JSONResponse(status_code=503, content={"error": "Simulation not started"})

    # Deduct USD, debit treasury, bump price by 1–50% of current price
    portal_usd[username] = round(portal_usd[username] - usd_amount, 4)
    _treasury_balance    = round(_treasury_balance - rnc_amount, 4)
    price_bump = round(random.uniform(0.01, 0.50) * _rnc_price, 2)
    _rnc_price = round(_rnc_price + price_bump, 2)
    # Persist updated USD, treasury, and price
    _db_update_portal_usd(username, portal_usd[username])
    _db_save_meta(treasury_balance=_treasury_balance, rnc_price=_rnc_price)
    _price_history.append({"t": int(time.time() * 1000), "price": _rnc_price})

    # Treasury signs the transfer
    to_addr   = portal_wallets[username].address
    timestamp = time.time()
    import hashlib as _hs
    tx_id = _hs.sha256(f"{_treasury.address}:{to_addr}:{rnc_amount}:{timestamp}".encode()).hexdigest()
    import json as _j
    signing_payload = _j.dumps(
        {"from_addr": _treasury.address, "to_addr": to_addr,
         "amount": rnc_amount, "timestamp": timestamp}, sort_keys=True
    )
    sig = _treasury.sign(signing_payload)

    tx_dict = {
        "tx_id":     tx_id,
        "from_addr": _treasury.address,
        "to_addr":   to_addr,
        "amount":    rnc_amount,
        "timestamp": timestamp,
        "signature": sig,
    }

    asyncio.create_task(log_event(
        f"\U0001f4b8 Portal BUY: {username} bought {rnc_amount} RNC"
        f" for ${usd_amount:.2f} USD — RNC price now ${_rnc_price:.2f}",
        "info",
    ))
    asyncio.create_task(_mine_transfer_block(tx_dict))

    return JSONResponse(status_code=200, content={
        "status":       "mining",
        "tx_id":        tx_id,
        "rnc_amount":   rnc_amount,
        "usd_spent":    usd_amount,
        "usd_balance":  portal_usd[username],
        "rnc_price":    _rnc_price,
        "message":      f"Buying {rnc_amount} RNC for ${usd_amount:.2f} — confirming...",
    })


@app.post("/portal/sell_rnc")
async def portal_sell_rnc(payload: dict) -> JSONResponse:
    """
    User sells RNC back to the treasury for fake USD.
    Body: { "username": str, "rnc_amount": float }
    Price drops by a random $1-5 per RNC sold (floor $1.00).
    """
    global _treasury_balance, _rnc_price

    username   = payload.get("username", "").strip()
    rnc_amount = float(payload.get("rnc_amount", 0))

    if username not in portal_wallets:
        return JSONResponse(status_code=404, content={"error": "user not found"})
    if rnc_amount <= 0:
        return JSONResponse(status_code=400, content={"error": "rnc_amount must be positive"})
    if blockchain is None:
        return JSONResponse(status_code=503, content={"error": "Simulation not started"})
    if _treasury is None:
        return JSONResponse(status_code=503, content={"error": "Treasury not initialised"})

    sender_wallet = portal_wallets[username]
    rnc_balance   = _get_confirmed_balance(sender_wallet.address)
    if rnc_balance < rnc_amount:
        return JSONResponse(status_code=400, content={
            "error": f"Insufficient RNC: have {rnc_balance:.4f}, need {rnc_amount:.4f}"
        })

    usd_received = round(rnc_amount * _rnc_price, 2)

    # Credit USD, restore treasury RNC, drop price
    portal_usd[username] = round(portal_usd[username] + usd_received, 4)
    _treasury_balance    = round(_treasury_balance + rnc_amount, 4)
    price_drop = round(random.uniform(0.01, 0.50) * _rnc_price, 2)
    _rnc_price = max(1.0, round(_rnc_price - price_drop, 2))

    _db_update_portal_usd(username, portal_usd[username])
    _db_save_meta(treasury_balance=_treasury_balance, rnc_price=_rnc_price)
    _price_history.append({"t": int(time.time() * 1000), "price": _rnc_price})

    # Sign and mine a transfer: user → treasury
    timestamp = time.time()
    import hashlib as _hs, json as _j
    tx_id = _hs.sha256(
        f"{sender_wallet.address}:{_treasury.address}:{rnc_amount}:{timestamp}".encode()
    ).hexdigest()
    signing_payload = _j.dumps(
        {"from_addr": sender_wallet.address, "to_addr": _treasury.address,
         "amount": rnc_amount, "timestamp": timestamp}, sort_keys=True
    )
    sig = sender_wallet.sign(signing_payload)

    tx_dict = {
        "tx_id":     tx_id,
        "from_addr": sender_wallet.address,
        "to_addr":   _treasury.address,
        "amount":    rnc_amount,
        "timestamp": timestamp,
        "signature": sig,
    }
    asyncio.create_task(log_event(
        f"\U0001f4c9 Portal SELL: {username} sold {rnc_amount} RNC"
        f" for ${usd_received:.2f} USD — RNC price now ${_rnc_price:.2f}",
        "info",
    ))
    asyncio.create_task(_mine_transfer_block(tx_dict))

    return JSONResponse(status_code=200, content={
        "status":       "mining",
        "tx_id":        tx_id,
        "rnc_amount":   rnc_amount,
        "usd_received": usd_received,
        "usd_balance":  portal_usd[username],
        "rnc_price":    _rnc_price,
        "message":      f"Selling {rnc_amount} RNC for ${usd_received:.2f} — confirming...",
    })


@app.post("/portal/send_rnc")
async def portal_send_rnc(payload: dict) -> JSONResponse:
    """
    Send RNC from one wallet to any other known address.
    Works for:  portal user \u2192 anyone,  miner \u2192 anyone (node proxies signing)
    Body: { "from_username": str | null, "from_miner_id": str | null,
            "to_address": str, "amount": float }
    Exactly one of from_username or from_miner_id must be set.
    """
    from_username = payload.get("from_username", "").strip() or None
    from_miner_id = payload.get("from_miner_id", "").strip() or None
    to_address    = payload.get("to_address", "").strip()
    amount        = float(payload.get("amount", 0))

    if not to_address:
        return JSONResponse(status_code=400, content={"error": "to_address required"})
    if amount <= 0:
        return JSONResponse(status_code=400, content={"error": "amount must be positive"})
    if blockchain is None:
        return JSONResponse(status_code=503, content={"error": "Simulation not started"})

    # Resolve sender
    if from_username:
        if from_username not in portal_wallets:
            return JSONResponse(status_code=404, content={"error": "sender not found"})
        sender_wallet   = portal_wallets[from_username]
        sender_address  = sender_wallet.address
        confirmed_bal   = _get_confirmed_balance(sender_address)
        if confirmed_bal < amount:
            return JSONResponse(status_code=400, content={
                "error": f"Insufficient balance: have {confirmed_bal:.2f} RNC, need {amount:.2f}"
            })
        # Sign locally (custodial)
        timestamp = time.time()
        import hashlib as _hs, json as _j
        tx_id = _hs.sha256(f"{sender_address}:{to_address}:{amount}:{timestamp}".encode()).hexdigest()
        signing_payload = _j.dumps(
            {"from_addr": sender_address, "to_addr": to_address,
             "amount": amount, "timestamp": timestamp}, sort_keys=True
        )
        sig = sender_wallet.sign(signing_payload)

    elif from_miner_id:
        if from_miner_id not in miner_registry:
            return JSONResponse(status_code=404, content={"error": "miner not found"})
        sender_address = miner_addresses.get(from_miner_id, "")
        if not sender_address:
            return JSONResponse(status_code=400, content={"error": "miner address unknown"})
        confirmed_bal = _get_confirmed_balance(sender_address)
        if confirmed_bal < amount:
            return JSONResponse(status_code=400, content={
                "error": f"Insufficient balance: have {confirmed_bal:.2f} RNC, need {amount:.2f}"
            })
        # Ask the miner container to sign
        timestamp  = time.time()
        miner_url  = miner_registry[from_miner_id]
        import hashlib as _hs, json as _j
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(f"{miner_url}/sign_transfer", json={
                    "to_addr": to_address, "amount": amount, "timestamp": timestamp,
                })
            if resp.status_code != 200:
                return JSONResponse(status_code=502, content={
                    "error": f"Miner signing failed: {resp.text}"
                })
            signed = resp.json()
            tx_id  = signed["tx_id"]
            sig    = signed["signature"]
        except Exception as exc:
            return JSONResponse(status_code=502, content={"error": str(exc)})
    else:
        return JSONResponse(status_code=400, content={
            "error": "Provide from_username or from_miner_id"
        })

    tx_dict = {
        "tx_id":     tx_id,
        "from_addr": sender_address,
        "to_addr":   to_address,
        "amount":    amount,
        "timestamp": timestamp,
        "signature": sig,
    }
    asyncio.create_task(_mine_transfer_block(tx_dict))

    return JSONResponse(status_code=200, content={
        "status":  "mining",
        "tx_id":   tx_id,
        "amount":  amount,
        "message": f"Sending {amount} RNC — confirming...",
    })


@app.get("/portal/history/{address}")
async def portal_history(address: str) -> dict:
    """
    Return all confirmed transactions involving a given address.
    """
    if blockchain is None:
        return {"transactions": []}
    txs = []
    for block in blockchain.chain:
        for tx in block.transactions:
            if not isinstance(tx, dict):
                continue
            if tx.get("from_addr") == address or tx.get("to_addr") == address:
                txs.append({**tx, "block_index": block.index,
                             "confirmed": block.index <= blockchain.confirmed_height})
    return {"transactions": sorted(txs, key=lambda t: t.get("timestamp", 0), reverse=True)}


@app.get("/portal/users")
async def portal_users() -> dict:
    """Return all portal usernames and addresses (for recipient dropdowns)."""
    return {"users": [
        {"username": u, "address": w.address}
        for u, w in portal_wallets.items()
    ]}


@app.get("/portal/addresses")
async def portal_all_addresses() -> dict:
    """Return every known address: miners + portal users (for send dropdowns)."""
    return {"addresses": _all_known_addresses()}


@app.get("/price_history")
async def get_price_history() -> dict:
    """
    Return the full RNC price history for the current simulation run.
    Used by the dashboard on page-load to pre-populate the price chart
    so late-joining browsers see the complete price history.
    """
    return {"history": list(_price_history)}


@app.get("/events")
async def events(request: Request) -> StreamingResponse:
    """
    Server-Sent Events endpoint.

    Each connected browser client gets its own asyncio.Queue.  The node
    pushes event dicts into every client queue; this async generator reads
    from the client's personal queue and yields SSE-formatted strings.

    Recent history is replayed for clients that join mid-simulation.
    A heartbeat comment is sent every 20 s to prevent proxy timeouts.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _sse_clients.append(queue)

    # Replay recent event history so a late-joining browser has context
    for entry in list(_event_log):
        await queue.put({"type": "log", "data": entry})

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20.0)
                    event_type = event.get("type", "log")
                    data = event["data"]
                    # Dicts must be JSON-serialized; strings sent as-is
                    if isinstance(data, dict):
                        data = json.dumps(data)
                    yield f"event: {event_type}\ndata: {data}\n\n"
                    # Do NOT break on shutdown — keep stream open so live market
                    # price updates and mempool trades keep reaching the browser
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"  # Keep the connection alive
        finally:
            if queue in _sse_clients:
                _sse_clients.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering if present
        },
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    """Serve the live mining dashboard (HTML injected with server config)."""
    return DASHBOARD_HTML


# ---------------------------------------------------------------------------
# Dashboard HTML
# Inline to keep the project self-contained — no templates/ folder needed.
# Uses vanilla JS + EventSource (no frameworks, no build step).
# ---------------------------------------------------------------------------

DASHBOARD_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>CryptoSim — Live Mining Dashboard</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0d1117;
      color: #c9d1d9;
      font-family: 'Courier New', monospace;
      padding: 24px;
    }}
    h1 {{ color: #f0883e; margin-bottom: 6px; font-size: 1.6rem; }}
    .subtitle {{ color: #8b949e; margin-bottom: 24px; font-size: 0.85rem; }}

    /* ── Fork banner ──────────────────────────────────────────────────── */
    #fork-banner {{
      display: none;
      background: #2d1a00;
      border: 1px solid #f0883e;
      border-radius: 8px;
      padding: 10px 18px;
      margin-bottom: 18px;
      font-size: 0.85rem;
      color: #f0883e;
      animation: fadeIn 0.3s ease;
    }}
    /* ── Lock icon for finalized blocks ───────────────────────────────── */
    .lock-icon {{
      color: #f0c040;
      font-size: 0.88rem;
      margin-left: 6px;
      title: 'Finalized';
    }}
    /* ── RennCoin leaderboard ─────────────────────────────────────────── */
    .leaderboard-section {{
      margin-top: 20px;
    }}
    .leaderboard-section h2 {{
      color: #58a6ff;
      font-size: 1rem;
      margin-bottom: 12px;
      border-bottom: 1px solid #21262d;
      padding-bottom: 8px;
    }}
    .leaderboard {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.8rem;
    }}
    .leaderboard th {{
      color: #8b949e;
      text-align: left;
      padding: 6px 12px;
      border-bottom: 1px solid #21262d;
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.8px;
    }}
    .leaderboard td {{
      padding: 8px 12px;
      border-bottom: 1px solid #161b22;
    }}
    .leaderboard tr:hover td {{ background: #161b22; }}
    .leaderboard .rnc-balance {{ color: #f0883e; font-weight: bold; }}
    .leaderboard .addr {{ color: #8b949e; font-size: 0.72rem; font-family: monospace; }}

    /* ── Setup screen ─────────────────────────────────────────────────── */
    #setup-screen {{
      display: flex;
      flex-direction: column;
      align-items: center;
      padding-top: 40px;
    }}
    #setup-screen h1 {{ margin-bottom: 8px; text-align: center; }}
    #setup-screen .subtitle {{ text-align: center; }}
    .setup-card {{
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 12px;
      padding: 32px 40px;
      width: 100%;
      max-width: 460px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }}
    .setup-card h2 {{
      color: #58a6ff;
      font-size: 1rem;
      border-bottom: 1px solid #21262d;
      padding-bottom: 10px;
    }}
    .reg-counter {{
      background: #0d1117;
      border: 1px solid #21262d;
      border-radius: 8px;
      padding: 12px 16px;
      text-align: center;
      font-size: 0.85rem;
      color: #8b949e;
    }}
    .reg-counter span {{
      color: #3fb950;
      font-size: 1.4rem;
      font-weight: bold;
    }}
    .form-group {{
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .form-group label {{
      color: #c9d1d9;
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.8px;
    }}
    .form-group input[type=number] {{
      background: #0d1117;
      border: 1px solid #30363d;
      border-radius: 6px;
      color: #f0883e;
      font-family: 'Courier New', monospace;
      font-size: 1.4rem;
      font-weight: bold;
      padding: 8px 14px;
      width: 100%;
      outline: none;
      transition: border-color 0.15s;
    }}
    .form-group input[type=number]:focus {{ border-color: #58a6ff; }}
    .form-group small {{
      color: #6e7681;
      font-size: 0.72rem;
    }}
    #btn-start {{
      background: #238636;
      border: 1px solid #2ea043;
      border-radius: 8px;
      color: #fff;
      cursor: pointer;
      font-family: 'Courier New', monospace;
      font-size: 1rem;
      font-weight: bold;
      padding: 12px;
      transition: background 0.15s, opacity 0.15s;
    }}
    #btn-start:hover:not(:disabled) {{ background: #2ea043; }}
    #btn-start:disabled {{ opacity: 0.4; cursor: not-allowed; }}
    .start-msg {{ color: #ff7b72; font-size: 0.78rem; min-height: 1.2em; text-align: center; }}

    /* ── Dashboard screen ─────────────────────────────────────────────── */
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
    .panel {{
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 8px;
      padding: 16px;
    }}
    .panel h2 {{
      color: #58a6ff;
      font-size: 1rem;
      margin-bottom: 12px;
      border-bottom: 1px solid #21262d;
      padding-bottom: 8px;
    }}
    .stats {{ display: flex; gap: 20px; margin-bottom: 24px; flex-wrap: wrap; }}
    .stat-box {{
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 8px;
      padding: 12px 20px;
      min-width: 130px;
      cursor: pointer;
      transition: border-color 0.15s, background 0.15s;
      user-select: none;
    }}
    .stat-box:hover {{
      border-color: #58a6ff;
      background: #1c2230;
    }}
    .stat-box .stat-hint {{
      color: #484f58;
      font-size: 0.62rem;
      margin-top: 5px;
    }}
    .stat-label {{
      color: #8b949e;
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 1px;
    }}
    .stat-value {{
      color: #f0883e;
      font-size: 1.8rem;
      font-weight: bold;
      margin-top: 4px;
    }}
    /* ── Info modal ──────────────────────────────────────────────────────── */
    #info-modal-overlay {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.65);
      z-index: 1000;
      align-items: center;
      justify-content: center;
    }}
    #info-modal-overlay.open {{ display: flex; }}
    #info-modal {{
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 10px;
      padding: 28px 32px;
      min-width: 340px;
      max-width: 540px;
      width: 90%;
      position: relative;
      box-shadow: 0 8px 32px rgba(0,0,0,0.6);
    }}
    #info-modal h3 {{
      margin: 0 0 14px;
      color: #e6edf3;
      font-size: 1.05rem;
    }}
    #info-modal p {{
      color: #8b949e;
      font-size: 0.88rem;
      line-height: 1.6;
      margin: 0 0 10px;
    }}
    #info-modal .modal-close {{
      position: absolute;
      top: 12px; right: 16px;
      background: none;
      border: none;
      color: #8b949e;
      font-size: 1.3rem;
      cursor: pointer;
      line-height: 1;
    }}
    #info-modal .modal-close:hover {{ color: #e6edf3; }}
    #info-modal table.modal-table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 10px;
      font-size: 0.83rem;
    }}
    #info-modal table.modal-table th {{
      text-align: left;
      color: #58a6ff;
      border-bottom: 1px solid #21262d;
      padding: 4px 8px;
    }}
    #info-modal table.modal-table td {{
      padding: 5px 8px;
      color: #e6edf3;
      border-bottom: 1px solid #21262d44;
    }}
    #info-modal table.modal-table td.rnc {{ color: #f0883e; font-weight: bold; }}
    #info-modal table.modal-table tr:last-child td {{ border-bottom: none; }}

    /* ── Wallet Portal Drawer ─────────────────────────────────────── */
    #wallet-drawer {{
      position: fixed;
      top: 0; right: 0;
      width: 340px;
      height: 100vh;
      background: #161b22;
      border-left: 1px solid #30363d;
      box-shadow: -4px 0 24px #0008;
      z-index: 300;
      display: flex;
      flex-direction: column;
      transform: translateX(100%);
      transition: transform 0.25s cubic-bezier(.4,0,.2,1);
      overflow: hidden;
    }}
    #wallet-drawer.open {{ transform: translateX(0); }}
    #wallet-drawer-overlay {{
      display: none;
      position: fixed;
      inset: 0;
      background: #0006;
      z-index: 299;
    }}
    #wallet-drawer-overlay.open {{ display: block; }}
    #drawer-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 16px 18px 14px;
      border-bottom: 1px solid #30363d;
      background: #0d1117;
      flex-shrink: 0;
    }}
    #drawer-header h2 {{
      margin: 0;
      font-size: 1rem;
      color: #e6edf3;
    }}
    #btn-close-drawer {{
      background: none;
      border: none;
      color: #8b949e;
      font-size: 1.3rem;
      cursor: pointer;
      line-height: 1;
      padding: 2px 6px;
    }}
    #btn-close-drawer:hover {{ color: #e6edf3; }}
    #drawer-body {{
      flex: 1;
      overflow-y: auto;
      padding: 18px;
    }}
    .drawer-screen {{ display: none; }}
    .drawer-screen.active {{ display: block; }}
    .drawer-field {{
      margin-bottom: 12px;
    }}
    .drawer-field label {{
      display: block;
      font-size: 0.78rem;
      color: #8b949e;
      margin-bottom: 4px;
    }}
    .drawer-field input, .drawer-field select {{
      width: 100%;
      box-sizing: border-box;
      background: #0d1117;
      border: 1px solid #30363d;
      border-radius: 6px;
      color: #e6edf3;
      padding: 7px 10px;
      font-size: 0.88rem;
      outline: none;
    }}
    .drawer-field input:focus, .drawer-field select:focus {{
      border-color: #58a6ff;
    }}
    .drawer-btn {{
      width: 100%;
      padding: 8px 0;
      background: #238636;
      color: #fff;
      border: none;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.92rem;
      font-weight: 600;
      margin-top: 4px;
    }}
    .drawer-btn:hover {{ background: #2ea043; }}
    .drawer-btn.secondary {{
      background: #21262d;
      color: #8b949e;
      border: 1px solid #30363d;
    }}
    .drawer-btn.secondary:hover {{ background: #30363d; color: #e6edf3; }}
    .drawer-btn.danger {{
      background: #21262d;
      color: #f85149;
      border: 1px solid #f8514940;
    }}
    .drawer-btn.danger:hover {{ background: #f8514922; }}
    #portal-wallet-addr {{
      font-size: 0.72rem;
      color: #8b949e;
      word-break: break-all;
      background: #0d1117;
      border: 1px solid #30363d;
      border-radius: 6px;
      padding: 6px 9px;
      margin-bottom: 12px;
    }}
    .portal-balance-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #0d1117;
      border: 1px solid #30363d;
      border-radius: 8px;
      padding: 10px 14px;
      margin-bottom: 10px;
    }}
    .portal-balance-row .pbl {{ font-size: 0.78rem; color: #8b949e; }}
    .portal-balance-row .pbv {{ font-size: 1.08rem; font-weight: bold; color: #f0883e; }}
    .portal-balance-row .pbv.usd {{ color: #3fb950; }}
    .drawer-divider {{
      border: none;
      border-top: 1px solid #30363d;
      margin: 16px 0;
    }}
    .drawer-section-title {{
      font-size: 0.78rem;
      color: #58a6ff;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      margin-bottom: 10px;
    }}
    #portal-msg {{
      font-size: 0.82rem;
      margin-top: 8px;
      min-height: 18px;
      color: #3fb950;
    }}
    #portal-msg.err {{ color: #f85149; }}
    #portal-history-list {{
      list-style: none;
      padding: 0; margin: 0;
      font-size: 0.8rem;
    }}
    #portal-history-list li {{
      display: flex;
      flex-direction: column;
      gap: 2px;
      padding: 7px 0;
      border-bottom: 1px solid #21262d;
      color: #8b949e;
    }}
    #portal-history-list li:last-child {{ border-bottom: none; }}
    #portal-history-list .htx-dir {{ color: #58a6ff; font-weight: 600; }}
    #portal-history-list .htx-dir.out {{ color: #f85149; }}
    #portal-history-list .htx-amt {{ color: #f0883e; font-weight: bold; }}
    .rate-hint {{ font-size: 0.75rem; color: #8b949e; margin-bottom: 10px; }}
    #btn-portal {{
      display: none;
      padding: 6px 16px;
      background: #1f6feb33;
      color: #58a6ff;
      border: 1px solid #1f6feb;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.88rem;
      font-weight: 600;
      margin-left: 12px;
      vertical-align: middle;
    }}
    #btn-portal:hover {{ background: #1f6feb55; }}
    .leaderboard tr[data-addr] {{ cursor: pointer; }}
    .leaderboard tr[data-addr]:hover td {{ background: #1f6feb11; }}

    #status-badge {{
      display: inline-block;
      padding: 3px 10px;
      border-radius: 10px;
      font-size: 0.72rem;
      background: #1f6feb33;
      color: #58a6ff;
      border: 1px solid #1f6feb;
      margin-left: 12px;
      vertical-align: middle;
    }}
    #status-badge.done {{
      background: #2ea04333;
      color: #3fb950;
      border-color: #2ea043;
    }}
    #btn-new-experiment {{
      display: inline-block;
      margin-top: 0.75rem;
      padding: 0.5rem 1.4rem;
      background: #238636;
      color: #fff;
      border: none;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.95rem;
      font-weight: 600;
      letter-spacing: 0.02em;
    }}
    #btn-new-experiment:hover {{ background: #2ea043; }}
    #btn-new-experiment:disabled {{ opacity: 0.6; cursor: default; }}
    #log {{
      height: 360px;
      overflow-y: auto;
      font-size: 0.78rem;
      line-height: 1.65;
      background: #0d1117;
      border-radius: 4px;
      padding: 8px;
    }}
    .log-line {{
      padding: 1px 0;
      border-bottom: 1px solid #161b22;
    }}
    .log-progress {{
      color: #484f58;
      font-size: 0.78rem;
      font-style: italic;
    }}
    #chain-display {{ height: 360px; overflow-y: auto; }}
    .block-card {{
      background: #0d1117;
      border: 1px solid #30363d;
      border-radius: 6px;
      padding: 10px 14px;
      margin-bottom: 8px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.8rem;
      animation: fadeIn 0.4s ease;
    }}
    @keyframes fadeIn {{
      from {{ opacity: 0; transform: translateY(-6px); }}
      to   {{ opacity: 1; transform: translateY(0);    }}
    }}
    .block-index {{ color: #f0883e; font-size: 1.1rem; font-weight: bold; min-width: 48px; }}
    .block-hash  {{ color: #3fb950; font-family: monospace; font-size: 0.75rem; }}
    .block-meta  {{ color: #8b949e; font-size: 0.72rem; text-align: right; line-height: 1.5; }}
    .chart-tf {{
      background: #21262d; border: 1px solid #30363d; color: #8b949e;
      padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 0.78rem;
      margin-right: 4px; font-family: inherit; transition: background 0.15s, color 0.15s;
    }}
    .chart-tf:hover {{ background: #30363d; color: #c9d1d9; }}
    .chart-tf.active {{ background: #f0883e22; border-color: #f0883e; color: #f0883e; }}
    .mc0 {{ color: #58a6ff; }}
    .mc1 {{ color: #f0883e; }}
    .mc2 {{ color: #bc8cff; }}
    .mc3 {{ color: #3fb950; }}
    .mc4 {{ color: #ff7b72; }}
    .mc5 {{ color: #ffa657; }}
    .mc6 {{ color: #79c0ff; }}
    .mc7 {{ color: #d2a8ff; }}
    .mc8 {{ color: #56d364; }}
    .mc9 {{ color: #ffa198; }}
  </style>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
</head>
<body>

  <!-- ── Setup Screen ──────────────────────────────────────────────────── -->
  <div id="setup-screen">
    <h1>&#x26CF; CryptoSim</h1>
    <p class="subtitle">Bitcoin-style proof-of-work simulation</p>

    <div class="setup-card">
      <h2>Configure Simulation</h2>

      <div class="reg-counter">
        <span id="reg-count">0</span> miners registered and ready
      </div>

      <div class="form-group">
        <label for="inp-miners">Number of Miners</label>
        <input type="number" id="inp-miners" min="1" max="10" value="3" />
        <small>How many miners compete to solve each block (1 &ndash; 10)</small>
      </div>

      <div class="form-group">
        <label for="inp-diff">Difficulty</label>
        <input type="number" id="inp-diff" min="1" max="8" value="{DIFFICULTY}" />
        <small>Leading zeros required in a valid hash &mdash; higher = slower (1 &ndash; 8)</small>
      </div>

      <button id="btn-start" disabled>&#x26CF; Start Mining</button>
      <p id="start-msg" class="start-msg"></p>
    </div>
  </div>

  <!-- ── Dashboard Screen ──────────────────────────────────────────────── -->
  <div id="dashboard-screen" style="display:none">
    <h1>&#x26CF; CryptoSim <span id="status-badge">MINING</span>
      <button id="btn-portal" onclick="togglePortal()">&#x1F4B3; My Wallet</button>
    </h1>
    <p class="subtitle" id="dash-subtitle">Bitcoin-style proof-of-work simulation</p>
    <button id="btn-new-experiment" onclick="resetSimulation()">&#x1F504; Run Another Experiment</button>

    <!-- Fork alert banner (hidden until a fork is detected) -->
    <div id="fork-banner">
      &#x26A1; Fork at block #<strong id="fork-height"></strong>!
      &nbsp; Canonical: <strong id="fork-canonical"></strong>
      &nbsp; Orphaned: <strong id="fork-orphan"></strong>
    </div>

    <div class="stats">
      <div class="stat-box">
        <div class="stat-label">Chain Length</div>
        <div class="stat-value" id="stat-length">1</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Blocks Mined</div>
        <div class="stat-value" id="stat-mined">0</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Active Miners</div>
        <div class="stat-value" id="stat-miners">-</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Difficulty</div>
        <div class="stat-value" id="stat-diff">-</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Target Blocks</div>
        <div class="stat-value" id="stat-target">-</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Mempool</div>
        <div class="stat-value" id="stat-mempool">0</div>
      </div>
      <div class="stat-box" id="stat-box-coins">
        <div class="stat-label">Total Coins</div>
        <div class="stat-value" id="stat-coins">0</div>
        <div class="stat-hint">click for breakdown</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">RNC Price</div>
        <div class="stat-value" id="stat-rnc-price">$10.00</div>
        <div class="stat-hint">USD per RNC</div>
      </div>
    </div>

    <!-- Info modal -->
    <div id="info-modal-overlay">
      <div id="info-modal">
        <button class="modal-close" id="modal-close-btn" title="Close">&times;</button>
        <h3 id="modal-title"></h3>
        <div id="modal-body"></div>
      </div>
    </div>

    <!-- ── Wallet Portal Drawer ─────────────────────────────────────── -->
    <div id="wallet-drawer-overlay" onclick="closePortal()"></div>
    <div id="wallet-drawer">
      <div id="drawer-header">
        <h2>&#x1F4B3; RennCoin Wallet</h2>
        <button id="btn-close-drawer" onclick="closePortal()" title="Close">&times;</button>
      </div>
      <div id="drawer-body">

        <!-- Screen: auth (login / create) -->
        <div class="drawer-screen active" id="portal-screen-auth">
          <div class="drawer-section-title">Sign In</div>
          <div class="drawer-field">
            <label for="portal-username">Username</label>
            <input type="text" id="portal-username" placeholder="e.g. alice" autocomplete="off"/>
          </div>
          <button class="drawer-btn" onclick="portalLogin()">Login</button>
          <hr class="drawer-divider"/>
          <div class="drawer-section-title">New Account</div>
          <div class="drawer-field">
            <label for="portal-new-username">Choose a username</label>
            <input type="text" id="portal-new-username" placeholder="e.g. bob" autocomplete="off"/>
          </div>
          <button class="drawer-btn secondary" onclick="portalCreate()">Create Wallet</button>
          <p id="portal-msg-auth"></p>
        </div>

        <!-- Screen: wallet home -->
        <div class="drawer-screen" id="portal-screen-home">
          <div class="drawer-section-title" id="portal-greeting">Hello!</div>
          <div id="portal-wallet-addr"></div>
          <div class="portal-balance-row">
            <span class="pbl">RNC Balance</span>
            <span class="pbv" id="portal-rnc-bal">0.00</span>
          </div>
          <div class="portal-balance-row">
            <span class="pbl">USD Balance</span>
            <span class="pbv usd" id="portal-usd-bal">$0.00</span>
          </div>
          <hr class="drawer-divider"/>

          <!-- Buy RNC -->
          <div class="drawer-section-title">Buy RNC</div>
          <div class="rate-hint">Rate: <strong id="rnc-rate-display">$10.00 USD = 1 RNC</strong></div>
          <div class="drawer-field">
            <label for="portal-buy-usd">USD amount to spend</label>
            <input type="number" id="portal-buy-usd" min="10" step="10" placeholder="100"/>
          </div>
          <button class="drawer-btn" onclick="buyRnc()">Buy RNC</button>
          <hr class="drawer-divider"/>

          <!-- Sell RNC -->
          <div class="drawer-section-title">Sell RNC</div>
          <div class="rate-hint">Rate: <strong id="rnc-sell-rate">$10.00 USD = 1 RNC</strong></div>
          <div class="drawer-field">
            <label for="portal-sell-rnc">RNC amount to sell</label>
            <input type="number" id="portal-sell-rnc" min="0.01" step="0.01" placeholder="1.00"/>
          </div>
          <button class="drawer-btn secondary" onclick="sellRnc()">Sell RNC</button>
          <hr class="drawer-divider"/>

          <!-- Send RNC -->
          <div class="drawer-section-title">Send RNC</div>
          <div class="drawer-field">
            <label for="portal-send-to">Recipient address</label>
            <select id="portal-send-to">
              <option value="">— select recipient —</option>
            </select>
          </div>
          <div class="drawer-field">
            <label for="portal-send-amt">Amount (RNC)</label>
            <input type="number" id="portal-send-amt" min="0.01" step="0.01" placeholder="1.00"/>
          </div>
          <button class="drawer-btn secondary" onclick="sendRnc()">Send RNC</button>
          <hr class="drawer-divider"/>

          <!-- Transaction History -->
          <div class="drawer-section-title">Transaction History</div>
          <ul id="portal-history-list"><li style="color:#8b949e">No transactions yet.</li></ul>
          <p id="portal-msg"></p>
          <hr class="drawer-divider"/>
          <button class="drawer-btn danger" onclick="portalLogout()">Logout</button>
        </div>

      </div><!-- /drawer-body -->
    </div><!-- /wallet-drawer -->

    <!-- RNC Price Chart -->
    <div class="leaderboard-section panel" id="price-chart-section" style="margin-top:16px">
      <h2>&#x1F4C8; RNC Price History&nbsp;<small style="color:#8b949e;font-size:0.75rem" id="chart-resolution-hint">Every trade &mdash; USD per RNC</small></h2>
      <div style="margin-bottom:10px">
        <span style="color:#8b949e;font-size:0.78rem;margin-right:10px">Resolution:</span>
        <button class="chart-tf active" onclick="setChartWindow(null,this)" title="Show every individual trade">All trades</button>
        <button class="chart-tf" onclick="setChartWindow(1,this)" title="Show closing price per 1-minute bucket">1 min</button>
        <button class="chart-tf" onclick="setChartWindow(10,this)" title="Show closing price per 10-minute bucket">10 min</button>
        <button class="chart-tf" onclick="setChartWindow(30,this)" title="Show closing price per 30-minute bucket">30 min</button>
        <button class="chart-tf" onclick="setChartWindow(60,this)" title="Show closing price per hour">1 hr</button>
        <button class="chart-tf" onclick="setChartWindow(1440,this)" title="Show closing price per day">1 day</button>
      </div>
      <div style="position:relative;height:240px;padding-top:4px">
        <canvas id="price-chart-canvas"></canvas>
      </div>
    </div>

    <div class="grid">
      <div class="panel">
        <h2>Event Log</h2>
        <div id="log"></div>
      </div>
      <div class="panel">
        <h2>Chain</h2>
        <div id="chain-display">
          <div class="block-card" data-index="0">
            <span class="block-index">#0</span>
            <span class="block-hash">Genesis Block</span>
            <span class="block-meta">NODE<br/>t=0.00s</span>
          </div>
        </div>
      </div>
    </div>

    <!-- RennCoin leaderboard -->
    <div class="leaderboard-section panel" style="margin-top:20px">
      <h2>&#x1F4B0; RennCoin Leaderboard&nbsp;<small style="color:#8b949e;font-size:0.75rem">(confirmed balances)</small></h2>
      <table class="leaderboard">
        <thead>
          <tr>
            <th>#</th>
            <th>Miner</th>
            <th>Wallet Address</th>
            <th>Balance (RNC)</th>
            <th>Blocks Won</th>
          </tr>
        </thead>
        <tbody id="leaderboard-body">
          <tr><td colspan="5" style="color:#6e7681;text-align:center;padding:16px">Waiting for first confirmed block&hellip;</td></tr>
        </tbody>
      </table>
    </div>

    <!-- Portal / User Wallets section -->
    <div class="leaderboard-section panel" id="portal-wallets-section" style="display:none;margin-top:16px">
      <h2>&#x1F4B3; Portal Wallets&nbsp;<small style="color:#8b949e;font-size:0.75rem">(confirmed on-chain balances)</small></h2>
      <table class="leaderboard">
        <thead>
          <tr>
            <th>User</th>
            <th>Wallet Address</th>
            <th>Balance (RNC)</th>
          </tr>
        </thead>
        <tbody id="portal-wallets-body"></tbody>
      </table>
    </div>
  </div>

  <script>
    const MINER_COLORS = ['mc0','mc1','mc2','mc3','mc4','mc5','mc6','mc7','mc8','mc9'];
    const minerColorMap = {{}};
    let minerColorIdx = 0;

    function colorClass(minerId) {{
      if (!(minerId in minerColorMap)) {{
        minerColorMap[minerId] = MINER_COLORS[minerColorIdx++ % MINER_COLORS.length];
      }}
      return minerColorMap[minerId];
    }}

    const logEl   = document.getElementById('log');
    const chainEl = document.getElementById('chain-display');
    let minedCount = 0;
    const blockCounts   = {{}}; // miner_id  → blocks won
    const latestBalances = {{}}; // address   → confirmed RNC balance
    // miner_id → wallet address (populated from /balances or balance_update events)
    const minerAddrs = {{}};
    // address → username for portal/user wallets
    const portalAddrs = {{}};

    // ── RNC Price chart ──────────────────────────────────────────────────
    const priceHistory = [];   // {{ ts: ms, label: str, price: float }}
    let priceChart = null;
    let activeBucketMin = null; // null = "All" (every raw point); number = bucket size in minutes

    // Build display dataset: null = every raw point; N = last price per N-minute bucket
    function _buildChartDataset() {{
      if (!activeBucketMin) {{
        // All — return every individual trade point
        return priceHistory.map(p => ({{ label: p.label, price: p.price }}));
      }}
      const bucketMs = activeBucketMin * 60000;
      const buckets = {{}}; // numeric key → {{ label, price }}
      for (const p of priceHistory) {{
        const key = Math.floor(p.ts / bucketMs);
        const d = new Date(key * bucketMs);
        let label;
        if (activeBucketMin >= 1440) {{
          label = d.toLocaleDateString('en-GB', {{ month: 'short', day: 'numeric' }});
        }} else {{
          label = d.toLocaleTimeString('en-GB', {{ hour: '2-digit', minute: '2-digit' }});
        }}
        // last price in bucket wins (closing price)
        buckets[key] = {{ label, price: p.price }};
      }}
      return Object.keys(buckets).sort((a,b) => a-b).map(k => buckets[k]);
    }}

    function applyChartWindow() {{
      if (!priceChart) return;
      const pts = _buildChartDataset();
      priceChart.data.labels = pts.map(p => p.label);
      priceChart.data.datasets[0].data = pts.map(p => p.price);
      // Update dot size: small for dense all-trades view, larger for bucketed
      priceChart.data.datasets[0].pointRadius = activeBucketMin ? 4 : (pts.length > 40 ? 2 : 3);
      priceChart.update('none');
    }}

    function setChartWindow(minutes, btn) {{
      activeBucketMin = minutes;
      document.querySelectorAll('.chart-tf').forEach(b => b.classList.remove('active'));
      if (btn) btn.classList.add('active');
      // Update subtitle hint
      const hint = document.getElementById('chart-resolution-hint');
      if (hint) {{
        if (!minutes) hint.textContent = 'Every trade \u2014 USD per RNC';
        else if (minutes < 60) hint.textContent = `Closing price per ${{minutes}}-minute interval \u2014 USD per RNC`;
        else if (minutes === 60) hint.textContent = 'Closing price per hour \u2014 USD per RNC';
        else hint.textContent = 'Closing price per day \u2014 USD per RNC';
      }}
      applyChartWindow();
    }}

    function pushPricePoint(price, skipPush = false) {{
      if (price == null) return;
      if (!skipPush) {{
        const now = new Date();
        const label = now.toLocaleTimeString('en-GB', {{hour:'2-digit', minute:'2-digit', second:'2-digit'}});
        priceHistory.push({{ ts: now.getTime(), label, price }});
      }}
      if (!priceChart) {{
        const canvas = document.getElementById('price-chart-canvas');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const grad = ctx.createLinearGradient(0, 0, 0, 240);
        grad.addColorStop(0, 'rgba(240,136,62,0.35)');
        grad.addColorStop(1, 'rgba(240,136,62,0.02)');
        const pts = _buildChartDataset();
        priceChart = new Chart(canvas, {{
          type: 'line',
          data: {{
            labels: pts.map(p => p.label),
            datasets: [{{
              label: 'RNC Price (USD)',
              data: pts.map(p => p.price),
              borderColor: '#f0883e',
              backgroundColor: grad,
              borderWidth: 2,
              tension: 0.35,
              pointRadius: 3,
              pointBackgroundColor: '#f0883e',
              fill: true,
            }}]
          }},
          options: {{
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            scales: {{
              x: {{ ticks: {{ color: '#8b949e', maxTicksLimit: 16 }}, grid: {{ color: '#21262d' }} }},
              y: {{ ticks: {{ color: '#8b949e', callback: v => '$' + v.toFixed(2) }}, grid: {{ color: '#21262d' }}, min: 0 }}
            }},
            plugins: {{
              legend: {{ labels: {{ color: '#c9d1d9' }} }},
              tooltip: {{ callbacks: {{ label: c => ' $' + c.parsed.y.toFixed(2) + ' USD' }} }}
            }}
          }}
        }});
      }} else {{
        applyChartWindow();
      }}
    }}

    // ── Modal helpers ─────────────────────────────────────────────────────
    const modalOverlay = document.getElementById('info-modal-overlay');
    const modalTitle   = document.getElementById('modal-title');
    const modalBody    = document.getElementById('modal-body');

    function openModal(title, bodyHTML) {{
      modalTitle.textContent = title;
      modalBody.innerHTML = bodyHTML;
      modalOverlay.classList.add('open');
    }}
    function closeModal() {{ modalOverlay.classList.remove('open'); }}
    document.getElementById('modal-close-btn').addEventListener('click', closeModal);
    modalOverlay.addEventListener('click', (e) => {{ if (e.target === modalOverlay) closeModal(); }});
    document.addEventListener('keydown', (e) => {{ if (e.key === 'Escape') closeModal(); }});

    function coinsBreakdownHTML() {{
      const miners = Object.keys(blockCounts);
      if (!miners.length) return '<p>No blocks mined yet.</p>';
      const rows = miners
        .map(mid => ({{ mid, addr: minerAddrs[mid] || '', bal: latestBalances[minerAddrs[mid]] || 0, won: blockCounts[mid] || 0 }}))
        .sort((a, b) => b.bal - a.bal || b.won - a.won);
      const totalCoins = rows.reduce((s, r) => s + r.bal, 0);
      const tableRows = rows.map((r, i) => {{
        const addrShort = r.addr.length > 14 ? r.addr.slice(0,8)+'\u2026'+r.addr.slice(-6) : (r.addr || '\u2014');
        const pct = totalCoins > 0 ? ((r.bal / totalCoins) * 100).toFixed(1) : '0.0';
        return `<tr><td>${{i+1}}</td><td>${{r.mid}}</td><td style="color:#8b949e;font-family:monospace;font-size:0.78rem">${{addrShort}}</td><td class="rnc">${{r.bal.toFixed(2)}} RNC</td><td>${{r.won}}</td><td>${{pct}}%</td></tr>`;
      }}).join('');
      return `<p style="color:#8b949e;font-size:0.82rem">Only <strong style="color:#e6edf3">confirmed</strong> (finalized) coinbase rewards are shown.</p>
        <table class="modal-table">
          <thead><tr><th>#</th><th>Miner</th><th>Wallet</th><th>Balance</th><th>Blocks</th><th>Share</th></tr></thead>
          <tbody>${{tableRows}}</tbody>
        </table>`;
    }}

    // ── Stat-box click handlers ───────────────────────────────────────────
    [
      ['stat-length',  'Chain Length',  'The total number of blocks in the chain, including the genesis block. Every accepted block increments this counter.'],
      ['stat-mined',   'Blocks Mined',  'How many blocks have been successfully solved and accepted by the node during this simulation run.'],
      ['stat-miners',  'Active Miners', 'The number of miner containers selected to compete in this simulation. Each runs an independent proof-of-work loop.'],
      ['stat-diff',    'Difficulty',    'The number of leading hex zeros required in a valid block hash. Each additional zero makes mining ~16\u00d7 harder on average.'],
      ['stat-target',  'Target Blocks', 'The simulation ends once this many blocks have been mined. Set before clicking Start.'],
      ['stat-mempool', 'Mempool',       'Pending transactions waiting to be included in a block. Miners pull up to 5 per block from this pool.'],
    ].forEach(([id, title, desc]) => {{
      const el = document.getElementById(id);
      if (el) el.closest('.stat-box').addEventListener('click', () => openModal(title, `<p>${{desc}}</p>`));
    }});
    document.getElementById('stat-box-coins').addEventListener('click', () =>
      openModal('💰 Total Coins Distributed', coinsBreakdownHTML())
    );

    function refreshTotalCoins() {{
      const total = Object.values(latestBalances).reduce((s, v) => s + v, 0);
      const el = document.getElementById('stat-coins');
      if (el) el.textContent = total.toFixed(0);
    }}

    function appendLog(text, style) {{
      const div = document.createElement('div');
      div.className = 'log-line' + (style ? ' log-' + style : '');
      div.innerHTML = text.replace(/(miner[_=\\w]+)/g, (match) => {{
        const id = match.replace('miner=', '');
        return `<span class="${{colorClass(id)}}">${{match}}</span>`;
      }});
      logEl.appendChild(div);
      logEl.scrollTop = logEl.scrollHeight;
    }}

    function prependBlock(data) {{
      document.getElementById('stat-length').textContent = data.index + 1;
      const card = document.createElement('div');
      card.className = 'block-card';
      card.dataset.index = data.index;
      if (data.miner === 'portal' && data.tx) {{
        // Transfer block — show who sent what to whom
        const fromLabel = portalAddrs[data.tx.from] || data.tx.from.slice(0,8)+'\u2026';
        const toLabel   = portalAddrs[data.tx.to]   || data.tx.to.slice(0,8)+'\u2026';
        card.innerHTML = `
          <span class="block-index">#${{data.index}}</span>
          <span class="block-hash" style="color:#f0883e">${{data.hash}}</span>
          <span class="block-meta" style="font-size:0.75rem;line-height:1.5">
            &#x1F4B8; transfer<br/>
            ${{fromLabel}} &rarr; ${{toLabel}}<br/>
            ${{data.tx.amount}} RNC
          </span>`;
      }} else {{
        minedCount++;
        document.getElementById('stat-mined').textContent = minedCount;
        blockCounts[data.miner] = (blockCounts[data.miner] || 0) + 1;
        const cc = colorClass(data.miner);
        card.innerHTML = `
          <span class="block-index">#${{data.index}}</span>
          <span class="block-hash ${{cc}}">${{data.hash}}</span>
          <span class="block-meta">
            <span class="${{cc}}">${{data.miner}}</span><br/>
            nonce:${{data.nonce.toLocaleString()}} &nbsp; ${{data.time}}s
          </span>`;
      }}
      chainEl.insertBefore(card, chainEl.firstChild);
      updateLeaderboard();
    }}

    /** Transition from setup screen to dashboard. */
    async function resetSimulation() {{
      const badge = document.getElementById('status-badge');
      if (badge && badge.textContent !== 'DONE') {{
        if (!confirm('Simulation is still running. Reset and start a new experiment?')) return;
      }}
      const btn = document.getElementById('btn-new-experiment');
      btn.disabled = true;
      btn.textContent = 'Resetting…';
      try {{ await fetch('/reset', {{method: 'POST'}}); }} catch(e) {{}}
      window.location.reload();
    }}

    function showDashboard(cfg) {{
      document.getElementById('setup-screen').style.display   = 'none';
      document.getElementById('dashboard-screen').style.display = '';
      const target = '0'.repeat(cfg.difficulty);
      document.getElementById('dash-subtitle').textContent =
        `RennCoin proof-of-work simulation — difficulty: ${{target}} — target: ${{cfg.target ?? cfg.num_blocks}} blocks`;
      document.getElementById('stat-diff').textContent   = cfg.difficulty;
      document.getElementById('stat-target').textContent = cfg.target ?? cfg.num_blocks;
      document.getElementById('stat-miners').textContent = cfg.num_miners ?? (cfg.active ? cfg.active.length : '-');
      const btnPortal = document.getElementById('btn-portal');
      if (btnPortal) btnPortal.style.display = 'inline-block';
    }}

    /** Flash the fork banner for 6 seconds. */
    function flashFork(data) {{
      document.getElementById('fork-height').textContent    = data.height;
      document.getElementById('fork-canonical').textContent = data.canonical_miner;
      document.getElementById('fork-orphan').textContent    = data.fork_miner;
      const banner = document.getElementById('fork-banner');
      banner.style.display = 'block';
      setTimeout(() => {{ banner.style.display = 'none'; }}, 6000);
    }}

    /** Add a lock icon to all block cards at or below the finalized height. */
    function markFinalized(height) {{
      document.querySelectorAll('.block-card[data-index]').forEach(card => {{
        if (parseInt(card.dataset.index) <= height) {{
          if (!card.querySelector('.lock-icon')) {{
            const lock = document.createElement('span');
            lock.className = 'lock-icon';
            lock.title = 'Finalized';
            lock.textContent = '🔒';
            const idxEl = card.querySelector('.block-index');
            if (idxEl) idxEl.appendChild(lock);
          }}
        }}
      }});
    }}

    /** Rebuild the RennCoin leaderboard table. */
    function updateLeaderboard() {{
      const tbody = document.getElementById('leaderboard-body');
      if (!tbody) return;
      const miners = Object.keys(blockCounts).filter(m => m !== 'portal');
      if (miners.length === 0) return;
      miners.sort((a, b) => {{
        const addrA = minerAddrs[a] || '';
        const addrB = minerAddrs[b] || '';
        const balA  = latestBalances[addrA] || 0;
        const balB  = latestBalances[addrB] || 0;
        if (balB !== balA) return balB - balA;
        return (blockCounts[b] || 0) - (blockCounts[a] || 0);
      }});
      tbody.innerHTML = miners.map((mid, i) => {{
        const addr = minerAddrs[mid] || '—';
        const bal  = latestBalances[addr] !== undefined
          ? latestBalances[addr].toFixed(2) : '0.00';
        const won  = blockCounts[mid] || 0;
        const cc   = colorClass(mid);
        const addrDisplay = addr.length > 16 ? addr.slice(0,8)+'…'+addr.slice(-6) : addr;
        const dataAddr = addr !== '—' ? `data-addr="${{addr}}"` : '';
        return `<tr ${{dataAddr}} title="${{addr !== '—' ? 'Click to send RNC to '+addr : ''}}" onclick="${{addr !== '—' ? 'prefillSendTo(\\''+addr+'\\')' : ''}}">
          <td>${{i+1}}</td>
          <td><span class="${{cc}}">${{mid}}</span></td>
          <td class="addr">${{addrDisplay}}</td>
          <td class="rnc-balance">${{bal}} RNC</td>
          <td>${{won}}</td>
        </tr>`;
      }}).join('');
    }}

    /** Rebuild the portal/user wallets table. */
    function updatePortalWallets() {{
      const entries = Object.entries(portalAddrs);
      const section = document.getElementById('portal-wallets-section');
      if (!section) return;
      if (entries.length === 0) {{ section.style.display = 'none'; return; }}
      section.style.display = '';
      const tbody = document.getElementById('portal-wallets-body');
      if (!tbody) return;
      entries.sort((a, b) => (latestBalances[b[0]] || 0) - (latestBalances[a[0]] || 0));
      tbody.innerHTML = entries.map(([addr, username]) => {{
        const bal = latestBalances[addr] !== undefined ? latestBalances[addr].toFixed(4) : '0.0000';
        const addrDisplay = addr.slice(0,8)+'\u2026'+addr.slice(-6);
        return `<tr data-addr="${{addr}}" title="Click to send RNC to ${{username}}" onclick="prefillSendTo('${{addr}}')">
          <td><strong style="color:#58a6ff">${{username}}</strong></td>
          <td class="addr">${{addrDisplay}}</td>
          <td class="rnc-balance">${{bal}} RNC</td>
        </tr>`;
      }}).join('');
    }}

    // ── On page load: check current state ────────────────────────────────
    (async () => {{
      try {{
        const cfg = await fetch('/config').then(r => r.json());
        document.getElementById('reg-count').textContent = cfg.registered_count;
        updateStartButton(cfg.registered_count);
        if (cfg.started) {{
          showDashboard({{ difficulty: cfg.difficulty, target: cfg.num_blocks, num_miners: cfg.active.length }});
          // Reload existing blocks from chain
          const chain = await fetch('/chain').then(r => r.json());
          (chain.chain || []).slice(1).forEach(b => {{
            minedCount++;
            document.getElementById('stat-length').textContent = b.index + 1;
            document.getElementById('stat-mined').textContent  = minedCount;
            const card = document.createElement('div');
            card.className = 'block-card';
            card.dataset.index = b.index;
            card.innerHTML = `
              <span class="block-index">#${{b.index}}</span>
              <span class="block-hash">${{b.hash.slice(0,16)}}...</span>
              <span class="block-meta">reloaded</span>`;
            chainEl.appendChild(card);
          }});
          // Seed leaderboard with confirmed balances
          try {{
            const bals = await fetch('/balances').then(r => r.json());
            if (bals.miner_addresses) {{
              Object.assign(minerAddrs, bals.miner_addresses);
            }}
            if (bals.balances) {{
              Object.assign(latestBalances, bals.balances);
            }}
            if (bals.portal_addresses) {{
              Object.assign(portalAddrs, bals.portal_addresses);
            }}
            // Seed block-won counts so leaderboard renders immediately
            if (bals.block_counts) {{
              Object.assign(blockCounts, bals.block_counts);
            }}
            // Show current RNC price and pre-populate chart with full history
            if (bals.rnc_price) {{
              const rateEl = document.getElementById('rnc-rate-display');
              if (rateEl) rateEl.textContent = '$' + bals.rnc_price.toFixed(2) + ' USD = 1 RNC';
              const sellEl = document.getElementById('rnc-sell-rate');
              if (sellEl) sellEl.textContent = '$' + bals.rnc_price.toFixed(2) + ' USD = 1 RNC';
              const statEl = document.getElementById('stat-rnc-price');
              if (statEl) statEl.textContent = '$' + bals.rnc_price.toFixed(2);
              // Fetch full price history so late-joining browsers see the whole chart
              try {{
                const ph = await fetch('/price_history').then(r => r.json());
                for (const pt of (ph.history || [])) {{
                  const d = new Date(pt.t);
                  const lbl = d.toLocaleTimeString('en-GB', {{hour:'2-digit', minute:'2-digit', second:'2-digit'}});
                  priceHistory.push({{ ts: pt.t, label: lbl, price: pt.price }});
                }}
              }} catch(e) {{ console.warn('Price history fetch failed', e); }}
              // skipPush=true when history already populated to avoid duplicating last point
              pushPricePoint(bals.rnc_price, priceHistory.length > 0);
            }}
            // Mark any already-finalized blocks
            if (chain.confirmed_height > 0) markFinalized(chain.confirmed_height);
            refreshTotalCoins();
            updateLeaderboard();
            updatePortalWallets();
          }} catch(e) {{ console.warn('Balances fetch failed', e); }}
        }}
      }} catch(e) {{ console.warn('Config fetch failed', e); }}
    }})();

    // ── Setup form logic ──────────────────────────────────────────────────
    function updateStartButton(count) {{
      const btn    = document.getElementById('btn-start');
      const inp    = document.getElementById('inp-miners');
      btn.disabled = count < 1;
      inp.max      = count;
      if (parseInt(inp.value) > count) inp.value = count;
    }}

    document.getElementById('btn-start').addEventListener('click', async () => {{
      const numMiners = parseInt(document.getElementById('inp-miners').value);
      const difficulty = parseInt(document.getElementById('inp-diff').value);
      const msgEl = document.getElementById('start-msg');
      msgEl.textContent = '';

      const resp = await fetch('/start', {{
        method:  'POST',
        headers: {{'Content-Type': 'application/json'}},
        body:    JSON.stringify({{ num_miners: numMiners, difficulty: difficulty }}),
      }});
      if (!resp.ok) {{
        const data = await resp.json().catch(() => ({{}}));
        msgEl.textContent = data.error || `Error ${{resp.status}}`;
      }}
      // On success the SSE 'started' event drives the transition
    }});

    // ── SSE ───────────────────────────────────────────────────────────────
    const es = new EventSource('/events');

    es.addEventListener('miner_registered', (e) => {{
      const data = JSON.parse(e.data);
      document.getElementById('reg-count').textContent = data.count;
      updateStartButton(data.count);
    }});

    es.addEventListener('started', (e) => {{
      const cfg = JSON.parse(e.data);
      showDashboard(cfg);
      // Store miner addresses if provided
      if (cfg.miner_addresses) Object.assign(minerAddrs, cfg.miner_addresses);
      // Show wallet portal button
      const btnPortal = document.getElementById('btn-portal');
      if (btnPortal) btnPortal.style.display = 'inline-block';
    }});

    es.addEventListener('log', (e) => {{
      appendLog(e.data);
    }});

    es.addEventListener('block', (e) => {{
      prependBlock(JSON.parse(e.data));
    }});

    es.addEventListener('shutdown', () => {{
      appendLog('[SYSTEM] Simulation complete \u2014 all target blocks mined.');
      const badge = document.getElementById('status-badge');
      badge.textContent = 'DONE';
      badge.className   = 'done';
      // Keep SSE open so live market activity (price, mempool trades) keeps streaming
      document.getElementById('btn-new-experiment').style.display = 'inline-block';
    }});

    es.addEventListener('fork', (e) => {{
      flashFork(JSON.parse(e.data));
    }});

    es.addEventListener('finalized', (e) => {{
      const {{height}} = JSON.parse(e.data);
      markFinalized(height);
    }});

    es.addEventListener('balance_update', (e) => {{
      const {{balances, miner_addresses, portal_addresses, block_counts, rnc_price}} = JSON.parse(e.data);
      if (balances) Object.assign(latestBalances, balances);
      if (miner_addresses) Object.assign(minerAddrs, miner_addresses);
      if (portal_addresses) Object.assign(portalAddrs, portal_addresses);
      if (block_counts) Object.assign(blockCounts, block_counts);
      if (rnc_price) {{
        const rateEl = document.getElementById('rnc-rate-display');
        if (rateEl) rateEl.textContent = '$' + rnc_price.toFixed(2) + ' USD = 1 RNC';
        const sellEl = document.getElementById('rnc-sell-rate');
        if (sellEl) sellEl.textContent = '$' + rnc_price.toFixed(2) + ' USD = 1 RNC';
        const statEl = document.getElementById('stat-rnc-price');
        if (statEl) statEl.textContent = '$' + rnc_price.toFixed(2);
        pushPricePoint(rnc_price);
      }}
      refreshTotalCoins();
      updateLeaderboard();
      updatePortalWallets();
      if (portalUser) {{ refreshPortalBalances(); refreshAddressDropdown(); }}
    }});

    es.addEventListener('user_registered', (e) => {{
      // A new portal user was created — refresh the recipient dropdown for all logged-in users
      if (portalUser) refreshAddressDropdown();
    }});

    es.addEventListener('mempool_update', (e) => {{
      const {{count}} = JSON.parse(e.data);
      const el = document.getElementById('stat-mempool');
      if (el) el.textContent = count;
    }});

    es.addEventListener('peer_validated', (e) => {{
      const d = JSON.parse(e.data);
      const check = d.valid ? '✓' : '✗';
      appendLog(`[P2P] ${{d.validator}} verified block #${{d.block_index}} ${{check}}`);
    }});

    es.addEventListener('mining_progress', (e) => {{
      const d = JSON.parse(e.data);
      const rateK = (d.rate / 1000).toFixed(1);
      const nonceStr = d.nonce.toLocaleString();
      appendLog(
        `\u26cf ${{d.miner_id}} \u2502 block #${{d.block_index}} \u2502 nonce=${{nonceStr}} \u2502 ${{rateK}}k h/s \u2502 ${{d.hash_prefix}}\u2026`,
        'progress'
      );
    }});

    es.addEventListener('transfer_confirmed', (e) => {{
      const d = JSON.parse(e.data);
      appendLog(`[PORTAL] Transfer confirmed: ${{d.amount}} RNC → ${{d.to_addr.slice(0,8)}}…`);
      if (portalUser) {{
        refreshPortalBalances();
        loadHistory();
      }}
    }});

    es.onerror = () => {{
      // Don't close — EventSource will auto-reconnect, which replays event history
      appendLog('[SYSTEM] SSE connection interrupted — reconnecting…');
    }};

    // ── Portal / Wallet Drawer ────────────────────────────────────────────
    let portalUser = null; // {{username, address}}

    function togglePortal() {{
      const drawer = document.getElementById('wallet-drawer');
      if (drawer.classList.contains('open')) {{
        closePortal();
      }} else {{
        openPortal();
      }}
    }}

    function openPortal() {{
      document.getElementById('wallet-drawer').classList.add('open');
      document.getElementById('wallet-drawer-overlay').classList.add('open');
      if (portalUser) {{
        loadHistory();
        refreshAddressDropdown();
      }}
    }}

    function closePortal() {{
      document.getElementById('wallet-drawer').classList.remove('open');
      document.getElementById('wallet-drawer-overlay').classList.remove('open');
    }}

    function showDrawerScreen(id) {{
      document.querySelectorAll('.drawer-screen').forEach(s => s.classList.remove('active'));
      const el = document.getElementById(id);
      if (el) el.classList.add('active');
    }}

    function setPortalMsg(msg, isErr) {{
      const el = document.getElementById('portal-msg');
      if (!el) return;
      el.textContent = msg;
      el.className = isErr ? 'err' : '';
    }}

    function setAuthMsg(msg, isErr) {{
      const el = document.getElementById('portal-msg-auth');
      if (!el) return;
      el.textContent = msg;
      el.className = isErr ? 'err' : '';
    }}

    async function portalLogin() {{
      const username = document.getElementById('portal-username').value.trim();
      if (!username) {{ setAuthMsg('Enter a username.', true); return; }}
      setAuthMsg('', false);
      try {{
        const r = await fetch('/portal/login', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{username}})
        }});
        const data = await r.json();
        if (!r.ok) {{ setAuthMsg(data.error || data.detail || 'Login failed.', true); return; }}
        portalUser = {{username: data.username, address: data.address}};
        renderPortalHome(data);
        showDrawerScreen('portal-screen-home');
        refreshAddressDropdown();
        loadHistory();
      }} catch(e) {{ setAuthMsg('Network error.', true); }}
    }}

    async function portalCreate() {{
      const username = document.getElementById('portal-new-username').value.trim();
      if (!username) {{ setAuthMsg('Choose a username.', true); return; }}
      setAuthMsg('', false);
      try {{
        const r = await fetch('/portal/create_wallet', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{username}})
        }});
        const data = await r.json();
        if (!r.ok) {{ setAuthMsg(data.error || data.detail || 'Create failed.', true); return; }}
        portalUser = {{username: data.username, address: data.address}};
        renderPortalHome(data);
        showDrawerScreen('portal-screen-home');
        refreshAddressDropdown();
        loadHistory();
      }} catch(e) {{ setAuthMsg('Network error.', true); }}
    }}

    function renderPortalHome(data) {{
      document.getElementById('portal-greeting').textContent = 'Hello, ' + data.username + '!';
      document.getElementById('portal-wallet-addr').textContent = data.address;
      document.getElementById('portal-rnc-bal').textContent = (data.rnc_balance || 0).toFixed(4) + ' RNC';
      document.getElementById('portal-usd-bal').textContent = '$' + (data.usd_balance || 0).toFixed(2);
      setPortalMsg('', false);
    }}

    async function refreshPortalBalances() {{
      if (!portalUser) return;
      try {{
        const r = await fetch('/portal/login', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{username: portalUser.username}})
        }});
        if (!r.ok) return;
        const data = await r.json();
        document.getElementById('portal-rnc-bal').textContent = (data.rnc_balance || 0).toFixed(4) + ' RNC';
        document.getElementById('portal-usd-bal').textContent = '$' + (data.usd_balance || 0).toFixed(2);
        if (data.rnc_price) {{
          const rateEl = document.getElementById('rnc-rate-display');
          if (rateEl) rateEl.textContent = '$' + data.rnc_price.toFixed(2) + ' USD = 1 RNC';
          const sellEl = document.getElementById('rnc-sell-rate');
          if (sellEl) sellEl.textContent = '$' + data.rnc_price.toFixed(2) + ' USD = 1 RNC';
          const statEl = document.getElementById('stat-rnc-price');
          if (statEl) statEl.textContent = '$' + data.rnc_price.toFixed(2);
          pushPricePoint(data.rnc_price);
        }}
      }} catch(_) {{}}
    }}

    async function buyRnc() {{
      if (!portalUser) return;
      const usdAmt = parseFloat(document.getElementById('portal-buy-usd').value);
      if (isNaN(usdAmt) || usdAmt <= 0) {{ setPortalMsg('Enter a USD amount.', true); return; }}
      setPortalMsg('Processing…', false);
      try {{
        const r = await fetch('/portal/buy_rnc', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{username: portalUser.username, usd_amount: usdAmt}})
        }});
        const data = await r.json();
        if (!r.ok) {{ setPortalMsg(data.error || data.detail || 'Buy failed.', true); return; }}
        setPortalMsg('Bought ' + (data.rnc_amount || data.rnc_received || 0).toFixed(4) + ' RNC!', false);
        document.getElementById('portal-buy-usd').value = '';
        await refreshPortalBalances();
        loadHistory();
      }} catch(e) {{ setPortalMsg('Network error.', true); }}
    }}

    async function sellRnc() {{
      if (!portalUser) return;
      const rncAmt = parseFloat(document.getElementById('portal-sell-rnc').value);
      if (isNaN(rncAmt) || rncAmt <= 0) {{ setPortalMsg('Enter an RNC amount.', true); return; }}
      setPortalMsg('Processing…', false);
      try {{
        const r = await fetch('/portal/sell_rnc', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{username: portalUser.username, rnc_amount: rncAmt}})
        }});
        const data = await r.json();
        if (!r.ok) {{ setPortalMsg(data.error || data.detail || 'Sell failed.', true); return; }}
        setPortalMsg('Sold ' + rncAmt.toFixed(4) + ' RNC for $' + (data.usd_received || 0).toFixed(2) + '!', false);
        document.getElementById('portal-sell-rnc').value = '';
        await refreshPortalBalances();
        loadHistory();
      }} catch(e) {{ setPortalMsg('Network error.', true); }}
    }}

    async function sendRnc() {{
      if (!portalUser) return;
      const toAddr = document.getElementById('portal-send-to').value;
      const amount = parseFloat(document.getElementById('portal-send-amt').value);
      if (!toAddr) {{ setPortalMsg('Select a recipient.', true); return; }}
      if (isNaN(amount) || amount <= 0) {{ setPortalMsg('Enter an amount.', true); return; }}
      setPortalMsg('Processing…', false);
      try {{
        const r = await fetch('/portal/send_rnc', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{from_username: portalUser.username, to_address: toAddr, amount}})
        }});
        const data = await r.json();
        if (!r.ok) {{ setPortalMsg(data.error || data.detail || 'Send failed.', true); return; }}
        setPortalMsg('Sent ' + amount.toFixed(4) + ' RNC!', false);
        document.getElementById('portal-send-amt').value = '';
        await refreshPortalBalances();
        loadHistory();
      }} catch(e) {{ setPortalMsg('Network error.', true); }}
    }}

    async function loadHistory() {{
      if (!portalUser) return;
      try {{
        const r = await fetch('/portal/history/' + portalUser.address);
        if (!r.ok) return;
        const txs = await r.json();
        const ul = document.getElementById('portal-history-list');
        if (!txs.length) {{
          ul.innerHTML = '<li style="color:#8b949e">No transactions yet.</li>';
          return;
        }}
        ul.innerHTML = txs.slice().reverse().map(tx => {{
          const isOut = tx.from_addr === portalUser.address;
          const dir   = isOut ? 'OUT' : 'IN';
          const other = isOut ? tx.to_addr : tx.from_addr;
          const otherShort = other.length > 16 ? other.slice(0,8)+'…'+other.slice(-6) : other;
          return `<li>
            <span class="htx-dir ${{isOut ? 'out' : ''}}">${{dir}}</span>
            <span class="htx-amt">${{(+tx.amount).toFixed(4)}} RNC</span>
            <span>${{isOut ? 'to' : 'from'}} ${{otherShort}}</span>
          </li>`;
        }}).join('');
      }} catch(_) {{}}
    }}

    async function refreshAddressDropdown() {{
      try {{
        const r = await fetch('/portal/addresses');
        if (!r.ok) return;
        const {{ addresses: addrs }} = await r.json(); // {{addresses: [{{label, address}}, ...]}}
        if (!Array.isArray(addrs)) return;
        const sel = document.getElementById('portal-send-to');
        const prev = sel.value;
        sel.innerHTML = '<option value="">— select recipient —</option>';
        addrs.forEach(a => {{
          if (a.address === portalUser?.address) return; // skip self
          const opt = document.createElement('option');
          opt.value = a.address;
          opt.textContent = a.label + ' (' + a.address.slice(0,8) + '…)';
          sel.appendChild(opt);
        }});
        if (prev) sel.value = prev;
      }} catch(_) {{}}
    }}

    function prefillSendTo(addr) {{
      openPortal();
      if (!portalUser) return;
      // Give drawer time to open
      setTimeout(() => {{
        const sel = document.getElementById('portal-send-to');
        if (sel) sel.value = addr;
        const amtEl = document.getElementById('portal-send-amt');
        if (amtEl) amtEl.focus();
      }}, 120);
    }}

    function portalLogout() {{
      portalUser = null;
      document.getElementById('portal-username').value = '';
      document.getElementById('portal-new-username').value = '';
      showDrawerScreen('portal-screen-auth');
      setPortalMsg('Logged out.', false);
    }}

    // Show portal button once simulation starts (handled in started SSE above)

  </script>
</body>
</html>"""
