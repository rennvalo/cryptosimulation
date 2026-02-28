"""
wallet.py — ECDSA wallet for the RennCoin simulation.

Uses the secp256k1 elliptic curve — the same curve Bitcoin uses.
Each wallet has a private/public keypair.  The wallet address is a
shortened SHA-256 of the public key (40 hex chars = 20 bytes), analogous
to a Bitcoin address derived from a public key hash.

Signing:
  wallet.sign(data)  →  hex-encoded 64-byte signature
Verification:
  verify_signature(pubkey_hex, data, sig_hex)  →  bool

Usage in the simulation:
  - Each miner generates (or loads from WALLET_KEY env var) a Wallet at startup.
  - Coinbase transactions are addressed to wallet.address.
  - Peer-to-peer transfers are signed with wallet.sign() and verified on the node.
"""

import hashlib

from ecdsa import SECP256k1, BadSignatureError, SigningKey, VerifyingKey


class Wallet:
    """
    An ECDSA keypair representing a RennCoin wallet.

    If private_key_hex is supplied the wallet is restored from that key;
    otherwise a fresh random keypair is generated.
    """

    def __init__(self, private_key_hex: str | None = None) -> None:
        if private_key_hex:
            self._sk = SigningKey.from_string(
                bytes.fromhex(private_key_hex), curve=SECP256k1
            )
        else:
            self._sk = SigningKey.generate(curve=SECP256k1)
        self._vk: VerifyingKey = self._sk.get_verifying_key()

    # ------------------------------------------------------------------
    # Key accessors
    # ------------------------------------------------------------------

    @property
    def private_key_hex(self) -> str:
        """32-byte private key as a hex string. Keep this secret."""
        return self._sk.to_string().hex()

    @property
    def public_key_hex(self) -> str:
        """64-byte uncompressed public key as a hex string."""
        return self._vk.to_string().hex()

    @property
    def address(self) -> str:
        """
        40-char wallet address = first 40 hex characters of SHA-256(pubkey).
        Analogous to a Bitcoin P2PKH address (simplified — no Base58Check).
        """
        return hashlib.sha256(self._vk.to_string()).hexdigest()[:40]

    # ------------------------------------------------------------------
    # Signing
    # ------------------------------------------------------------------

    def sign(self, data: str) -> str:
        """
        Sign an arbitrary string with this wallet's private key.
        Uses deterministic signing (RFC 6979) so the same input always
        produces the same signature — useful for testing/debugging.

        Returns a 64-byte (128 hex char) signature.
        """
        return self._sk.sign_deterministic(data.encode()).hex()

    # ------------------------------------------------------------------
    # Representation
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"Wallet(address={self.address})"


# ------------------------------------------------------------------
# Module-level verification helper (no wallet instance needed)
# ------------------------------------------------------------------

def verify_signature(public_key_hex: str, data: str, signature_hex: str) -> bool:
    """
    Verify that signature_hex was produced by the private key corresponding
    to public_key_hex over the string data.

    Returns True if valid, False for any failure (bad sig, malformed hex, etc).
    """
    try:
        vk = VerifyingKey.from_string(
            bytes.fromhex(public_key_hex), curve=SECP256k1
        )
        return vk.verify(bytes.fromhex(signature_hex), data.encode())
    except (BadSignatureError, Exception):
        return False
