"""The recorded Justice registers must load into a flat address -> value map."""

from tests.conftest import load_justice_registers


def test_loader_flattens_blocks_and_settings():
    regs = load_justice_registers()
    # From justice_inv1_blocks.json, block "grid_out" at 0x0210.
    assert regs[0x0210] == 2          # machine state: AC bypass
    assert regs[0x0212] == 5266       # bus voltage 526.6 V
    assert regs[0x0213] == 1225       # grid L1 122.5 V
    assert regs[0x021E] == 10         # grid charge current 1.0 A
    # From justice_inv1_blocks.json, block "battery" at 0x0100.
    assert regs[0x0100] == 55         # SOC 55 %
    assert regs[0x0101] == 531        # 53.1 V
    assert regs[0x0102] == 65527      # raw -9 -> charging at 0.9 A
    # From justice_inv1_settings.json ("before" map, authoritative for 0xE0xx).
    assert regs[0xE004] == 0          # battery type USER
    assert regs[0xE01E] == 15         # SOC low alarm
    assert regs[0xE008] == 144


def test_loader_settings_override_older_block_capture():
    """justice_inv1_settings.json is newer, so it wins on 0xE000-0xE02F."""
    regs = load_justice_registers()
    assert regs[0xE00D] == 122
