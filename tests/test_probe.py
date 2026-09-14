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


async def test_probe_marks_every_declared_block_supported(
    justice_registers_synthetic_complete,
):
    # Named "declared", not "recorded": 59 of the addresses this reads come
    # from the synthetic fixture's 0xF00D filler (faults, inverter_b,
    # control_high, device_info and meter are only partially captured in the
    # real recording -- see the module docstring above), not from Casa
    # Justice. This test is about probe() classifying every block
    # registers.BLOCKS declares, whatever the transport answers with -- not
    # about what was actually recorded.
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
    battery = next(b for b in BLOCKS if b.name == "battery")
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        unsupported=DEFAULT_UNSUPPORTED + (meter.addresses,),
    )
    await transport.connect()
    result = await probe(transport, pause=0)
    assert result.support[0xF02C] is BlockSupport.UNSUPPORTED
    assert result.support[0x0100] is BlockSupport.SUPPORTED
    assert "0xF02C" in " ".join(f"0x{a:04X}" for a in result.errors)
    # Fix round 1, Finding 5: supported_blocks() must actually FILTER by
    # support, not just echo back every block it was given.
    supported = result.supported_blocks()
    assert meter not in supported
    assert battery in supported
    assert len(supported) == len(BLOCKS) - 1


async def test_probe_unsupported_register_error_short_circuits_without_retry(
    justice_registers_synthetic_complete,
):
    """UnsupportedRegisterError is the device's own unambiguous
    IllegalDataAddress answer -- a permanent, definitive fact. It must
    classify UNSUPPORTED on the very first attempt, spending none of
    `retries` on a verdict retrying cannot change (cheap pin requested by
    fix round 1: probe.py's no-retry short-circuit for this exception)."""
    meter = next(b for b in BLOCKS if b.name == "meter")
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        unsupported=DEFAULT_UNSUPPORTED + (meter.addresses,),
    )
    await transport.connect()
    result = await probe(transport, pause=0, retries=3)
    assert result.support[meter.addr] is BlockSupport.UNSUPPORTED
    assert transport.reads.count((meter.addr, meter.count)) == 1


async def test_probe_raises_when_every_block_is_unsupported(
    justice_registers_synthetic_complete,
):
    """Fix round 1, Finding 1 (CRITICAL): a wrong slave id is this site's
    own confusable pair (Casa Justice inverter 1 is 192.168.188.240 slave 1,
    inverter 2 is .242 slave 2) and makes every block answer
    Empty/AcknowledgeError -- every block UNSUPPORTED, none UNKNOWN. That
    must not return a "successful" ProbeResult with supported_blocks() == ()
    -- it would set up a Home Assistant device with zero entities that polls
    nothing forever, with no ConfigEntryNotReady and no recovery except a
    restart (UNSUPPORTED blocks are never re-probed). ProbeFailedError must
    fire whenever NO block is SUPPORTED -- not only when every block is
    UNKNOWN, which is the narrower case test_probe_raises_when_nothing_answered
    already covers.
    """
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        unsupported=tuple(block.addresses for block in BLOCKS),
    )
    await transport.connect()
    with pytest.raises(ProbeFailedError):
        await probe(transport, pause=0)


async def test_probe_retries_protocol_errors_then_marks_unknown(
    justice_registers_synthetic_complete,
):
    """Fix round 1, Finding 2: a TransportProtocolError (malformed/empty
    frame -- e.g. a stolen-then-returned TCP session, this hardware's most
    common real disruption) surviving its retries must become UNKNOWN, never
    a permanent UNSUPPORTED, on a block that may well be genuinely present.
    Finding 4's pin: assert the read was actually attempted twice, not just
    that the final verdict happens to be right -- a mutation that skipped
    the retry entirely must fail this test.
    """
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        read_errors=[
            TransportProtocolError("empty"),
            TransportProtocolError("empty"),
        ],
    )
    await transport.connect()
    result = await probe(transport, pause=0, retries=1)
    assert result.support[BLOCKS[0].addr] is BlockSupport.UNKNOWN
    assert transport.reads.count((BLOCKS[0].addr, BLOCKS[0].count)) == 2


async def test_probe_retries_protocol_error_once_then_recovers(
    justice_registers_synthetic_complete,
):
    """Fix round 1, Finding 4's positive case: one bad frame, then a good
    read, must recover the block as SUPPORTED -- the retry exists precisely
    so a single transient glitch does not cost a genuinely-present block its
    entities."""
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        read_errors=[TransportProtocolError("empty")],
    )
    await transport.connect()
    result = await probe(transport, pause=0, retries=1)
    assert result.support[BLOCKS[0].addr] is BlockSupport.SUPPORTED
    assert transport.reads.count((BLOCKS[0].addr, BLOCKS[0].count)) == 2


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


async def test_probe_raises_when_the_overall_deadline_is_exceeded(
    justice_registers_synthetic_complete,
):
    """Fix round 1, Finding 3: probe() must bound its OWN wall-clock time,
    independently of however long any individual read takes -- Home
    Assistant's bootstrap does not bound the config-entry setup gather, and
    the measured worst case without a deadline (10 blocks x 2 attempts x the
    transport's 10 s DEFAULT_TIMEOUT, plus pauses/backoffs) is 204.25 s for
    one unreachable unit against a 300 s budget shared by every integration.
    `pause` here is deliberately much larger than `deadline` so the timeout
    fires deterministically during the very first inter-block sleep, without
    depending on FakeTransport's (effectively zero) read latency.
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    await transport.connect()
    with pytest.raises(ProbeFailedError):
        await probe(transport, pause=0.3, retries=0, deadline=0.05)


async def test_probe_never_swallows_a_fixture_gap_lookup_error(justice_registers):
    """Cheap pin requested by fix round 1: a bare LookupError from
    FakeTransport means the test fixture itself is incomplete -- it is
    deliberately NOT a TransportError subclass and must never be caught into
    a classification (UNSUPPORTED or UNKNOWN would both silently treat a
    hole in test data as if it were a verified signal about the device).
    Uses the plain (gapped) fixture on purpose: BLOCKS[1] ("faults",
    0x0200-0x0207) is entirely missing from it and is not declared
    unsupported, so probing it raises exactly the fixture-gap LookupError
    this test exists to prove is never swallowed.
    """
    transport = FakeTransport(justice_registers)
    await transport.connect()
    with pytest.raises(LookupError):
        await probe(transport, pause=0)


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
