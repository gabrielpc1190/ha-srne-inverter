"""Per-unit capability probe.

Not every SRNE firmware exposes every block. Instead of assuming, each block is
read exactly once at setup and classified:

  SUPPORTED   -- the read returned data; its fields become entities.
  UNSUPPORTED -- the device answered IllegalDataAddress (UnsupportedRegisterError),
                 its own unambiguous "this address does not exist" signal. This
                 is a PERMANENT verdict: the block is removed from polling and
                 never re-tried until the next setup. Nothing else may produce
                 it -- see Fix round 1, Finding 2 below.
  UNKNOWN     -- the read failed for any other reason (timeout, socket, logger
                 busy, a malformed/empty frame) and stayed failed after
                 `retries` extra attempts. We do NOT create entities from a
                 guess; the coordinator re-probes later, which is cheap and
                 self-correcting -- unlike UNSUPPORTED, this is never
                 permanent.

The result of a probe is never persisted to disk -- it is recomputed on every
setup so a firmware change (or a swapped unit) is picked up by a restart
instead of being shadowed by a stale cache.

Fix round 1 (2026-09-13, Opus review): two corrections from the original
draft, both about what counts as a *permanent* verdict removing a block from
polling forever (until a restart):
  1. (Finding 1, CRITICAL) `probe()` used to raise ProbeFailedError only when
     EVERY block ended UNKNOWN. A wrong slave id (this site's own confusable
     pair: Casa Justice inverter 1 is 192.168.188.240 slave 1, inverter 2 is
     .242 slave 2) makes every block answer Empty/AcknowledgeError, which
     used to end in every block UNSUPPORTED -- returning a "successful"
     ProbeResult with zero supported blocks: a Home Assistant device with no
     entities that polls nothing, no ConfigEntryNotReady, recoverable only by
     a restart (UNSUPPORTED blocks are never re-probed). Fixed: raise
     whenever NO block is SUPPORTED, which subsumes the former all-UNKNOWN
     check.
  2. (Finding 2) A `TransportProtocolError` (the transport's catch-all for a
     malformed/empty/short frame, `AcknowledgeError`, `ServerDeviceBusyError`,
     `GatewayTargetDeviceFailedToRespond`, and any other Modbus exception
     code that isn't IllegalDataAddress/IllegalDataValue) used to become a
     PERMANENT UNSUPPORTED verdict if it survived one retry. A stolen-then-
     returned TCP session (this hardware's single most common real disruption)
     can easily deliver two bad frames in a 0.2 s window on a block that is
     genuinely present -- that used to permanently strip its entities until a
     restart. Only UnsupportedRegisterError (the device's own IllegalDataAddress
     answer) may now produce UNSUPPORTED; every other failure that survives
     its retries -- protocol or connection -- becomes UNKNOWN and is retried
     at the next setup.

THIS MODULE MUST NOT IMPORT homeassistant.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field as dc_field
from enum import StrEnum

from .registers import BLOCKS, Block
from .transport.base import (
    Transport,
    TransportConnectionError,
    TransportError,
    TransportProtocolError,
    UnsupportedRegisterError,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PAUSE = 0.25

# Fixed backoff between retry attempts on the SAME block -- independent of
# `pause` (the gap BETWEEN different blocks), which callers legitimately want
# to zero out in tests without also removing the retry's own breathing room.
RETRY_BACKOFF = 0.2

# Fix round 1, Finding 3: probe() used to have no wall-clock budget of its own
# at all -- worst case (10 blocks x (retries+1)=2 attempts x the transport's
# 10 s DEFAULT_TIMEOUT, plus pauses/backoffs) measured at 204.25 s for one
# unreachable unit, against a shared Home Assistant bootstrap budget
# (SLOW_SETUP_MAX_WAIT) of 300 s for every integration combined. 60 s is
# roughly the time for 3 blocks to each exhaust a full retry cycle against an
# unresponsive logger (3 x 2 x 10 s = 60 s) -- enough to be confident the unit
# is actually unreachable rather than just slow, while capping how much of
# the shared bootstrap budget one bad unit can consume. Pass `deadline=None`
# to disable (e.g. a script with no bootstrap budget to protect).
DEFAULT_DEADLINE = 60.0


class BlockSupport(StrEnum):
    """Whether a block exists on this particular unit."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class ProbeFailedError(TransportError):
    """Not a single block answered -- the unit or the link is down."""


@dataclass(slots=True)
class ProbeResult:
    """Outcome of one probe pass."""

    support: dict[int, BlockSupport] = dc_field(default_factory=dict)
    registers: dict[int, int] = dc_field(default_factory=dict)
    errors: dict[int, str] = dc_field(default_factory=dict)

    def supported_blocks(self, blocks: tuple[Block, ...] = BLOCKS) -> tuple[Block, ...]:
        """Blocks whose fields may become entities."""
        return tuple(
            block
            for block in blocks
            if self.support.get(block.addr) is BlockSupport.SUPPORTED
        )

    def as_diagnostics(self) -> dict[str, str]:
        """Support map keyed by hex address, for diagnostics output."""
        return {f"0x{addr:04X}": state.value for addr, state in sorted(self.support.items())}


async def probe(
    transport: Transport,
    blocks: tuple[Block, ...] = BLOCKS,
    *,
    pause: float = DEFAULT_PAUSE,
    retries: int = 1,
    deadline: float | None = DEFAULT_DEADLINE,
) -> ProbeResult:
    """Read every block once and classify it.

    Raises ProbeFailedError if:
      - the whole pass exceeds `deadline` seconds (None disables the budget), or
      - no block ended SUPPORTED (every block is UNSUPPORTED, UNKNOWN, or a
        mix of the two) --
    so the caller can surface ConfigEntryNotReady instead of setting up a
    device with zero entities that would otherwise poll nothing forever.
    """
    result = ProbeResult()

    try:
        async with asyncio.timeout(deadline):
            for index, block in enumerate(blocks):
                if index and pause:
                    await asyncio.sleep(pause)
                state, error = await _probe_one(transport, block, retries, result)
                result.support[block.addr] = state
                if error is not None:
                    result.errors[block.addr] = error
                _LOGGER.debug(
                    "Probe 0x%04X (%s) -> %s%s",
                    block.addr, block.name, state.value,
                    f" [{error}]" if error else "",
                )
    except TimeoutError as err:
        raise ProbeFailedError(
            f"probe exceeded its {deadline} s deadline; the unit or the "
            "link is unresponsive"
        ) from err

    if not any(state is BlockSupport.SUPPORTED for state in result.support.values()):
        raise ProbeFailedError("no block answered; the unit or the link is down")

    return result


async def _probe_one(
    transport: Transport,
    block: Block,
    retries: int,
    result: ProbeResult,
) -> tuple[BlockSupport, str | None]:
    """Probe one block, retrying protocol/connection errors `retries` times.

    Only UnsupportedRegisterError (the device's own IllegalDataAddress
    answer) short-circuits immediately with no retry -- it is a definitive,
    permanent fact about this unit, and spending a retry on it would not
    change the answer. Every other failure (TransportProtocolError,
    TransportConnectionError, and subclasses of either) is retried and, if it
    survives `retries` extra attempts, becomes UNKNOWN -- never UNSUPPORTED --
    so the coordinator's next re-probe gets a fair chance to recover it.
    """
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            values = await transport.read_holding(block.addr, block.count)
        except UnsupportedRegisterError as err:
            return BlockSupport.UNSUPPORTED, str(err)
        except (TransportProtocolError, TransportConnectionError) as err:
            last_error = err
            if attempt < retries:
                await asyncio.sleep(RETRY_BACKOFF)
            continue
        for offset, value in enumerate(values):
            result.registers[block.addr + offset] = value
        return BlockSupport.SUPPORTED, None

    return BlockSupport.UNKNOWN, str(last_error)
