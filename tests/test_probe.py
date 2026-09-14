"""Capability probe: one read per block, classify, never guess.

Fixture choice matters here and is deliberate, not interchangeable with the
brief's original draft (see task-5-report.md for the full account):

- `justice_registers` is the REAL recorded capture of Casa Justice inverter 1.
  It has real holes inside 5 of the 10 declared-present BLOCKS (faults,
  inverter_b, control_high, device_info, meter) -- gaps in what we happened
  to capture, not evidence the device lacks those registers. Probing ALL of
  BLOCKS against it raises bare LookupError (a fixture-gap signal, see
  tests/fake_transport.py's module docstring) for any of those five, which
  is not caught anywhere in probe.py and would abort the whole probe() call
  -- so it is only safe to use here when every read in the test is either
  intercepted by an injected error before the fixture is even consulted, or
  targets exactly one block that IS fully captured.
- `justice_registers_synthetic_complete` pads every address inside every
  declared block with synthetic filler so all 10 blocks read cleanly. Used
  wherever a test lets probe() walk the full default `BLOCKS` tuple and
  expects every block to resolve (to SUPPORTED, or to a specific classification
  driven by an injected error/unsupported range, never by a fixture hole).

Device absence (UNSUPPORTED) is expressed ONLY via FakeTransport's
`unsupported=` construction argument -- never by deleting keys from the
registers dict, which produces LookupError (fixture-gap), not
UnsupportedRegisterError (device fact). Getting this backwards would make
the "missing block" test silently exercise the wrong code path.
"""

import pytest

from custom_components.srne_inverter.probe import (
    BlockSupport,
    ProbeFailedError,
    probe,
)
from custom_components.srne_inverter.registers import BLOCKS
from custom_components.srne_inverter.transport.base import (
    TransportBusyError,
    TransportConnectionError,
    TransportProtocolError,
)
from tests.fake_transport import DEFAULT_UNSUPPORTED, FakeTransport


async def test_probe_marks_every_recorded_block_supported(
    justice_registers_synthetic_complete,
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    await transport.connect()
    result = await probe(transport, pause=0)
    for block in BLOCKS:
        assert result.support[block.addr] is BlockSupport.SUPPORTED, block.name
    assert len(transport.reads) == len(BLOCKS)


async def test_probe_marks_missing_block_unsupported(
    justice_registers_synthetic_complete,
):
    meter = next(b for b in BLOCKS if b.name == "meter")
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        unsupported=DEFAULT_UNSUPPORTED + (meter.addresses,),
    )
    await transport.connect()
    result = await probe(transport, pause=0)
    assert result.support[0xF02C] is BlockSupport.UNSUPPORTED
    assert result.support[0x0100] is BlockSupport.SUPPORTED
    assert "0xF02C" in " ".join(f"0x{a:04X}" for a in result.errors)


async def test_probe_retries_protocol_errors_then_marks_unsupported(
    justice_registers_synthetic_complete,
):
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        read_errors=[
            TransportProtocolError("empty"),
            TransportProtocolError("empty"),
        ],
    )
    await transport.connect()
    result = await probe(transport, pause=0, retries=1)
    assert result.support[BLOCKS[0].addr] is BlockSupport.UNSUPPORTED


async def test_probe_marks_connection_errors_unknown(
    justice_registers_synthetic_complete,
):
    errors: list[Exception | None] = [TransportConnectionError("timeout")] * 2
    errors += [None] * 32
    transport = FakeTransport(
        justice_registers_synthetic_complete, read_errors=errors
    )
    await transport.connect()
    result = await probe(transport, pause=0, retries=1)
    assert result.support[BLOCKS[0].addr] is BlockSupport.UNKNOWN
    assert result.support[BLOCKS[1].addr] is BlockSupport.SUPPORTED


async def test_probe_treats_busy_as_connection_error_with_bounded_retry(
    justice_registers_synthetic_complete,
):
    """TransportBusyError is a TransportConnectionError subclass (another
    client holds the logger's one TCP slot) -- distinct from a timeout, but
    the probe still only needs to know "back off and retry" here, bounded by
    `retries`. This pins that the retry loop stops after retries+1 attempts
    instead of spinning: a busy logger will not clear on its own within a
    single probe pass, so an unbounded retry would hang the whole probe.
    """
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        read_errors=[
            TransportBusyError("logger busy"),
            TransportBusyError("logger busy"),
        ],
    )
    await transport.connect()
    result = await probe(transport, pause=0, retries=1)
    assert result.support[BLOCKS[0].addr] is BlockSupport.UNKNOWN
    assert transport.reads.count((BLOCKS[0].addr, BLOCKS[0].count)) == 2


async def test_probe_raises_when_nothing_answered(justice_registers):
    # Every single read is intercepted by the injected-error queue before the
    # fixture dict is ever consulted, so the plain (gapped) fixture is safe
    # here -- none of its holes are ever reached.
    transport = FakeTransport(
        justice_registers,
        read_errors=[TransportConnectionError("dead")] * 100,
    )
    await transport.connect()
    with pytest.raises(ProbeFailedError):
        await probe(transport, pause=0, retries=1)


async def test_probe_returns_the_registers_it_read(
    justice_registers_synthetic_complete,
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    await transport.connect()
    result = await probe(transport, pause=0)
    assert result.registers[0x0100] == 55
    assert result.registers[0xE01E] == 15


async def test_supported_blocks_and_diagnostics(
    justice_registers_synthetic_complete,
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    await transport.connect()
    result = await probe(transport, pause=0)
    assert len(result.supported_blocks()) == len(BLOCKS)
    diag = result.as_diagnostics()
    assert diag["0x0100"] == "supported"
