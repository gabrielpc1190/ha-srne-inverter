"""Per-unit capability probe.

Not every SRNE firmware exposes every block. Instead of assuming, each block is
read exactly once at setup and classified:

  SUPPORTED   -- the read returned data; its fields become entities.
  UNSUPPORTED -- the device answered IllegalDataAddress, or a protocol error
                 that survived a retry (empty/acknowledge replies mean the
                 block is not there for this slave). No entities are created.
  UNKNOWN     -- the link failed (timeout / socket / logger busy). We do NOT
                 create entities from a guess; the coordinator re-probes
                 later.

The result of a probe is never persisted to disk -- it is recomputed on every
setup so a firmware change (or a swapped unit) is picked up by a restart
instead of being shadowed by a stale cache.

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
) -> ProbeResult:
    """Read every block once and classify it.

    Raises ProbeFailedError if every block ended UNKNOWN (dead link), so the
    caller can surface ConfigEntryNotReady instead of an empty device.
    """
    result = ProbeResult()

    for index, block in enumerate(blocks):
        if index and pause:
            await asyncio.sleep(pause)
        state, error = await _probe_one(transport, block, retries, result)
        result.support[block.addr] = state
        if error is not None:
            result.errors[block.addr] = error
        _LOGGER.debug(
            "Probe 0x%04X (%s) -> %s%s",
            block.addr, block.name, state.value, f" [{error}]" if error else "",
        )

    if all(state is BlockSupport.UNKNOWN for state in result.support.values()):
        raise ProbeFailedError("no block answered; the unit or the link is down")

    return result


async def _probe_one(
    transport: Transport,
    block: Block,
    retries: int,
    result: ProbeResult,
) -> tuple[BlockSupport, str | None]:
    """Probe one block, retrying protocol/connection errors `retries` times."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            values = await transport.read_holding(block.addr, block.count)
        except UnsupportedRegisterError as err:
            return BlockSupport.UNSUPPORTED, str(err)
        except (TransportProtocolError, TransportConnectionError) as err:
            last_error = err
            if attempt < retries:
                await asyncio.sleep(0.2)
            continue
        for offset, value in enumerate(values):
            result.registers[block.addr + offset] = value
        return BlockSupport.SUPPORTED, None

    if isinstance(last_error, TransportProtocolError):
        # Empty / AcknowledgeError that survived a retry: the block is not
        # there for this slave id.
        return BlockSupport.UNSUPPORTED, str(last_error)
    return BlockSupport.UNKNOWN, str(last_error)
