"""ALTCHA proof-of-work solver for ``GET /captcha``.

BRAIN gates sign-in behind ALTCHA: the server sends a target digest and a salt, and the
client hashes ``salt + n`` for increasing integers ``n`` until the digest matches. Solving
is pure CPU, so sign-in runs it in a worker thread and a ~1e6-iteration search cannot stall
the event loop.
"""

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

# ALTCHA permits SHA-1 and SHA-512 too; BRAIN sends SHA-256.
ALGORITHM = "SHA-256"

DEFAULT_MAX_NUMBER = 1_000_000


class AltchaError(RuntimeError):
    """Raised when a challenge is malformed or cannot be solved."""


@dataclass(frozen=True, slots=True)
class Challenge:
    """A challenge as issued by ``GET /captcha``."""

    algorithm: str
    challenge: str
    salt: str
    signature: str = ""
    maxnumber: int = DEFAULT_MAX_NUMBER

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Challenge:
        try:
            algorithm = str(payload["algorithm"]).upper()
            challenge = str(payload["challenge"])
            salt = str(payload["salt"])
        except KeyError as exc:
            raise AltchaError(f"Challenge is missing required field {exc.args[0]!r}") from exc

        if algorithm != ALGORITHM:
            raise AltchaError(f"Unsupported ALTCHA algorithm {algorithm!r}; expected {ALGORITHM}")

        # `maxnumber` is optional in the ALTCHA challenge format.
        raw_max = payload.get("maxnumber", DEFAULT_MAX_NUMBER)
        try:
            maxnumber = int(raw_max)
        except (TypeError, ValueError) as exc:
            raise AltchaError(f"Challenge has non-integer maxnumber {raw_max!r}") from exc

        return cls(
            algorithm=algorithm,
            challenge=challenge,
            salt=salt,
            signature=str(payload.get("signature", "")),
            maxnumber=maxnumber,
        )


@dataclass(frozen=True, slots=True)
class Solution:
    """A solved challenge, ready to send to ``POST /authentication``."""

    challenge: Challenge
    number: int
    took_ms: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "algorithm": self.challenge.algorithm,
            "challenge": self.challenge.challenge,
            "number": self.number,
            "salt": self.challenge.salt,
            "signature": self.challenge.signature,
            "took": self.took_ms,
        }

    def encode(self) -> str:
        """Base64 of the compact JSON payload — the value of the ``captcha`` field."""
        compact = json.dumps(self.to_payload(), separators=(",", ":"), sort_keys=True)
        return base64.b64encode(compact.encode("utf-8")).decode("ascii")


_DIGITS = tuple(str(n).encode("ascii") for n in range(100))
_TWO_DIGITS = tuple(f"{n:02d}".encode("ascii") for n in range(100))


def solve(challenge: Challenge) -> Solution:
    """Search for the integer whose digest matches the challenge.

    Raises :class:`AltchaError` if no solution exists at or below ``maxnumber``, which
    means the challenge was malformed or the algorithm changed.
    """
    # The salt hashed once and copied, and raw digests compared: measured, faster.
    salted = hashlib.sha256(challenge.salt.encode("utf-8"))
    try:
        target = bytes.fromhex(challenge.challenge)
    except ValueError:
        target = b""  # Matches no digest, so it ends as any unsolvable challenge does.
    started = time.monotonic()

    def found(number: int) -> Solution:
        took_ms = int((time.monotonic() - started) * 1000)
        return Solution(challenge=challenge, number=number, took_ms=took_ms)

    limit = challenge.maxnumber
    for number in range(min(limit + 1, 100)):
        attempt = salted.copy()
        attempt.update(_DIGITS[number])
        if attempt.digest() == target:
            return found(number)
    # Past 99 the leading digits are hashed once per hundred numbers, each then finished with
    # its last two: the same digest as the whole number's, since SHA-256 reads a stream.
    for hundreds in range(1, limit // 100 + 1):
        prefix = salted.copy()
        prefix.update(str(hundreds).encode("ascii"))
        base = hundreds * 100
        for last in range(min(100, limit - base + 1)):
            attempt = prefix.copy()
            attempt.update(_TWO_DIGITS[last])
            if attempt.digest() == target:
                return found(base + last)

    raise AltchaError(
        f"No solution found for {challenge.algorithm} challenge within "
        f"maxnumber={challenge.maxnumber}. The captcha scheme may have changed."
    )
