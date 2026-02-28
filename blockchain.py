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

logger = logging.getLogger(__name__)


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

    def verify_block(self, block: Block) -> tuple[bool, str]:
        """
        Verify a candidate block submitted by a miner.

        Checks performed (in order):
          1. Index is exactly last_block.index + 1  — no gaps or duplicates.
          2. previous_hash matches last_block.hash   — correct chain linkage.
          3. Recomputed hash matches block.hash       — data integrity.
          4. Hash meets the difficulty target          — valid proof-of-work.

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
        target = "0" * self.difficulty
        if not block.hash.startswith(target):
            return False, (
                f"Difficulty not met: hash {block.hash[:16]}... "
                f"does not start with '{target}'"
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

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize the full chain to a JSON-compatible dict."""
        return {
            "difficulty": self.difficulty,
            "length":     len(self.chain),
            "chain":      [b.to_dict() for b in self.chain],
        }
