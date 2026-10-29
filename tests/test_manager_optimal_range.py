"""Tests for the 'optimal working range' check in power_charge / power_discharge.

With more than one active device, the manager tracks how far the share of the
first device in the sorted list falls below 1.5 x its start power (`pwr_low`)
and drops that device once the accumulated deficit exceeds its optimal power.
This keeps inverters out of their inefficient low-power range.

The discharge side works with positive values. The charge side works with
negative values (charge_start / charge_optimal are negative), so the
accumulated deficit (always >= 0) has to be compared against the magnitude of
charge_optimal.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from test_manager_dispatch import _FakeDevice, _make_manager_harness

from custom_components.zendure_ha.manager import ZendureManager


def _charge_device(level: int) -> _FakeDevice:
    # SolarFlow 2400 AC+ limits from devices/solarflow2400.py: setLimits(-3200, 2400)
    d = _FakeDevice(pwr_max=-3200, electric_level=level)
    d.charge_start = -3200 // 10  # -320
    d.charge_optimal = -3200 // 4  # -800
    return d


def _discharge_device(level: int) -> _FakeDevice:
    d = _FakeDevice(pwr_max=2400, electric_level=level)
    d.discharge_start = 2400 // 10  # 240
    d.discharge_optimal = 2400 // 4  # 600
    return d


def _prime_charge(manager: ZendureManager, devices: list[_FakeDevice]) -> None:
    manager.charge = list(devices)
    manager.charge_limit = sum(d.pwr_max for d in devices)
    manager.charge_optimal = sum(d.charge_optimal for d in devices)
    manager.charge_weight = sum(
        d.pwr_max * (100 - d.electricLevel.asInt) for d in devices
    )


def _prime_discharge(manager: ZendureManager, devices: list[_FakeDevice]) -> None:
    manager.discharge = list(devices)
    manager.discharge_limit = sum(d.pwr_max for d in devices)
    manager.discharge_optimal = sum(d.discharge_optimal for d in devices)
    manager.discharge_weight = sum(d.pwr_max * d.electricLevel.asInt for d in devices)


async def test_power_discharge_drops_device_with_small_share() -> None:
    """Drop the lower-SoC device when its discharge share stays too small.

    Reference behaviour: two devices sharing 600 W. The lower-SoC device
    (first in the ascending sort) gets 240 W < 1.5 x 240 W and is dropped once
    the accumulated deficit exceeds discharge_optimal (600 W).
    """
    manager = _make_manager_harness()
    low, high = _discharge_device(40), _discharge_device(60)

    for _ in range(12):
        _prime_discharge(manager, [low, high])
        await manager.power_discharge(600)

    assert low.discharge_calls[0] > 0
    assert low.discharge_calls[-1] == 0


async def test_power_charge_drops_device_with_small_share() -> None:
    """Drop the higher-SoC device when its charge share stays too small.

    Mirror of the discharge case: two devices charging with -600 W surplus.
    The higher-SoC device (first in the descending sort) gets -150 W, well
    below 1.5 x charge_start (-480 W). Once the accumulated deficit exceeds
    |charge_optimal| (800 W) it must be dropped (set to 0), like on the
    discharge side.
    """
    manager = _make_manager_harness()
    now = datetime(2026, 10, 29, 12, 0, 0)
    manager.charge_time = now - timedelta(seconds=1)  # hysteresis already passed
    high, low = _charge_device(80), _charge_device(40)

    for _ in range(4):
        _prime_charge(manager, [high, low])
        await manager.power_charge(-600, now)

    assert high.charge_calls[0] < 0
    assert high.charge_calls[-1] == 0


async def test_power_charge_keeps_device_with_large_share() -> None:
    """Keep both devices when their charge shares are large enough.

    Regression guard: with both shares well above the threshold the deficit
    counter stays at 0 and no device is dropped.
    """
    manager = _make_manager_harness()
    now = datetime(2026, 10, 29, 12, 0, 0)
    manager.charge_time = now - timedelta(seconds=1)
    high, low = _charge_device(55), _charge_device(45)

    for _ in range(10):
        _prime_charge(manager, [high, low])
        await manager.power_charge(-3000, now)

    assert manager.pwr_low == 0
    assert all(p < 0 for p in high.charge_calls)
