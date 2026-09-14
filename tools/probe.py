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
installed.

Usage:
  tools/probe.py 192.168.188.240 --serial 3548208972 --slave 1
  tools/probe.py 192.168.188.242 --serial 3548738877 --slave 2 --json inv2.json

Only ONE client can talk to a logger at a time: stop justice_watch.py and
any other tool already pointed at the same IP before running this, or the
connection attempt below will fail with "logger busy".

Exit codes:
  0   every declared block answered SUPPORTED.
  1   the probe completed but at least one block is UNSUPPORTED or UNKNOWN.
  2   bad invocation (argparse's own convention -- missing/invalid argument).
  12  logger busy -- another client already holds its one TCP slot.
  13  timeout -- the logger/inverter never answered in time.
  14  connection failed -- could not reach host:port at all.
  15  probe failed -- connected fine, but gave up without a usable reply:
      either NOT ONE block answered (the classic symptom of a wrong
      --slave for this unit) or the probe's own deadline expired
      (the unit or link is genuinely unresponsive). Which one applies
      is on stderr.
  16  some other transport-level error.

Codes start at 12 (skipping 2) to stay clear of argparse's own exit(2) for a
bad invocation -- a script checking the raw exit code, not just stderr text,
should still be able to tell "you typed the command wrong" apart from
"the logger is busy".
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
    TransportBusyError,
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
    the names of the blocks that did NOT come back SUPPORTED."""
    values = R.decode(result.registers)

    print(f"=== {args.host} (logger {args.serial}, slave {args.slave}) ===")
    print("\nBlocks")
    for block in R.BLOCKS:
        state = result.support[block.addr]
        mark = _SUPPORT_MARK[state.value]
        note = result.errors.get(block.addr, "")
        print(f"  {mark} 0x{block.addr:04X} +{block.count:<3} {block.name:<14} {note}")

    print("\nDecoded values")
    for key in sorted(values):
        print(f"  {key:<32} {values[key]}")

    missing = [
        block.name for block in R.BLOCKS
        if result.support[block.addr] is not BlockSupport.SUPPORTED
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
        Path(args.json).write_text(json.dumps(report, indent=1, ensure_ascii=False))
        print(f"\nreport saved: {args.json}")

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
        result = await probe(transport)
    except TransportBusyError as err:
        print(
            f"ERROR: logger busy: {err}\n"
            "Another client already holds this logger's one TCP slot -- a "
            "leftover justice_watch.py (or similar), a second config entry "
            "pointed at the same IP, or Solarman's own cloud client. Stop "
            "it and retry.",
            file=sys.stderr,
        )
        return 12
    except TransportTimeoutError as err:
        print(
            f"ERROR: timeout: {err}\n"
            f"No reply within {args.timeout}s. The logger never answered at "
            "all -- check that it is powered and reachable on this network.",
            file=sys.stderr,
        )
        return 13
    except TransportConnectionError as err:
        print(
            f"ERROR: connection failed: {err}\n"
            f"Could not reach {args.host}:{args.port}. Check the IP/port "
            "and the network path to it.",
            file=sys.stderr,
        )
        return 14
    except ProbeFailedError as err:
        # probe() raises this for two distinct reasons with two distinct
        # messages (see custom_components/srne_inverter/probe.py): "no block
        # answered" (a WRONG --slave id is the classic cause -- the logger
        # accepted the TCP connection, but the inverter itself never replies
        # to Modbus) or "exceeded its deadline" (the unit or link is
        # genuinely unresponsive). {err} already carries whichever one this
        # run hit -- do not collapse the two into a single claimed cause.
        print(
            f"ERROR: probe failed: {err}\n"
            "The logger accepted the connection, but the probe gave up "
            "without a usable reply. If the message above says \"no block "
            "answered\", double check --serial and --slave match this IP -- "
            "a wrong slave id is the classic cause. If it says the deadline "
            "was exceeded, the unit or the link itself is unresponsive.",
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
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
