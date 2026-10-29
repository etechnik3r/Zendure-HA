"""Tests for the configurable power distribution thresholds.

Three manager settings replace formerly hard-coded values:

* distribution_start_pct (default 50): another device is started when the
  setpoint exceeds this share of the active devices' limits
  (previously 2 x optimal = 2 x limit // 4).
* distribution_stop_pct (default 15): the first device in the sort order counts
  as underused when its share is below this share of its own limit
  (previously 1.5 x start = 1.5 x limit // 10).
* distribution_soc_diff (default 3): an idle device is started when its SoC
  differs from an active device by more than this (previously + 3).

The defaults must reproduce the previous behaviour exactly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_manager_dispatch import _FakeDevice, _make_manager_harness

from custom_components.zendure_ha.const import DeviceState
from custom_components.zendure_ha.manager import ZendureManager

TRANSLATIONS = Path(__file__).parent.parent / "custom_components" / "zendure_ha" / "translations"
NEW_KEYS = ("distribution_start", "distribution_stop", "distribution_soc_diff")


def _device(level: int, limit: int = 2400) -> _FakeDevice:
    """Discharging device (pwr_max = positive discharge limit)."""
    d = _FakeDevice(pwr_max=limit, electric_level=level)
    d.discharge_start = limit // 10
    d.discharge_optimal = limit // 4
    return d


def _charge_device(level: int, limit: int = -3200) -> _FakeDevice:
    """Charging device (pwr_max = negative charge limit, SF 2400 AC+: -3200)."""
    d = _FakeDevice(pwr_max=limit, electric_level=level)
    d.charge_start = limit // 10
    d.charge_optimal = limit // 4
    return d


def _prime_discharge(manager: ZendureManager, active: list[_FakeDevice], idle: list[_FakeDevice] | None = None) -> None:
    manager.discharge = list(active)
    manager.discharge_limit = sum(d.pwr_max for d in active)
    manager.discharge_optimal = sum(d.discharge_optimal for d in active)
    manager.discharge_weight = sum(d.pwr_max * d.electricLevel.asInt for d in active)
    manager.idle = list(idle or [])
    manager.idle_lvlmax = max((d.electricLevel.asInt for d in manager.idle), default=0)


def _prime_charge(manager: ZendureManager, active: list[_FakeDevice], idle: list[_FakeDevice] | None = None) -> None:
    manager.charge = list(active)
    manager.charge_limit = sum(d.pwr_max for d in active)
    manager.charge_optimal = sum(d.charge_optimal for d in active)
    manager.charge_weight = sum(d.pwr_max * (100 - d.electricLevel.asInt) for d in active)
    manager.idle = list(idle or [])
    manager.idle_lvlmin = min((d.electricLevel.asInt for d in manager.idle), default=100)


def _charging_harness() -> tuple[ZendureManager, datetime]:
    manager = _make_manager_harness()
    now = datetime(2026, 10, 29, 12, 0, 0)
    manager.charge_time = now - timedelta(seconds=1)  # hysteresis already passed
    return manager, now


# ── defaults reproduce the previous hard-coded values ──────────────────────


def test_defaults_match_previous_constants() -> None:
    manager = _make_manager_harness()
    for limit in (800, 1000, 1200, 1600, 2400, 3200):
        optimal, start = limit // 4, limit // 10
        assert manager.start_power(optimal) == optimal * 2
        assert manager.stop_power(start) == start * 1.5
        assert manager.start_power(-optimal) == -optimal * 2
        assert manager.stop_power(-start) == -start * 1.5
    assert manager.distribution_soc_diff == 3


# ── start threshold ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("start_pct", "expect_start"), [(50, False), (30, True)])
async def test_discharge_start_threshold(start_pct: float, expect_start: bool) -> None:
    """1000 W on one 2400 W device: default starts the second only above 1200 W."""
    manager = _make_manager_harness()
    manager.distribution_start_pct = start_pct
    active, idle = _device(60), _device(60)
    _prime_discharge(manager, [active], [idle])

    await manager.power_discharge(1000)

    assert (idle.discharge_calls == [50]) is expect_start


@pytest.mark.parametrize(("start_pct", "expect_start"), [(50, False), (30, True)])
async def test_charge_start_threshold(start_pct: float, expect_start: bool) -> None:
    """-1200 W surplus on one -3200 W device: default starts the second only beyond -1600 W."""
    manager, now = _charging_harness()
    manager.distribution_start_pct = start_pct
    active, idle = _charge_device(50), _charge_device(50)
    _prime_charge(manager, [active], [idle])

    await manager.power_charge(-1200, now)

    assert (idle.charge_calls == [-50]) is expect_start


# ── SoC balance difference ─────────────────────────────────────────────────


@pytest.mark.parametrize(("soc_diff", "expect_start"), [(3, True), (10, False)])
async def test_discharge_soc_balance(soc_diff: float, expect_start: bool) -> None:
    """Idle device has 5 % more SoC than the active one at only 600 W load."""
    manager = _make_manager_harness()
    manager.distribution_soc_diff = soc_diff
    active, idle = _device(50), _device(55)
    _prime_discharge(manager, [active], [idle])

    await manager.power_discharge(600)

    assert (idle.discharge_calls == [50]) is expect_start


@pytest.mark.parametrize(("soc_diff", "expect_start"), [(3, True), (10, False)])
async def test_charge_soc_balance(soc_diff: float, expect_start: bool) -> None:
    """Idle device has 5 % less SoC than the charging one at only -600 W surplus."""
    manager, now = _charging_harness()
    manager.distribution_soc_diff = soc_diff
    active, idle = _charge_device(55), _charge_device(50)
    _prime_charge(manager, [active], [idle])

    await manager.power_charge(-600, now)

    assert (idle.charge_calls == [-50]) is expect_start


# ── stop threshold ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(("stop_pct", "expect_drop"), [(15, False), (25, True), (0, False)])
async def test_discharge_stop_threshold(stop_pct: float, expect_drop: bool) -> None:
    """Two devices share 800 W (about 400 W each, i.e. ~17 % of 2400 W)."""
    manager = _make_manager_harness()
    manager.distribution_start_pct = 80  # keeps the hysteresis guard (start / 2 - 5) out of the way
    manager.distribution_stop_pct = stop_pct
    low, high = _device(50), _device(50)

    for _ in range(15):
        _prime_discharge(manager, [low, high])
        await manager.power_discharge(800)

    assert (low.discharge_calls[-1] == 0) is expect_drop


@pytest.mark.parametrize(("stop_pct", "expect_drop"), [(15, False), (25, True), (0, False)])
async def test_charge_stop_threshold(stop_pct: float, expect_drop: bool) -> None:
    """Two devices share -1200 W (about -600 W each, i.e. ~19 % of 3200 W)."""
    manager, now = _charging_harness()
    manager.distribution_start_pct = 80  # keeps the hysteresis guard (start / 2 - 5) out of the way
    manager.distribution_stop_pct = stop_pct
    high, low = _charge_device(50), _charge_device(50)

    for _ in range(15):
        _prime_charge(manager, [high, low])
        await manager.power_charge(-1200, now)

    assert (high.charge_calls[-1] == 0) is expect_drop


# ── safety: thresholds never exceed device / fuse group limits ─────────────


@pytest.mark.parametrize("start_pct", [10, 50, 100])
@pytest.mark.parametrize("stop_pct", [0, 15, 50])
async def test_thresholds_respect_pwr_max(start_pct: float, stop_pct: float) -> None:
    """pwr_max (set by the fuse group) caps every device regardless of thresholds."""
    manager = _make_manager_harness()
    manager.distribution_start_pct = start_pct
    manager.distribution_stop_pct = stop_pct
    a, b = _device(70, limit=1200), _device(40, limit=1200)  # e.g. shared 2400 W fuse group

    for setpoint in (300, 900, 2400, 5000):
        _prime_discharge(manager, [a, b])
        await manager.power_discharge(setpoint)
        assert all(0 <= p <= 1200 for p in a.discharge_calls + b.discharge_calls)
        assert a.discharge_calls[-1] + b.discharge_calls[-1] <= max(setpoint, 0)


# ── settings entity handling ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "value", "attr", "expected"),
    [
        ("distribution_start", 80, "distribution_start_pct", 80),
        ("distribution_start", 5, "distribution_start_pct", 10),
        ("distribution_start", 500, "distribution_start_pct", 100),
        ("distribution_stop", 0, "distribution_stop_pct", 0),
        ("distribution_stop", 90, "distribution_stop_pct", 50),
        ("distribution_soc_diff", 0, "distribution_soc_diff", 1),
        ("distribution_soc_diff", 10, "distribution_soc_diff", 10),
    ],
)
def test_update_distribution_clamps(key: str, value: float, attr: str, expected: float) -> None:
    manager = _make_manager_harness()
    manager.update_distribution(SimpleNamespace(translation_key=key), value)
    assert getattr(manager, attr) == expected


def test_update_distribution_ignores_invalid_values() -> None:
    manager = _make_manager_harness()
    manager.update_distribution(SimpleNamespace(translation_key="distribution_start"), "unknown")
    assert manager.distribution_start_pct == 50


def test_class_defaults_are_not_shared_after_update() -> None:
    """Updating one manager must not change the class defaults."""
    manager = _make_manager_harness()
    manager.update_distribution(SimpleNamespace(translation_key="distribution_soc_diff"), 12)
    assert ZendureManager.distribution_soc_diff == 3
    assert _make_manager_harness().distribution_soc_diff == 3


@pytest.mark.parametrize("lang", ["en", "de"])
def test_translations_contain_new_number_entities(lang: str) -> None:
    """en.json also feeds the entity clean-up map (EntityDevice.checkEntity)."""
    numbers = json.loads((TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))["entity"]["number"]
    for key in NEW_KEYS:
        assert numbers[key]["name"]


async def test_idle_socempty_device_is_not_started() -> None:
    """Unchanged rule: a SOCEMPTY idle device is never started, even with a low start threshold."""
    manager = _make_manager_harness()
    manager.distribution_start_pct = 10
    active, empty = _device(60), _device(5)
    empty.state = DeviceState.SOCEMPTY
    _prime_discharge(manager, [active], [empty])

    await manager.power_discharge(2000)

    assert empty.discharge_calls == []


# ── number entities: user write and restore after restart (real HA objects) ──


def _threshold_number(device, manager: ZendureManager, key: str, maximum: int, minimum: int):  # noqa: ANN001, ANN202
    from homeassistant.components.number import NumberMode

    from custom_components.zendure_ha.number import ZendureRestoreNumber

    return ZendureRestoreNumber(device, key, manager.update_distribution, None, "%", None, maximum, minimum, NumberMode.BOX, True)


async def test_threshold_written_by_user_reaches_manager(zensdk_device) -> None:  # noqa: ANN001
    manager = _make_manager_harness()
    number = _threshold_number(zensdk_device, manager, "distribution_start", 100, 10)

    await number.async_set_native_value(80)

    assert manager.distribution_start_pct == 80
    assert number.native_value == 80

    await number.async_set_native_value(150)  # above the entity range -> clamped to 100

    assert manager.distribution_start_pct == 100


async def test_threshold_is_restored_after_restart(hass, zensdk_device) -> None:  # noqa: ANN001
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    manager = _make_manager_harness()
    number = _threshold_number(zensdk_device, manager, "distribution_soc_diff", 50, 1)
    number.hass = hass
    number.entity_id = "number.solarflow_2400_ac_distribution_soc_diff"
    mock_restore_cache(hass, [State(number.entity_id, "12")])

    await number.async_added_to_hass()

    assert manager.distribution_soc_diff == 12
    assert number.native_value == 12


async def test_threshold_without_stored_state_keeps_default(hass, zensdk_device) -> None:  # noqa: ANN001
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    manager = _make_manager_harness()
    number = _threshold_number(zensdk_device, manager, "distribution_stop", 50, 0)
    number.update_value(manager.distribution_stop_pct)  # as done in ZendureManager.loadDevices
    number.hass = hass
    number.entity_id = "number.solarflow_2400_ac_distribution_stop"
    mock_restore_cache(hass, [])

    await number.async_added_to_hass()

    assert manager.distribution_stop_pct == 15
    assert number.native_value == 15


# ── hysteresis guard between start and stop threshold ──────────────────────


@pytest.mark.parametrize(
    ("start_pct", "stop_pct", "effective"),
    [(50, 15, 15), (50, 25, 20), (20, 30, 5), (10, 50, 0), (100, 50, 45)],
)
def test_effective_stop_keeps_hysteresis_band(start_pct: float, stop_pct: float, effective: float) -> None:
    manager = _make_manager_harness()
    manager.distribution_start_pct = start_pct
    manager.distribution_stop_pct = stop_pct
    assert manager.effective_stop_pct() == effective


async def test_no_toggling_with_close_thresholds() -> None:
    """start 20 % / stop 30 % would toggle; the guard keeps the second device running.

    One device carries 900 W -> above 20 % of 2400 W, so the idle device is started.
    Afterwards both share ~450 W (~19 %), which is below the configured 30 % but above
    the effective stop threshold (5 %), so neither device is dropped again.
    """
    manager = _make_manager_harness()
    manager.distribution_start_pct = 20
    manager.distribution_stop_pct = 30
    a, b = _device(50), _device(50)

    _prime_discharge(manager, [a], [b])
    await manager.power_discharge(900)
    assert b.discharge_calls == [50]

    for _ in range(20):
        _prime_discharge(manager, [a, b])
        await manager.power_discharge(900)

    assert a.discharge_calls[-1] > 0
    assert b.discharge_calls[-1] > 0
