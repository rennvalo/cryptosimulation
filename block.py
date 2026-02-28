"""
block.py — Defines the Block structure for the blockchain simulation.

Each block contains all the fields that make up a Bitcoin-style block.
SHA-256 hashing ties the chain together via previous_hash — changing any
field in a block changes its hash, which breaks the link to every block that
follows it (tamper-evidence).
"""

import hashlib
import json
import time
from dataclasses import dataclass, asdict
from typing import List


@dataclass
class Block:
    """A single block in the blockchain."""

    index: int              # Position in the chain (0 = genesis block)
    timestamp: float        # Unix epoch time when the block was created
    transactions: List[dict] # List of serialised Transaction dicts (coinbase first)
    previous_hash: str      # Hash of the preceding block (links the chain)
    nonce: int = 0          # Proof-of-work value that miners iterate over
    hash: str = ""          # SHA-256 hash of this block's full contents

    # ------------------------------------------------------------------
    # Hashing
    # ------------------------------------------------------------------

    def compute_hash(self) -> str:
        """
        Serialize all block fields (except 'hash' itself) to a canonical
        JSON string, then return its SHA-256 hex digest.

        sort_keys=True guarantees deterministic output regardless of
        Python dict insertion order — critical for consensus because every
        miner and the node must compute the exact same hash for the same
        block data.
        """
        block_dict = {
            "index":         self.index,
            "timestamp":     self.timestamp,
            "transactions":  self.transactions,
            "previous_hash": self.previous_hash,
            "nonce":         self.nonce,
        }
        raw = json.dumps(block_dict, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    # ------------------------------------------------------------------
    # Serialization helpers
    # Used when sending blocks over HTTP between containers.
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Return a plain-dict representation of this block."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Block":
        """Reconstruct a Block from a plain dict (e.g. received over HTTP)."""
        return cls(
            index=data["index"],
            timestamp=data["timestamp"],
            transactions=data["transactions"],
            previous_hash=data["previous_hash"],
            nonce=data["nonce"],
            hash=data["hash"],
        )

    def __repr__(self) -> str:
        return (
            f"Block(index={self.index}, "
            f"hash={self.hash[:12]}..., "
            f"prev={self.previous_hash[:12]}..., "
            f"nonce={self.nonce})"
        )
