"""
transaction.py — RennCoin transaction types.

A Transaction represents a transfer of RennCoin between two wallet addresses.
Coinbase transactions are special: they have no sender (from_addr = "COINBASE")
and represent the block reward paid to the miner who solved that block.

Transaction structure mirrors Bitcoin's UTXO model conceptually, but is
simplified to a flat signed message for educational purposes.
"""

import hashlib
import json
import time
from dataclasses import dataclass, asdict


# Block reward paid to the winning miner per block (mimics Bitcoin halving concept)
BLOCK_REWARD = 50.0  # RennCoin per block


@dataclass
class Transaction:
    """A single RennCoin transaction."""

    tx_id:     str    # SHA-256 fingerprint of this transaction's content
    from_addr: str    # Sender's wallet address ("COINBASE" for block rewards)
    to_addr:   str    # Recipient's wallet address
    amount:    float  # Amount of RennCoin transferred
    timestamp: float  # Unix epoch when the transaction was created
    signature: str    # Hex ECDSA signature from sender (empty for coinbase)

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def coinbase(cls, to_addr: str, block_index: int) -> "Transaction":
        """
        Create a coinbase reward transaction — no sender, no signature.
        This is the first transaction in every mined block and represents
        the 50 RennCoin reward paid to the winning miner.
        """
        ts = time.time()
        tx_id = hashlib.sha256(
            f"coinbase:{to_addr}:{block_index}:{ts}".encode()
        ).hexdigest()
        return cls(
            tx_id=tx_id,
            from_addr="COINBASE",
            to_addr=to_addr,
            amount=BLOCK_REWARD,
            timestamp=ts,
            signature="",
        )

    @classmethod
    def transfer(
        cls,
        from_addr: str,
        to_addr: str,
        amount: float,
        signature: str,
    ) -> "Transaction":
        """
        Create a signed transfer transaction between two wallets.
        The signature must be produced by the wallet corresponding to from_addr.
        """
        ts = time.time()
        tx_id = hashlib.sha256(
            f"{from_addr}:{to_addr}:{amount}:{ts}".encode()
        ).hexdigest()
        return cls(
            tx_id=tx_id,
            from_addr=from_addr,
            to_addr=to_addr,
            amount=amount,
            timestamp=ts,
            signature=signature,
        )

    # ------------------------------------------------------------------
    # Signing helpers
    # ------------------------------------------------------------------

    def signing_data(self) -> str:
        """
        Return the canonical string that a wallet signs (for transfers) or
        that can be used to verify this transaction's integrity.
        Excludes tx_id and signature so they don't create circular dependency.
        """
        return json.dumps(
            {
                "from_addr": self.from_addr,
                "to_addr":   self.to_addr,
                "amount":    self.amount,
                "timestamp": self.timestamp,
            },
            sort_keys=True,
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Transaction":
        return cls(**data)

    def __repr__(self) -> str:
        frm = "COINBASE" if self.from_addr == "COINBASE" else self.from_addr[:8] + "…"
        to  = self.to_addr[:8] + "…"
        return f"Transaction({frm} → {to}  {self.amount} RNC)"
