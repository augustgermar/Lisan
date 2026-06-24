from __future__ import annotations

"""Deterministic agent identity generation.

The generated identity has two layers:
- ``seed``: the reproducible random seed we keep on disk
- ``sha256``: the cryptographic identity digest derived from the seed

The pronounceable name is only a human-facing projection of the digest.
"""

import hashlib
import secrets
from dataclasses import dataclass


_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CONSONANTS = "bcdfghjklmnpqrstvwxyz"
_VOWELS = "aeiouy"


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    seed: str
    sha256: str
    konstel_hash: str
    name: str


def generate_agent_identity(random_bytes: int = 16, name_length: int = 6) -> AgentIdentity:
    """Generate a fresh unique agent identity."""
    seed = secrets.token_hex(random_bytes)
    return generate_agent_identity_from_seed(seed, name_length=name_length)


def generate_agent_identity_from_seed(seed: str, name_length: int = 6) -> AgentIdentity:
    """Recreate the agent identity from a stored seed."""
    seed = str(seed or "").strip()
    sha256 = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return AgentIdentity(
        seed=seed,
        sha256=sha256,
        konstel_hash=crockford_base32(sha256),
        name=capitalize_first(phonemes_16_4(sha256, name_length=name_length)),
    )


def crockford_base32(hex_digest: str) -> str:
    """Encode a hex digest in Crockford base32."""
    value = int(hex_digest, 16)
    if value == 0:
        return "0"
    chars: list[str] = []
    while value:
        value, remainder = divmod(value, 32)
        chars.append(_CROCKFORD_ALPHABET[remainder])
    return "".join(reversed(chars))


def phonemes_16_4(hex_digest: str, name_length: int = 6) -> str:
    """Build a pronounceable lower-case name from a digest.

    The output is a simple consonant-vowel alternation, seeded by the digest
    bytes so the same seed always produces the same surface name.
    """
    name_length = max(2, int(name_length or 0))
    data = bytes.fromhex(hex_digest)
    if not data:
        data = b"\x00"
    letters: list[str] = []
    for index in range(name_length):
        byte = data[index % len(data)]
        if index % 2 == 0:
            letters.append(_CONSONANTS[byte % len(_CONSONANTS)])
        else:
            letters.append(_VOWELS[byte % len(_VOWELS)])
    return "".join(letters)


def capitalize_first(value: str) -> str:
    value = str(value or "")
    return value[:1].upper() + value[1:]
