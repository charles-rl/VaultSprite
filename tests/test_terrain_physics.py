"""TerrainPhysics: fall sim with an injected fake viewport (no win32 needed)."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from tests.conftest import FakeConfig
from vaultsprite.terrain_physics import TerrainPhysics


class FakeViewport:
    """Stands in for the overlay window: position/move/size callbacks."""

    def __init__(self, w=96, h=96, x=100, y=40):
        self.x, self.y = x, y
        self.w, self.h = w, h
        self.move_log: list[tuple[int, int]] = []

    def position(self):
        return self.x, self.y

    def move_to(self, x, y):
        self.x, self.y = int(x), int(y)
        self.move_log.append((x, y))


@pytest.fixture()
def world(qapp):
    cfg = FakeConfig({"physics.tick_ms": 16})   # fast ticks for tests
    vp = FakeViewport()
    phys = TerrainPhysics(cfg)
    phys.set_mover(position=vp.position, move_to=vp.move_to, pet_size=lambda: (vp.w, vp.h))
    yield phys, vp
    phys.stop()


def _floor():
    return QApplication.primaryScreen().availableGeometry().bottom()


def test_floor_line_is_work_area_bottom(world):
    phys, _ = world
    floor = phys.get_floor_line(0)
    assert floor == _floor()
    assert isinstance(floor, int) and floor > 100


def test_plain_drop_falls_and_lands_once(world):
    phys, vp = world
    landed: list[tuple[int, int]] = []
    falls_started: list[int] = []
    phys.landed.connect(lambda x, y: landed.append((x, y)))
    phys.falling_started.connect(lambda: falls_started.append(1))

    vp.y = 50                                   # drop from mid-air above the floor
    assert not phys.falling
    for _ in range(2):
        phys._tick()
    assert phys.falling                          # gravity re-arm kicked in
    assert falls_started == [1]

    ticks = 0
    while phys.falling and ticks < 600:          # run to rest
        phys._tick()
        ticks += 1
    assert landed, "pet never emitted landed"
    lx, ly = landed[-1]
    assert abs((ly + vp.h) - _floor()) <= 2      # snapped onto the work-area line


def test_fall_capped_at_terminal_velocity(world):
    phys, vp = world
    vp.y = 0                                     # start very high in the work area
    speeds: list[float] = []
    ticks = 0
    while ticks < 300 and (phys.falling or not speeds):
        phys._tick()
        if phys.falling:
            speeds.append(abs(phys._vy))
        ticks += 1
        if not phys.falling and speeds:
            break
    assert speeds, "never entered a fall"
    # hard cap from config (fall_terminal=8.0 px/tick): never exceed +eps
    assert max(speeds) <= 8.0 + 1e-6


def test_flick_impulse_moves_horizontally(world):
    phys, vp = world
    geo = QApplication.primaryScreen().availableGeometry()
    vp.x, vp.y = int(geo.left()) + 200, 30      # start high enough to stay airborne
    started: list[int] = []
    phys.falling_started.connect(lambda: started.append(1))
    phys.apply_impulse(vx_px_s=900.0, vy_px_s=-200.0)   # strong rightward flick
    assert started == [1] and phys.falling
    x0 = vp.x
    ticks = 0
    while phys.falling and ticks < 600:
        phys._tick()
        ticks += 1
    assert vp.x > x0                             # horizontal travel happened


def test_impulse_ignored_while_disabled(world):
    phys, _vp = world
    phys.enable(False)
    phys.apply_impulse(vx_px_s=900.0, vy_px_s=-200.0)
    assert not phys.falling                      # paused by a drag → no fall


def test_release_slow_is_plain_drop(world):
    phys, vp = world
    geo = QApplication.primaryScreen().availableGeometry()
    vp.x, vp.y = int(geo.left()) + 100, 30
    landed: list[tuple[int, int]] = []
    phys.landed.connect(lambda x, y: landed.append((x, y)))
    phys.release(vx_px_s=4.0, vy_px_s=-2.0)      # below impulse threshold
    ticks = 0
    while not landed and ticks < 600:            # should still settle naturally
        if phys.falling or vp.y + vp.h < _floor() - 2:
            phys._tick()
        else:
            break
        ticks += 1
    assert landed, "slow release never settled"
