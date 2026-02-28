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
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from block import Block
from blockchain import Blockchain

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DIFFICULTY = int(os.environ.get("DIFFICULTY", 4))   # overridden by /start
NUM_BLOCKS = int(os.environ.get("NUM_BLOCKS", 10))

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

# Each connected SSE browser client gets its own Queue
_sse_clients: list[asyncio.Queue] = []

# Ring-buffer of recent events so late-joining dashboards see history
_event_log: deque[str] = deque(maxlen=500)

# Protect chain mutations from concurrent miner submissions
_chain_lock = asyncio.Lock()

simulation_done = False

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Node started — difficulty=%d, target_blocks=%d", DIFFICULTY, NUM_BLOCKS
    )
    yield
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

    We use httpx.AsyncClient for non-blocking async HTTP calls so this
    function does not stall the FastAPI event loop while waiting for each
    miner to respond.
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
    miner_id = payload["miner_id"]
    callback_url = payload["callback_url"]

    miner_registry[miner_id] = callback_url
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
            await log_event(
                f"Stale block #{block.index} from {miner_id} — "
                f"chain already at #{blockchain.last_block.index}"
            )
            return JSONResponse(
                status_code=409,
                content={"status": "stale", "chain_tip": blockchain.last_block.index},
            )

        # Verification (hash integrity + PoW + chain linkage)
        valid, reason = blockchain.verify_block(block)
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

    # Check simulation target
    if blockchain.last_block.index >= NUM_BLOCKS:
        simulation_done = True
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

    start_data = {
        "difficulty": DIFFICULTY,
        "num_miners": num_miners,
        "active": active_miner_ids,
        "target": NUM_BLOCKS,
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
                    if event_type == "shutdown":
                        break
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
    <h1>&#x26CF; CryptoSim <span id="status-badge">MINING</span></h1>
    <p class="subtitle" id="dash-subtitle">Bitcoin-style proof-of-work simulation</p>

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
    </div>

    <div class="grid">
      <div class="panel">
        <h2>Event Log</h2>
        <div id="log"></div>
      </div>
      <div class="panel">
        <h2>Chain</h2>
        <div id="chain-display">
          <div class="block-card">
            <span class="block-index">#0</span>
            <span class="block-hash">Genesis Block</span>
            <span class="block-meta">NODE<br/>t=0.00s</span>
          </div>
        </div>
      </div>
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

    function appendLog(text) {{
      const div = document.createElement('div');
      div.className = 'log-line';
      div.innerHTML = text.replace(/(miner[_=\\w]+)/g, (match) => {{
        const id = match.replace('miner=', '');
        return `<span class="${{colorClass(id)}}">${{match}}</span>`;
      }});
      logEl.appendChild(div);
      logEl.scrollTop = logEl.scrollHeight;
    }}

    function prependBlock(data) {{
      minedCount++;
      document.getElementById('stat-length').textContent = data.index + 1;
      document.getElementById('stat-mined').textContent  = minedCount;
      const cc = colorClass(data.miner);
      const card = document.createElement('div');
      card.className = 'block-card';
      card.innerHTML = `
        <span class="block-index">#${{data.index}}</span>
        <span class="block-hash ${{cc}}">${{data.hash}}</span>
        <span class="block-meta">
          <span class="${{cc}}">${{data.miner}}</span><br/>
          nonce:${{data.nonce.toLocaleString()}} &nbsp; ${{data.time}}s
        </span>`;
      chainEl.insertBefore(card, chainEl.firstChild);
    }}

    /** Transition from setup screen to dashboard. */
    function showDashboard(cfg) {{
      document.getElementById('setup-screen').style.display   = 'none';
      document.getElementById('dashboard-screen').style.display = '';
      const target = '0'.repeat(cfg.difficulty);
      document.getElementById('dash-subtitle').textContent =
        `Bitcoin-style proof-of-work simulation — difficulty: ${{target}} — target: ${{cfg.target ?? cfg.num_blocks}} blocks`;
      document.getElementById('stat-diff').textContent   = cfg.difficulty;
      document.getElementById('stat-target').textContent = cfg.target ?? cfg.num_blocks;
      document.getElementById('stat-miners').textContent = cfg.num_miners ?? (cfg.active ? cfg.active.length : '-');
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
            card.innerHTML = `
              <span class="block-index">#${{b.index}}</span>
              <span class="block-hash">${{b.hash.slice(0,16)}}...</span>
              <span class="block-meta">reloaded</span>`;
            chainEl.appendChild(card);
          }});
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
      showDashboard(JSON.parse(e.data));
    }});

    es.addEventListener('log', (e) => {{
      appendLog(e.data);
    }});

    es.addEventListener('block', (e) => {{
      prependBlock(JSON.parse(e.data));
    }});

    es.addEventListener('shutdown', () => {{
      appendLog('[SYSTEM] Simulation complete — all target blocks mined.');
      const badge = document.getElementById('status-badge');
      badge.textContent = 'DONE';
      badge.className   = 'done';
      es.close();
    }});

    es.onerror = () => {{
      appendLog('[SYSTEM] SSE connection lost — simulation may have ended.');
      es.close();
    }};
  </script>
</body>
</html>"""
