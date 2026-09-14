"""DataUpdateCoordinator for one SRNE inverter behind one Solarman logger."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field as dc_field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import registers as R
from .const import BACKOFF_SECONDS, BLOCK_PAUSE, DOMAIN, REPROBE_AFTER_FAILURES, WRITE_SETTLE
from .probe import BlockSupport, ProbeFailedError, ProbeResult, probe
from .registers import BlockTier, Field
from .transport.base import Transport, TransportError, UnsupportedRegisterError

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SrneData:
    """One decoded snapshot handed to the entities."""

    registers: dict[int, int] = dc_field(default_factory=dict)
    values: dict[str, object] = dc_field(default_factory=dict)
    support: dict[int, BlockSupport] = dc_field(default_factory=dict)


class SrneCoordinator(DataUpdateCoordinator[SrneData]):
    """Polls one inverter in three tiers over a single, locked connection.

    Tier scheduling: `_tier_due` maps a tier to the monotonic time it next
    becomes due. A tier with no entry in that dict (the state right after
    __init__) is treated as due immediately -- see `_read_due_blocks`'s
    `self._tier_due.get(tier, 0.0)` default, which is always
    <= `time.monotonic()`.

    Fix round 1 (Task 7 review, Finding 6): `async_probe()` seeds every
    tier's next deadline from the probe's own read time (`now + interval`)
    rather than clearing the schedule to "everything due". An earlier
    version of this method cleared it, which made the very first poll cycle
    after any probe re-read all 10 blocks a SECOND time -- the probe had
    just read every one of them moments earlier. Seeding is also what the
    original brief's starter code did, but it seeded WARM/COLD too eagerly
    (see the History note below) -- this only reschedules from the probe's
    OWN `now`, which is correct because the probe really did just read
    everything at that instant.

    History: the brief this task was built from pre-seeded WARM/COLD the
    same way, but its very first test asserted the OPPOSITE ("first cycle is
    due on every tier") -- a real contradiction between the brief's starter
    code and the brief's own test, not a synonym for what is implemented
    now. The first fix round (see `task-7-report.md`) resolved that by
    clearing the schedule instead, which the review then flagged as
    needlessly doubling every probe's cost; this docstring records the third
    and final shape: seed correctly, and let the test that used to demand
    "read everything again" instead assert "only HOT needs re-reading,
    because the probe already has fresh WARM/COLD data"
    (`test_probe_then_first_cycle_only_rereads_hot`).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        transport: Transport,
        *,
        scan_interval: int,
        warm_interval: int,
        cold_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.transport = transport
        self.scan_interval = scan_interval
        self.warm_interval = warm_interval
        self.cold_interval = cold_interval

        self.probe_result: ProbeResult | None = None
        self.failure_count = 0
        self._connection_enabled = True
        self._registers: dict[int, int] = {}
        self._tier_due: dict[BlockTier, float] = {}
        self._backoff_until = 0.0
        self._backoff_index = 0
        self._reprobe_pending = False

    # ---- state ---------------------------------------------------------

    @property
    def connection_enabled(self) -> bool:
        return self._connection_enabled

    @property
    def support(self) -> dict[int, BlockSupport]:
        # Fix round 2 (Task 7 review): return a COPY, not the live dict.
        # self.probe_result.support is the exact object _read_due_blocks
        # mutates in place for the UnsupportedRegisterError reclassification
        # (Fix round 1, Finding 1), and every internal reader (this class's
        # own supported_field_keys(), the block-polling filter, SrneData
        # construction) reads it, never mutates it -- so handing out the
        # live dict costs nothing internally but lets any external caller
        # (any of the eight downstream tasks) corrupt the coordinator's own
        # capability map by mutating what looked like a harmless snapshot,
        # silently, with no error and no test to catch it.
        return dict(self.probe_result.support) if self.probe_result else {}

    def supported_field_keys(self) -> set[str]:
        """Field and derived keys whose blocks answered during the probe."""
        supported = {
            addr for addr, state in self.support.items()
            if state is BlockSupport.SUPPORTED
        }
        keys = {f.key for f in R.FIELDS if f.block_addr in supported}
        keys |= {
            d.key for d in R.DERIVED if all(i in keys for i in d.inputs)
        }
        return keys

    # ---- probe -----------------------------------------------------------

    async def async_probe(self) -> ProbeResult:
        """Run the capability probe, reusing the open connection.

        Raises ProbeFailedError (from .probe) if no block answered SUPPORTED
        -- deliberately NOT caught here. The caller (async_setup_entry, a
        later task) is the one that turns that into ConfigEntryNotReady; a
        coordinator that swallowed it here would set up a device with zero
        entities that polls nothing, which is the exact silent-failure mode
        probe.py's own "Fix round 1" exists to prevent.

        Fix round 1 (Task 7 review, Finding 12): on that failure path this
        now closes the transport before re-raising. Leaving the socket open
        would strand a connected client across whatever ConfigEntryNotReady
        retry follows -- holding the logger's one TCP slot hostage against
        this integration's own next attempt, the standalone CLI probe, and
        Gabriel's own justice_watch.py all at once. A caller is still free to
        close it again (idempotent, see Transport.close()'s contract); this
        just guarantees the failure path never leaves it open by omission.
        """
        if not self.transport.connected:
            await self.transport.connect()
        try:
            result = await probe(self.transport, pause=BLOCK_PAUSE)
        except ProbeFailedError:
            await self.transport.close()
            raise
        self.probe_result = result
        self._registers.update(result.registers)
        self._reprobe_pending = False
        # The probe just read every block once: seed every tier's next
        # deadline from THIS moment instead of leaving everything due
        # immediately. See the class docstring's "Fix round 1" note for why
        # this used to clear the schedule and cost a second full ten-block
        # pass on every probe.
        now = time.monotonic()
        self._tier_due = {
            BlockTier.HOT: now + self.scan_interval,
            BlockTier.WARM: now + self.warm_interval,
            BlockTier.COLD: now + self.cold_interval,
        }
        _LOGGER.debug("Probe result for %s: %s", self.name, result.as_diagnostics())
        return result

    # ---- polling -----------------------------------------------------------

    async def _async_update_data(self) -> SrneData:
        if not self._connection_enabled:
            raise UpdateFailed("connection disabled by the user")

        now = time.monotonic()
        if now < self._backoff_until:
            raise UpdateFailed(
                f"backing off for {self._backoff_until - now:.0f} s after "
                f"{self.failure_count} failures"
            )

        try:
            if not self.transport.connected:
                await self.transport.connect()
            if self._reprobe_pending:
                await self.async_probe()
            await self._read_due_blocks()
        except TransportError as err:
            await self._register_failure(err)
            # Include the concrete exception type (TransportBusyError vs.
            # TransportTimeoutError vs. plain TransportConnectionError) in
            # the surfaced message: telling "someone else holds the logger"
            # apart from "the logger didn't answer" is this project's
            # number-one support question, and str(err) alone loses it.
            raise UpdateFailed(f"{type(err).__name__}: {err}") from err

        self._register_success()
        return SrneData(
            registers=dict(self._registers),
            values=R.decode(self._registers),
            support=dict(self.support),
        )

    async def _read_due_blocks(self) -> None:
        now = time.monotonic()
        # A tier absent from `_tier_due` (never scheduled yet, or just reset
        # by async_probe()) defaults to 0.0, which is always <= `now` -- i.e.
        # due immediately. Iterating over the tier ENUM (not
        # `self._tier_due.items()`) is what makes that default apply; a dict
        # with no entries at all would otherwise yield an empty `due`.
        due = {tier for tier in BlockTier if now >= self._tier_due.get(tier, 0.0)}
        if not due:
            # Every tier already has a future deadline (the normal steady
            # state, called again well inside the HOT interval): still poll
            # HOT every cycle, matching the coordinator's own natural cadence
            # (update_interval == scan_interval).
            due = {BlockTier.HOT}

        blocks = [
            block
            for block in R.BLOCKS
            if block.tier in due
            and self.support.get(block.addr) is not BlockSupport.UNSUPPORTED
        ]
        for index, block in enumerate(blocks):
            if index:
                await asyncio.sleep(BLOCK_PAUSE)
            if not self._connection_enabled:
                # Fix round 1, Finding 2: a pause can land WHILE a cycle is
                # already in flight (async_set_connection_enabled(False) only
                # runs between cycles from this method's own point of view).
                # Checked AFTER the pause-sleep above, not just at the top of
                # the loop: the sleep is the one real yield point per block,
                # so this is where a pause that landed during it is actually
                # observed, immediately before the read that would otherwise
                # raise TransportConnectionError on the now-closed socket --
                # which used to get booked as a device failure (backoff
                # armed, an HA error logged) for what was a deliberate user
                # action. Stop quietly instead; blocks already read this
                # cycle keep their data, and the tiers still pending stay
                # due (the reschedule loop below is never reached for them).
                _LOGGER.debug(
                    "%s: connection paused mid-cycle, stopping after %d/%d "
                    "due blocks", self.name, index, len(blocks),
                )
                return
            try:
                values = await self.transport.read_holding(block.addr, block.count)
            except UnsupportedRegisterError as err:
                # Fix round 1, Finding 1: IllegalDataAddress is the device
                # answering on a socket that is demonstrably fine -- proof
                # the block does not exist, not evidence the link is down.
                # Reclassify it (permanently, matching probe()'s own
                # UNSUPPORTED semantics) and carry on with the rest of this
                # cycle instead of tearing down the one-slot session and
                # discarding every block already read successfully.
                if self.probe_result is not None:
                    self.probe_result.support[block.addr] = BlockSupport.UNSUPPORTED
                _LOGGER.debug(
                    "%s: 0x%04X answered IllegalDataAddress mid-poll; "
                    "reclassified UNSUPPORTED (%s)",
                    self.name, block.addr, err,
                )
                continue
            for offset, value in enumerate(values):
                self._registers[block.addr + offset] = value

        interval = {
            BlockTier.HOT: self.scan_interval,
            BlockTier.WARM: self.warm_interval,
            BlockTier.COLD: self.cold_interval,
        }
        for tier in due:
            self._tier_due[tier] = now + interval[tier]

    def _register_success(self) -> None:
        self.failure_count = 0
        self._backoff_index = 0
        self._backoff_until = 0.0

    async def _register_failure(self, err: Exception) -> None:
        self.failure_count += 1
        delay = BACKOFF_SECONDS[min(self._backoff_index, len(BACKOFF_SECONDS) - 1)]
        self._backoff_index += 1
        self._backoff_until = time.monotonic() + delay
        if self.failure_count >= REPROBE_AFTER_FAILURES:
            # The unit may have come back different; re-probe on reconnect.
            self._reprobe_pending = True
        _LOGGER.debug(
            "%s: read cycle failed (%s: %s); backing off %.0f s",
            self.name, type(err).__name__, err, delay,
        )
        await self.transport.close()

    # ---- writes ------------------------------------------------------------

    async def async_write_field(self, field: Field, value: float | str) -> None:
        """Write a mapped field and verify the read-back."""
        try:
            raw = R.encode(field, value)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self.async_write_raw(field.address, raw)

    async def async_write_raw(self, address: int, raw: int) -> int:
        """Write one raw register, re-read it and fail on any mismatch.

        Deviation from the task brief: the brief's starter code called
        `write_holding` and `read_holding` as two SEPARATE, independently
        locked calls with a plain `asyncio.sleep(WRITE_SETTLE)` between them.
        That does not satisfy transport.base.Transport.atomic()'s own
        stated purpose ("a caller that needs a write followed by its own
        read-back to happen as one unit") -- another task's poll could
        acquire the lock in the gap between the write and the read-back.
        This uses `atomic()` instead, exactly as
        `tests/fake_transport.py`'s FakeTransport.atomic() docstring says
        Task 7 should ("This is what lets Task 7/12/15 test a
        write-plus-read-back bracket against this fake").
        """
        if not self._connection_enabled:
            raise HomeAssistantError("the connection to this inverter is disabled")
        try:
            if not self.transport.connected:
                await self.transport.connect()
            async with self.transport.atomic() as locked:
                await locked.write_holding(address, raw)
                await asyncio.sleep(WRITE_SETTLE)
                read_back = (await locked.read_holding(address, 1))[0]
        except TransportError as err:
            raise HomeAssistantError(
                f"write to 0x{address:04X} failed: {type(err).__name__}: {err}"
            ) from err

        if read_back != raw:
            raise HomeAssistantError(
                f"0x{address:04X}: wrote {raw} but read back {read_back}; "
                "the inverter refused or clamped the value"
            )

        self._registers[address] = read_back
        self.async_set_updated_data(
            SrneData(
                registers=dict(self._registers),
                values=R.decode(self._registers),
                support=dict(self.support),
            )
        )
        return read_back

    async def async_read_raw(self, address: int, count: int) -> list[int]:
        """Read raw registers on demand (service `read_register`)."""
        if not self._connection_enabled:
            raise HomeAssistantError("the connection to this inverter is disabled")
        try:
            if not self.transport.connected:
                await self.transport.connect()
            return await self.transport.read_holding(address, count)
        except TransportError as err:
            raise HomeAssistantError(
                f"read of 0x{address:04X} failed: {type(err).__name__}: {err}"
            ) from err

    # ---- connection switch ---------------------------------------------

    async def async_set_connection_enabled(self, enabled: bool) -> None:
        """Pause/resume polling so external tools can own the logger.

        The paused flag is set BEFORE awaiting close() (not after): close()
        on the real transport runs entirely under the transport lock and can
        take up to ~14 s (a 10 s connect plus two 2 s teardown windows) if it
        races a connect() -- the switch's turn-off must not block on that
        teardown to already report itself as disabled.
        """
        if enabled == self._connection_enabled:
            return
        self._connection_enabled = enabled
        if enabled:
            self.update_interval = timedelta(seconds=self.scan_interval)
            self._backoff_until = 0.0
            self._backoff_index = 0
            await self.async_refresh()
        else:
            self.update_interval = None
            await self.transport.close()
            self.last_update_success = False
            self.async_update_listeners()

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.transport.close()
