"""
miner.py — A miner container.

Each miner:
  1. On startup, registers with the node via POST /register to get the
     current chain tip (so it knows which block to build on top of).
  2. Runs a background OS thread that repeatedly increments a nonce,
     SHA-256-hashes the candidate block, and checks the difficulty target.
  3. Exposes POST /new_block so the node can PUSH accepted blocks,
     causing the mining thread to abandon its current nonce search and
     immediately begin mining the next block height.
  4. Exposes POST /shutdown so the node can stop the miner cleanly once
     the simulation target block count is reached.

Why a background thread and not asyncio?
  The proof-of-work loop is pure CPU work (tight loop, no I/O).  Running
  it in an asyncio coroutine would starve the FastAPI event loop because
  Python coroutines are cooperative — a tight loop never yields.  A
  threading.Thread gives the OS scheduler true pre-emptive control so
  the HTTP routes remain responsive while mining is in progress.

Environment variables:
  NODE_URL    Base URL of the node       (default: http://node:8182)
  MINER_ID    Human-readable name        (default: miner_unknown)
  MINER_URL   This container's callback  (default: http://miner_1:8001)
  DIFFICULTY  Leading-zero target        (default: 4)
"""

import logging
import os
import threading
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from block import Block

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NODE_URL   = os.environ.get("NODE_URL",   "http://node:8182")
MINER_ID   = os.environ.get("MINER_ID",   "miner_unknown")
MINER_URL  = os.environ.get("MINER_URL",  "http://miner_1:8001")
DIFFICULTY = int(os.environ.get("DIFFICULTY", 4))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [{MINER_ID.upper()}] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared mining state
# Accessed from both the FastAPI event loop (via /new_block route) and the
# background mining thread — protected by _lock.
# ---------------------------------------------------------------------------

_lock = threading.Lock()

# Threading events used to coordinate the mining loop:
#   _stop_event  — set when the current block height is won (by any miner)
#                  so the loop resets and starts the next height
#   _shutdown    — set once, permanently, when the simulation ends
_stop_event = threading.Event()
_shutdown   = threading.Event()

# Latest chain tip known to this miner (updated by /new_block handler)
_current_tip: dict = {"index": 0, "hash": ""}

# ---------------------------------------------------------------------------
# Lifespan — runs once when the container starts
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Retry registering with the node until it accepts us.
    # Docker starts containers in parallel, so the node may not be ready yet.
    reg = _register_with_node()
    status = reg.get("status", "registered")

    if status == "waiting":
        # Node is up but the user hasn't clicked Start yet — poll /config.
        logger.info("Waiting for simulation to start...")
        activated, config = _wait_for_activation()
        if not activated:
            logger.info("Not selected for this simulation run — staying idle")
            yield
            _shutdown.set()
            _stop_event.set()
            return
        tip = _get_chain_tip()
        mining_target = "0" * config["difficulty"]

    elif status == "active":
        tip = reg["tip"]
        mining_target = "0" * reg["difficulty"]

    else:
        # status == "idle" — sim already running but this miner was not selected
        logger.info("Not selected for this simulation run — staying idle")
        yield
        _shutdown.set()
        _stop_event.set()
        return

    with _lock:
        _current_tip["index"] = tip["index"]
        _current_tip["hash"]  = tip["hash"]

    # Launch the CPU-intensive mining loop in a background OS thread
    t = threading.Thread(
        target=_mining_loop,
        args=(mining_target,),
        daemon=True,
        name=f"miner-{MINER_ID}",
    )
    t.start()
    logger.info(
        "Mining thread started — tip is block #%d, target='%s'",
        tip["index"], mining_target,
    )

    yield

    # Graceful shutdown on SIGTERM / container stop
    logger.info("Miner shutting down")
    _shutdown.set()
    _stop_event.set()


app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _register_with_node() -> dict:
    """
    POST /register to the node with a retry loop.

    Returns the full registration response dict, e.g.:
      {"status": "waiting"}                                     — sim not yet started
      {"status": "active", "tip": {...}, "difficulty": N}      — sim running, we're in
      {"status": "idle"}                                        — sim running, not selected
    """
    payload = {"miner_id": MINER_ID, "callback_url": MINER_URL}
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = httpx.post(f"{NODE_URL}/register", json=payload, timeout=5.0)
            resp.raise_for_status()
            data = resp.json()
            logger.info(
                "Registered with node (attempt %d) — status: %s",
                attempt, data.get("status"),
            )
            return data
        except Exception as exc:
            logger.warning(
                "Node not ready (attempt %d): %s — retrying in 2 s…", attempt, exc
            )
            time.sleep(2)


def _wait_for_activation() -> tuple[bool, dict]:
    """
    Poll GET /config every 2 seconds until the simulation starts.

    Returns (True, config) if this miner is in the active list,
    or (False, {}) if the shutdown flag is set before start.
    """
    while not _shutdown.is_set():
        try:
            resp = httpx.get(f"{NODE_URL}/config", timeout=5.0)
            resp.raise_for_status()
            config = resp.json()
            if config.get("started"):
                is_active = MINER_ID in config.get("active", [])
                return is_active, config
        except Exception as exc:
            logger.warning("Config poll failed: %s", exc)
        time.sleep(2)
    return False, {}


def _get_chain_tip() -> dict:
    """
    Fetch the current chain tip from the node by reading /config.
    Called after activation to find out which block to build on.
    """
    while not _shutdown.is_set():
        try:
            resp = httpx.get(f"{NODE_URL}/config", timeout=5.0)
            resp.raise_for_status()
            tip = resp.json().get("tip")
            if tip:
                return tip
        except Exception as exc:
            logger.warning("Tip fetch failed: %s", exc)
        time.sleep(1)
    return {"index": 0, "hash": ""}


# ---------------------------------------------------------------------------
# Mining loop  (runs in background thread)
# ---------------------------------------------------------------------------


def _mining_loop(target: str) -> None:
    """
    Core proof-of-work loop.

    Algorithm:
      1. Snapshot the current chain tip (index + hash).
      2. Build a candidate block at height tip.index + 1.
      3. Increment nonce, compute SHA-256, check for the difficulty prefix.
      4. Every 1 000 iterations check _stop_event — if set, it means another
         miner won or the node broadcast a new block, so we restart the outer
         loop to pick up the new tip.
      5. If we find a valid hash first, submit the block to the node.
    """
    logger.info("PoW target: hash must start with '%s'", target)

    while not _shutdown.is_set():

        # --- Snapshot current tip atomically ---
        with _lock:
            tip_index = _current_tip["index"]
            tip_hash  = _current_tip["hash"]

        next_index = tip_index + 1

        # Build a fresh candidate block for this height
        candidate = Block(
            index=next_index,
            timestamp=time.time(),
            transactions=[
                f"tx::{MINER_ID}::{next_index}::1",
                f"tx::{MINER_ID}::{next_index}::2",
            ],
            previous_hash=tip_hash,
            nonce=0,
        )

        logger.info(
            "Starting PoW for block #%d  (prev_hash=...%s)",
            next_index, tip_hash[-8:] if tip_hash else "0" * 8,
        )

        start_time = time.time()
        nonce = 0
        _stop_event.clear()  # Reset interruption flag for this round

        # --- Inner nonce search ---
        while not _shutdown.is_set():
            candidate.nonce = nonce
            candidate.hash  = candidate.compute_hash()

            if candidate.hash.startswith(target):
                # ====== SOLVED ======
                solve_time = time.time() - start_time
                logger.info(
                    "SOLVED block #%d  nonce=%d  hash=%s...  (%.2f s)",
                    next_index, nonce, candidate.hash[:16], solve_time,
                )
                _submit_block(candidate, solve_time)
                break  # Outer loop will pick up the new tip

            nonce += 1

            # Check for interruption every 1 000 iterations to stay responsive
            # without the overhead of checking a threading.Event every loop
            if nonce % 1_000 == 0 and _stop_event.is_set():
                logger.info(
                    "Block #%d mining interrupted at nonce=%d "
                    "(node received a block from another miner)",
                    next_index, nonce,
                )
                break

        # Brief pause to let the /new_block handler update _current_tip
        # before the outer loop reads it again
        time.sleep(0.05)


def _submit_block(block: Block, solve_time: float) -> None:
    """
    POST the solved block to the node's /submit_block endpoint.

    We include miner_id and solve_time as extra fields so the node can
    log and display them on the dashboard.
    """
    payload = block.to_dict()
    payload["miner_id"]   = MINER_ID
    payload["solve_time"] = solve_time

    try:
        resp = httpx.post(
            f"{NODE_URL}/submit_block", json=payload, timeout=10.0
        )
        if resp.status_code == 200:
            logger.info("Block #%d accepted by node ✓", block.index)
        elif resp.status_code == 409:
            logger.info(
                "Block #%d rejected (409) — another miner was faster",
                block.index,
            )
        else:
            logger.warning(
                "Unexpected response %d submitting block #%d: %s",
                resp.status_code, block.index, resp.text,
            )
    except Exception as exc:
        logger.error("Failed to submit block #%d to node: %s", block.index, exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/new_block")
async def new_block(payload: dict) -> dict:
    """
    Called by the node when it accepts a block (from any miner).

    We update our local chain tip and set _stop_event to interrupt the
    mining thread's inner nonce loop so it restarts at the new height.
    This is the push-based block propagation that mirrors Bitcoin.
    """
    block = Block.from_dict(payload)

    with _lock:
        _current_tip["index"] = block.index
        _current_tip["hash"]  = block.hash

    _stop_event.set()
    logger.info(
        "New block tip received: #%d  hash=...%s",
        block.index, block.hash[-8:],
    )
    return {"status": "ok"}


@app.post("/shutdown")
async def shutdown() -> dict:
    """
    Called by the node when the simulation target block count is reached.
    Permanently stops the mining thread.
    """
    logger.info("Shutdown signal received from node — stopping mining loop")
    _shutdown.set()
    _stop_event.set()
    return {"status": "shutting_down"}


@app.get("/health")
async def health() -> dict:
    """Simple health check endpoint for Docker or debugging."""
    with _lock:
        tip = dict(_current_tip)
    return {
        "status":      "ok",
        "miner_id":    MINER_ID,
        "current_tip": tip,
        "shutdown":    _shutdown.is_set(),
    }
