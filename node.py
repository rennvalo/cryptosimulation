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

DIFFICULTY = int(os.environ.get("DIFFICULTY", 4))
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

blockchain = Blockchain(difficulty=DIFFICULTY)

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
    Miners call this on startup to announce themselves and receive the
    current chain tip so they know which block to build on top of.

    Body: { "miner_id": "miner_1", "callback_url": "http://miner_1:8001" }
    """
    miner_id = payload["miner_id"]
    callback_url = payload["callback_url"]

    miner_registry[miner_id] = callback_url
    tip = blockchain.get_tip()

    await log_event(
        f"Miner registered: {miner_id} @ {callback_url}  "
        f"(chain tip: block #{tip['index']})"
    )

    return {
        "status": "registered",
        "tip": tip,
        "difficulty": DIFFICULTY,
    }


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
    return blockchain.to_dict()


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
    /* Stats bar */
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
    /* Status badge */
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
    /* Event log */
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
    /* Chain display */
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
    /* Per-miner colour coding (up to 5 miners) */
    .mc0 {{ color: #58a6ff; }}
    .mc1 {{ color: #f0883e; }}
    .mc2 {{ color: #bc8cff; }}
    .mc3 {{ color: #3fb950; }}
    .mc4 {{ color: #ff7b72; }}
  </style>
</head>
<body>
  <h1>&#x26CF; CryptoSim <span id="status-badge">MINING</span></h1>
  <p class="subtitle">
    Bitcoin-style proof-of-work simulation &mdash; difficulty: {"0" * DIFFICULTY}
    &mdash; target: {NUM_BLOCKS} blocks
  </p>

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
      <div class="stat-value" id="stat-miners">0</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Difficulty</div>
      <div class="stat-value">{DIFFICULTY}</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Target Blocks</div>
      <div class="stat-value">{NUM_BLOCKS}</div>
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

  <script>
    // Assign a stable colour class to each miner ID we see
    const MINER_COLORS = ['mc0','mc1','mc2','mc3','mc4'];
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

    /** Append a line to the event log, colorising miner IDs. */
    function appendLog(text) {{
      const div = document.createElement('div');
      div.className = 'log-line';
      // Replace "miner=miner_X" or bare "miner_X" patterns with coloured spans
      div.innerHTML = text.replace(/(miner[_=\\w]+)/g, (match) => {{
        const id = match.replace('miner=', '');
        return `<span class="${{colorClass(id)}}">${{match}}</span>`;
      }});
      logEl.appendChild(div);
      logEl.scrollTop = logEl.scrollHeight;
    }}

    /** Prepend a block card to the chain display (newest at top). */
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
        </span>
      `;
      chainEl.insertBefore(card, chainEl.firstChild);
    }}

    // Connect to the SSE event stream
    const es = new EventSource('/events');

    es.addEventListener('log', (e) => {{
      const text = e.data;
      appendLog(text);
      // Increment miner counter whenever a new miner registers
      if (text.includes('Miner registered')) {{
        const el = document.getElementById('stat-miners');
        el.textContent = parseInt(el.textContent || '0') + 1;
      }}
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
