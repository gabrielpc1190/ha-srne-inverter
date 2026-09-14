#!/usr/bin/env python3
"""Probe one SRNE/BlueSun inverter through its Solarman logger and print what
it actually supports, using the very same register map the Home Assistant
integration uses.

Home Assistant is NOT required: the core modules (registers, probe,
transport.base, transport.solarman_v5) are imported below as bare
TOP-LEVEL modules, by putting custom_components/srne_inverter/ on
sys.path -- that never executes that package's __init__.py, and so never
pulls in homeassistant. This script is meant to run standalone on a
technician's laptop, on a client's network, where Home Assistant is not
installed. What such a laptop DOES need: `pip install "pysolarmanv5>=3.0.6,<4"`
(pulls in umodbus and pyserial transitively) -- that dependency is otherwise
only declared in manifest.json, which is for Home Assistant to read, not a
technician.

Usage:
  tools/probe.py 192.168.188.240 --serial 3548208972 --slave 1
  tools/probe.py 192.168.188.242 --serial 3548738877 --slave 2 --json inv2.json

Only ONE client can talk to a logger at a time: stop justice_watch.py and
any other tool already pointed at the same IP before running this. If you
don't, this tool cannot promise to tell you so distinctly -- see the note on
exit code 14 below; the logger's own reaction to a second client is, by this
project's own transport design, indistinguishable at the socket level from a
wrong IP.

The probe below has NO overall time limit (unlike the Home Assistant
runtime, which bounds it to protect its own startup budget) -- against a
genuinely silent unit it can take on the order of a few minutes. Ctrl-C
aborts cleanly and releases the logger's slot.

Exit codes:
  0   every declared block answered SUPPORTED.
  1   the probe completed but at least one block is UNSUPPORTED or UNKNOWN
      (see the block table above this line for which, and why).
  2   bad invocation (argparse's own convention -- missing/invalid argument).
  13  timeout -- the logger/inverter never answered a single request in time.
  14  connection failed -- could not open a session to host:port at all.
      This is ALSO what a busy logger looks like (see the note above): the
      transport cannot tell "wrong IP" apart from "another client already
      holds the slot" at connect time, by design, so this message covers
      both and the text on stderr is what there is to go on.
  15  probe failed -- connected fine, but gave up without a usable reply.
      The per-block table this prints (from the partial result the probe
      DID collect) is the real diagnosis -- read it rather than assuming a
      single cause.
  16  some other transport-level error.
  130 aborted by Ctrl-C; the logger's slot was released before exiting.

Codes start at 13 (skipping 2) to stay clear of argparse's own exit(2) for a
bad invocation -- a script checking the raw exit code, not just stderr text,
should still be able to tell "you typed the command wrong" apart from a
probe failure. Code 12 does not appear: an earlier draft reserved it for a
directly-detected "logger busy" exception that this transport's connect()
never actually raises (see the exit-14 note) -- removed rather than reused,
so a reader of an old report doesn't go looking for a distinction that was
never real.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "srne_inverter"
sys.path.insert(0, str(COMPONENT))

import registers as R  # noqa: E402
from probe import BlockSupport, ProbeFailedError, ProbeResult, probe  # noqa: E402
from transport.base import (  # noqa: E402
    TransportConnectionError,
    TransportError,
    TransportTimeoutError,
)
from transport.solarman_v5 import SolarmanV5Transport  # noqa: E402

_SUPPORT_MARK = {"supported": "OK ", "unsupported": "-- ", "unknown": "?? "}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools/probe.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("host", help="logger IP or hostname")
    parser.add_argument("--serial", type=int, required=True, help="logger serial number")
    parser.add_argument("--port", type=int, default=8899, help="logger TCP port (default: 8899)")
    parser.add_argument("--slave", type=int, default=1, help="Modbus slave id (default: 1)")
    parser.add_argument(
        "--timeout", type=float, default=10.0,
        help="per-request timeout in seconds (default: 10)",
    )
    parser.add_argument("--json", metavar="OUT", help="also write the full report to this file")
    return parser


def render_report(args: argparse.Namespace, result: ProbeResult) -> list[str]:
    """Print the human-readable report, optionally write --json, and return
    the names of the blocks that did NOT come back SUPPORTED.

    A pure function of (args, result) -- no transport, no I/O beyond
    stdout/stderr and the optional --json file -- so it can be, and is (see
    tests/test_probe_cli.py), exercised directly against a hand-built
    ProbeResult instead of only ever running against live hardware. Also
    called with a PARTIAL result on a failed probe (see the ProbeFailedError
    branch in run()), which is exactly why every read from `result` here
    uses .get()/indexing that tolerates a block never having been reached.
    """
    values = R.decode(result.registers)

    print(f"=== {args.host} (logger {args.serial}, slave {args.slave}) ===")
    print("\nBlocks")
    for block in R.BLOCKS:
        state = result.support.get(block.addr)
        mark = _SUPPORT_MARK[state.value] if state is not None else "?? "
        note = result.errors.get(block.addr, "" if state is not None else "not reached")
        print(f"  {mark} 0x{block.addr:04X} +{block.count:<3} {block.name:<14} {note}")

    print("\nDecoded values")
    for key in sorted(values):
        print(f"  {key:<32} {values[key]}")

    missing = [
        block.name for block in R.BLOCKS
        if result.support.get(block.addr) is not BlockSupport.SUPPORTED
    ]
    print(
        f"\n{len(R.BLOCKS) - len(missing)}/{len(R.BLOCKS)} blocks supported"
        + (f"; missing: {', '.join(missing)}" if missing else "")
    )

    if args.json:
        report = {
            "host": args.host,
            "serial": args.serial,
            "slave": args.slave,
            "support": result.as_diagnostics(),
            "errors": {f"0x{addr:04X}": err for addr, err in result.errors.items()},
            "registers": {
                f"0x{addr:04X}": value for addr, value in sorted(result.registers.items())
            },
            "values": values,
        }
        try:
            Path(args.json).write_text(
                json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8"
            )
            print(f"\nreport saved: {args.json}")
        except OSError as err:
            # Deliberately NOT fatal, and deliberately does not change the
            # return value: whether the OPTIONAL --json side artifact could
            # be written is orthogonal to whether the probe itself found
            # every declared block supported, and the human already has the
            # report above on stdout regardless. Collapsing this into the
            # same exit code as "some blocks missing" would make exit 1
            # ambiguous between two unrelated conditions.
            print(f"\nWARNING: could not write --json report to {args.json}: {err}", file=sys.stderr)

    return missing


async def run(args: argparse.Namespace) -> int:
    """Connect, probe, always close -- even on error, since the logger has
    exactly one TCP slot and a leaked socket locks everyone else out."""
    transport = SolarmanV5Transport(
        args.host, args.serial, port=args.port,
        slave_id=args.slave, timeout=args.timeout,
    )
    try:
        await transport.connect()
        # deadline=None: probe()'s 60 s default exists to protect Home
        # Assistant's shared startup budget (see probe.py's DEFAULT_DEADLINE
        # docstring) -- a human at a terminal, explicitly running this tool,
        # has no such shared budget and would rather wait than have a
        # perfectly good partial result thrown away by a deadline that was
        # never meant for this caller. Ctrl-C is the human's own deadline.
        result = await probe(transport, deadline=None)
    except TransportTimeoutError as err:
        print(
            f"ERROR: timeout: {err}\n"
            f"No reply within {args.timeout}s. The logger never answered at "
            "all -- check that it is powered and reachable on this network.",
            file=sys.stderr,
        )
        return 13
    except TransportConnectionError as err:
        # This is ALSO what a busy logger looks like: connect()'s own
        # deliberate design (transport/solarman_v5.py's
        # _translate_no_socket_available, phase="connect") maps a refused,
        # unreachable, AND "another client already has this logger's one
        # slot" connection attempt onto the SAME plain TransportConnectionError
        # -- distinguishing a mistyped IP from a busy logger from a rejected
        # second client is not information the transport has at this point,
        # by its own documented design, not an oversight here.
        print(
            f"ERROR: could not establish a connection: {err}\n"
            f"Could not open a session to {args.host}:{args.port}. This "
            "means either the IP/port or network path is wrong, OR another "
            "client already holds this logger's one TCP slot (a leftover "
            "justice_watch.py, a second config entry on this IP, Solarman's "
            "own cloud client) -- the message above is everything there is "
            "to go on to tell those apart. If in doubt, stop whatever else "
            f"might be talking to {args.host} and retry.",
            file=sys.stderr,
        )
        return 14
    except ProbeFailedError as err:
        # probe() attaches the ProbeResult it accumulated (possibly partial)
        # as err.result -- report what it actually recorded per block
        # instead of asserting a single likely cause. A session stolen
        # mid-probe and a wrong --slave id both end up here, with two very
        # different sets of per-block errors; only the table below can tell
        # them apart (fix round 1, task-6 review Critical 2).
        print(f"ERROR: probe failed: {err}", file=sys.stderr)
        if err.result is not None:
            print(
                "Per-block detail from before the probe gave up (this is "
                "the actual measured cause -- read it before assuming a "
                "wrong --slave id):",
                file=sys.stderr,
            )
            render_report(args, err.result)
        else:
            print(
                "No per-block detail was recorded. Double check --serial "
                "and --slave match this unit, and that it is powered and "
                "reachable.",
                file=sys.stderr,
            )
        return 15
    except TransportError as err:
        print(f"ERROR: {type(err).__name__}: {err}", file=sys.stderr)
        return 16
    finally:
        await transport.close()

    missing = render_report(args, result)
    return 0 if not missing else 1


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        # run()'s own `finally: await transport.close()` already ran before
        # this propagates -- asyncio.run()/Runner cancels the in-flight
        # coroutine and lets its finally blocks execute before re-raising --
        # so the logger's one slot IS released; without this, the human
        # sees a 40+ line asyncio traceback with no way to tell whether that
        # happened, at exactly the moment they most need to know before
        # restarting justice_watch.py or similar.
        print("\naborted -- logger released", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
