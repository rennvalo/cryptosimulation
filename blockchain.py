"""
blockchain.py — The Blockchain class that maintains the canonical chain state.

The node (main container) holds exactly one Blockchain instance.  Miners
submit candidate blocks; the node calls verify_block() before appending.

Design notes:
  - The genesis block (index 0) is created automatically on init.
  - verify_block() is pure — it does not mutate state — so it is safe to
    call from inside an asyncio.Lock without blocking the event loop.
  - All mutation goes through append_block() so there is a single place
    to add audit logging if needed.
"""

import logging
import time

from block import Block
from wallet import verify_signature

logger = logging.getLogger(__name__)


# Finality depth: a block is considered confirmed once this many blocks
# have been built on top of it. 2 mirrors a common educational minimum.
CONFIRMATION_DEPTH = 2

# Difficulty Adjustment Algorithm (DAA) parameters.
# Every ADJUSTMENT_INTERVAL blocks the chain recalculates the PoW target
# to keep average solve time close to TARGET_BLOCK_TIME_SECS — matching
# the spirit of Bitcoin's 2-week / 2016-block retarget window.
ADJUSTMENT_INTERVAL  = 3      # blocks between each recalculation
TARGET_BLOCK_TIME_SECS = 10.0 # desired seconds per block
MAX_DIFFICULTY = 8            # hard ceiling (matches /start validation)


class Blockchain:
    """
    Maintains an ordered list of Blocks linked by previous_hash.

    Thread / coroutine safety: this class itself is NOT thread-safe.
    The caller (node.py) is responsible for wrapping mutations with
    an asyncio.Lock to prevent race conditions when multiple miners
    submit blocks concurrently.
    """

    def __init__(self, difficulty: int = 4) -> None:
        self.difficulty: int = difficulty   # Leading zeros required in hash
        self.chain: list[Block] = []
        # Fork event log: list of dicts recorded whenever a competing block
        # arrives at a height that was already filled on the canonical chain.
        self.fork_events: list[dict] = []
        self._create_genesis_block()

    # ------------------------------------------------------------------
    # Genesis
    # ------------------------------------------------------------------

    def _create_genesis_block(self) -> None:
        """
        Create block #0 — the hard-coded starting point of the chain.
        The genesis block is NOT mined (nonce = 0) and uses an all-zeros
        previous_hash because there is no block before it.
        """
        genesis = Block(
            index=0,
            timestamp=time.time(),
            transactions=["Genesis Block — CryptoSim"],
            previous_hash="0" * 64,  # Conventional all-zeros sentinel
            nonce=0,
        )
        genesis.hash = genesis.compute_hash()
        self.chain.append(genesis)
        logger.info("Genesis block created: %s", genesis)

    # ------------------------------------------------------------------
    # Properties / accessors
    # ------------------------------------------------------------------

    @property
    def last_block(self) -> Block:
        """Return the most recently appended block (the chain tip)."""
        return self.chain[-1]

    def get_tip(self) -> dict:
        """
        Return a minimal snapshot of the chain tip.
        Miners use this to know which block they must build on top of.
        """
        return {
            "index": self.last_block.index,
            "hash":  self.last_block.hash,
        }

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify_block(
        self,
        block: Block,
        addr_to_pubkey: dict[str, str] | None = None,
        pow_target: str | None = None,
    ) -> tuple[bool, str]:
        """
        Verify a candidate block submitted by a miner.

        Checks performed (in order):
          1. Index is exactly last_block.index + 1  — no gaps or duplicates.
          2. previous_hash matches last_block.hash   — correct chain linkage.
          3. Recomputed hash matches block.hash       — data integrity.
          4. Hash meets the difficulty target          — valid proof-of-work.
             Pass pow_target="" to skip PoW (used for instant portal transfers).
          5. Transfer signatures (if addr_to_pubkey supplied) — authenticity.

        Returns:
          (True,  "")       on success
          (False, reason)   on any failure, with a human-readable reason
        """
        expected_index = self.last_block.index + 1

        # 1. Index check
        if block.index != expected_index:
            return False, (
                f"Bad index: expected {expected_index}, got {block.index}"
            )

        # 2. Chain linkage check
        if block.previous_hash != self.last_block.hash:
            return False, (
                f"Chain break: previous_hash mismatch — "
                f"expected ...{self.last_block.hash[-12:]}, "
                f"got ...{block.previous_hash[-12:]}"
            )

        # 3. Hash integrity check — recompute and compare
        recomputed = block.compute_hash()
        if recomputed != block.hash:
            return False, (
                f"Hash mismatch: block claims {block.hash[:12]}..., "
                f"recomputed {recomputed[:12]}..."
            )

        # 4. Proof-of-work check
        # pow_target=None  → use the chain's current difficulty (normal miner blocks)
        # pow_target=""    → skip PoW entirely (instant portal transfer blocks)
        target = "0" * self.difficulty if pow_target is None else pow_target
        if target and not block.hash.startswith(target):
            return False, (
                f"Difficulty not met: hash {block.hash[:16]}... "
                f"does not start with '{target}'"
            )

        # 5. Transfer signature verification (optional — only when pubkey map provided)
        if addr_to_pubkey:
            for tx in block.transactions:
                if not isinstance(tx, dict):
                    continue
                if tx.get("from_addr") in ("COINBASE", "simulated"):
                    continue
                sig = tx.get("signature", "")
                if sig in ("", "simulated"):
                    # Legacy simulated txs are allowed through without verification
                    continue
                from_addr = tx["from_addr"]
                pubkey = addr_to_pubkey.get(from_addr)
                if not pubkey:
                    return False, (
                        f"Unknown sender address {from_addr[:12]}... — "
                        f"no public key registered"
                    )
                # Reconstruct the signing_data the sender would have signed
                import json as _json
                signing_payload = _json.dumps(
                    {
                        "from_addr": tx["from_addr"],
                        "to_addr":   tx["to_addr"],
                        "amount":    tx["amount"],
                        "timestamp": tx["timestamp"],
                    },
                    sort_keys=True,
                )
                if not verify_signature(pubkey, signing_payload, sig):
                    return False, (
                        f"Invalid signature on tx {tx.get('tx_id','?')[:12]}... "
                        f"from {from_addr[:12]}..."
                    )

        return True, ""

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def append_block(self, block: Block) -> None:
        """Append a verified block to the chain."""
        self.chain.append(block)
        logger.info(
            "Block appended: %s  (chain length now: %d)",
            block, len(self.chain)
        )

    def recalculate_difficulty(self) -> int | None:
        """
        Bitcoin-style Difficulty Adjustment Algorithm.

        Called after every append_block().  Returns the new difficulty value
        when an adjustment is made, or None when no adjustment occurs.

        Adjustment fires when the chain tip index is a positive multiple of
        ADJUSTMENT_INTERVAL (i.e. every 3rd mined block, skipping genesis).

        Formula:
          actual_time = elapsed seconds over the last ADJUSTMENT_INTERVAL blocks
          ratio       = actual_time / (ADJUSTMENT_INTERVAL * TARGET_BLOCK_TIME_SECS)

        ratio < 1 → blocks came in fast → raise difficulty
        ratio > 1 → blocks came in slow → lower difficulty

        Bitcoin caps the ratio at 4× per window to prevent runaway adjustments.
        We apply an additional ±1 per-window step cap because our difficulty
        range is only 1–8 (integers): without it, a single fast window can jump
        straight to MAX_DIFFICULTY and end the experiment immediately.  The step
        cap keeps the climb gradual and observable.
        Difficulty is always clamped to [1, MAX_DIFFICULTY].
        """
        tip_index = self.last_block.index
        # Only adjust at positive multiples of the interval (never at genesis)
        if tip_index == 0 or tip_index % ADJUSTMENT_INTERVAL != 0:
            return None
        if len(self.chain) <= ADJUSTMENT_INTERVAL:
            return None  # not enough history yet

        window_start = self.chain[tip_index - ADJUSTMENT_INTERVAL]
        window_end   = self.last_block
        actual_time  = window_end.timestamp - window_start.timestamp

        # Guard against zero / negative elapsed time (e.g. fast test machines)
        if actual_time <= 0:
            actual_time = 0.001

        expected_time = ADJUSTMENT_INTERVAL * TARGET_BLOCK_TIME_SECS
        ratio = actual_time / expected_time

        # Cap ratio at 4× per window (Bitcoin's rule)
        ratio = max(0.25, min(4.0, ratio))

        uncapped = max(1, min(MAX_DIFFICULTY, round(self.difficulty / ratio)))

        # ±1 step cap: difficulty moves at most one level per window so the
        # experiment progresses visibly rather than jumping to the ceiling.
        if uncapped > self.difficulty:
            new_difficulty = self.difficulty + 1
        elif uncapped < self.difficulty:
            new_difficulty = self.difficulty - 1
        else:
            new_difficulty = self.difficulty

        if new_difficulty != self.difficulty:
            logger.info(
                "DAA: difficulty %d → %d  (window=%d blocks, actual=%.1fs, expected=%.1fs, ratio=%.2f)",
                self.difficulty, new_difficulty,
                ADJUSTMENT_INTERVAL, actual_time, expected_time, ratio,
            )
            self.difficulty = new_difficulty

        return new_difficulty

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Finality
    # ------------------------------------------------------------------

    @property
    def confirmed_height(self) -> int:
        """
        The index of the highest block that has reached finality.

        A block is considered final once CONFIRMATION_DEPTH additional blocks
        have been appended on top of it.  Blocks at or below confirmed_height
        will never be reorganised away in this simulation.

        Returns -1 when no non-genesis blocks have been confirmed yet.
        """
        # chain length includes genesis (index 0), tip index = len-1
        tip_index = len(self.chain) - 1
        confirmed = tip_index - CONFIRMATION_DEPTH
        return confirmed  # may be negative; callers should clamp at 0

    # ------------------------------------------------------------------
    # Balances
    # ------------------------------------------------------------------

    def compute_balances(self, confirmed_only: bool = True) -> dict[str, float]:
        """
        Walk blocks and compute RennCoin balances for every address.

        confirmed_only=True  (default) — only include blocks that have reached
          2-block finality.  Used by the leaderboard and /balances endpoint.
        confirmed_only=False — include every block in the chain (including the
          latest unconfirmed tip).  Used by the portal wallet display so a
          user sees their balance update immediately after buying/receiving RNC
          rather than waiting for 2 more miner blocks.

        Simulated/legacy transfers (signature == 'simulated') are always skipped.
        """
        if confirmed_only:
            max_index = max(0, self.confirmed_height)
        else:
            max_index = len(self.chain) - 1
        balances: dict[str, float] = {}

        # Pass 1: coinbase rewards
        for block in self.chain:
            if block.index > max_index:
                break
            for tx in block.transactions:
                if isinstance(tx, dict) and tx.get("from_addr") == "COINBASE":
                    addr   = tx["to_addr"]
                    amount = float(tx.get("amount", 0))
                    balances[addr] = round(balances.get(addr, 0.0) + amount, 4)

        # Pass 2: signed peer transfers (real signatures only)
        for block in self.chain:
            if block.index > max_index:
                break
            for tx in block.transactions:
                if not isinstance(tx, dict):
                    continue
                from_addr = tx.get("from_addr", "")
                sig       = tx.get("signature", "")
                if from_addr in ("", "COINBASE") or sig in ("", "simulated"):
                    continue
                to_addr = tx["to_addr"]
                amount  = float(tx.get("amount", 0))
                balances[from_addr] = round(balances.get(from_addr, 0.0) - amount, 4)
                balances[to_addr]   = round(balances.get(to_addr, 0.0)   + amount, 4)

        return balances

    # ------------------------------------------------------------------
    # Fork recording
    # ------------------------------------------------------------------

    def record_fork(
        self, height: int, canonical_miner: str, fork_miner: str
    ) -> dict:
        """
        Log a fork event — two miners submitted valid blocks at the same height.
        The canonical chain keeps the first-accepted block; the competing block
        becomes an orphan.  This mirrors Bitcoin's natural fork resolution where
        the longest chain always wins.
        """
        event = {
            "height":           height,
            "canonical_miner":  canonical_miner,
            "fork_miner":       fork_miner,
        }
        self.fork_events.append(event)
        logger.info(
            "Fork recorded at height %d: canonical=%s orphan=%s",
            height, canonical_miner, fork_miner,
        )
        return event

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize the full chain to a JSON-compatible dict."""
        return {
            "difficulty":    self.difficulty,
            "length":        len(self.chain),
            "chain":         [b.to_dict() for b in self.chain],
            "confirmed_height": max(0, self.confirmed_height),
            "fork_events":   self.fork_events,
        }
