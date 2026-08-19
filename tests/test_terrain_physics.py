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


def test_wall_bounce_reverses_velocity(world):
    """A throw into a side wall must rebound inward. (Old code double-negated the
    bounce and left the pet glued to / pushing through the wall forever.)"""
    phys, vp = world
    geo = QApplication.primaryScreen().availableGeometry()
    vp.x, vp.y = int(geo.left()), int(geo.top()) + 50     # high up, against the left wall
    phys.apply_impulse(vx_px_s=-300.0, vy_px_s=10.0)      # hard push straight into the wall
    assert phys.falling
    for _ in range(4):
        phys._tick()
    assert vp.x > int(geo.left()), "pet must rebound away from the wall it hit"


def test_upward_flick_clamps_at_screen_top_and_lands(world):
    """An up-flick from near the screen top used to fling the pet off-display, where
    it fell back slowly (and re-logged every tick). Now flight is clamped to the
    work area: it bounces off the top edge and lands on-screen."""
    phys, vp = world
    geo = QApplication.primaryScreen().availableGeometry()
    vp.x, vp.y = int(geo.left()) + 300, int(geo.top())    # dragged up to the very top
    landed: list[tuple[int, int]] = []
    phys.landed.connect(lambda x, y: landed.append((x, y)))
    min_y = float("inf")
    phys.apply_impulse(vx_px_s=-60.0, vy_px_s=-2000.0)    # hard throw straight up
    assert phys.falling
    ticks = 0
    while not landed and ticks < 600:
        phys._tick()
        min_y = min(min_y, vp.y)
        ticks += 1
    assert landed, "pet never landed after an upward flick"
    assert int(min_y) >= int(geo.top()), "pet left the screen top while airborne"


# -- P2: window-standing settle guarantees (Shijima cross-ref pass) -------------------

def _fake_window(x: int, y: int, w: int = 400, hgt: int = 300):
    return {"hwnd": 1234, "left": x, "top": y, "right": x + w, "bottom": y + hgt,
            "title": "Fake Editor - main.py"}


def test_drop_settles_on_window_top(world, monkeypatch):
    """A drop whose path crosses a window top must land and rest ON the window
    (standee recorded), not pass through to the taskbar."""
    phys, vp = world
    geo = QApplication.primaryScreen().availableGeometry()
    win_y = int(geo.top()) + 200
    monkeypatch.setattr(phys, "_get_visible_windows", lambda: [_fake_window(int(geo.left()), win_y)])
    phys._stand_on_windows = True

    vp.x = int(geo.left()) + 150            # horizontally inside the fake window
    vp.y = win_y - 300                       # dropped well above the window top
    landed: list[tuple[int, int]] = []
    falls: list[int] = []
    phys.landed.connect(lambda x, y: landed.append((x, y)))
    phys.falling_started.connect(lambda: falls.append(1))

    ticks = 0
    while not landed and ticks < 600:
        phys._tick()
        ticks += 1
    assert landed, "pet never landed on the window"
    assert abs((landed[-1][1] + vp.h) - win_y) <= 2, "feet must rest on the window top"
    assert phys._standee is not None and abs(phys._standee["top"] - win_y) <= 1


def test_perched_on_window_stays_grounded(world, monkeypatch):
    """Regression: a pet resting on a window top used to re-arm 'floating above floor'
    every tick (floor-only check), logging falling at a fixed x and never settling.
    The any-surface check must keep it grounded with no fall ever starting."""
    phys, vp = world
    geo = QApplication.primaryScreen().availableGeometry()
    win_y = int(geo.top()) + 250
    monkeypatch.setattr(phys, "_get_visible_windows", lambda: [_fake_window(int(geo.left()), win_y)])
    phys._stand_on_windows = True

    vp.x = int(geo.left()) + 150            # above the window's horizontal span
    vp.y = win_y - vp.h                     # feet exactly on the window top
    pos0 = (vp.x, vp.y)
    falls: list[int] = []
    phys.falling_started.connect(lambda: falls.append(1))

    for _ in range(40):                     # well past a re-arm cycle
        phys._tick()
    assert not phys.falling, "resting on a window must not start a fall"
    assert falls == [], f"spurious falling episodes while perched: {len(falls)}"
    assert (vp.x, vp.y) == pos0, "perched pet must not drift or drop"


def test_walked_off_window_rearms_fall(world, monkeypatch):
    """B1 regression: a pet that walks off the window it landed on must re-arm a
    real fall (and settle on the floor) instead of hovering at window height."""
    phys, vp = world
    geo = QApplication.primaryScreen().availableGeometry()
    win_x, win_y = int(geo.left()) + 100, int(geo.top()) + 250
    win_w = 300
    monkeypatch.setattr(phys, "_get_visible_windows",
                        lambda: [_fake_window(win_x, win_y, w=win_w)])
    phys._stand_on_windows = True

    # land ON the window top, center within its span
    vp.x = win_x + win_w // 2
    vp.y = win_y - vp.h
    phys._standee = {"top": win_y, "hwnd": 1234, "title": "Fake",
                     "left": win_x, "right": win_x + win_w}
    falls: list[int] = []
    phys.falling_started.connect(lambda: falls.append(1))
    for _ in range(5):
        phys._tick()
    assert not phys.falling and falls == [], "perched in-span pet must stay grounded"

    # walk off the right edge → feet center leaves the window span
    vp.x = win_x + win_w + 100
    landed: list[tuple[int, int]] = []
    phys.landed.connect(lambda x, y: landed.append((x, y)))
    ticks = 0
    while not phys.falling and ticks < 20:
        phys._tick()
        ticks += 1
    assert phys.falling, "pet walked off its window but never re-armed a fall"

    ticks = 0
    while phys.falling and ticks < 600:
        phys._tick()
        ticks += 1
    assert landed, "pet never settled after walking off its window"
    assert abs((landed[-1][1] + vp.h) - _floor()) <= 2, "must settle onto the work-area floor"


def test_landing_zeroes_horizontal_velocity(world, monkeypatch):
    """B2 regression: a landing must zero _vx so a later fall (e.g. standee loss)
    doesn't side-launch the pet with the stale residual flick velocity."""
    phys, vp = world
    geo = QApplication.primaryScreen().availableGeometry()
    monkeypatch.setattr(phys, "_get_visible_windows", lambda: [])
    phys._stand_on_windows = True

    vp.x, vp.y = int(geo.left()) + 200, int(geo.top()) + 50
    phys.apply_impulse(vx_px_s=2000.0, vy_px_s=-100.0)   # strong horizontal flick
    assert phys._vx != 0.0 and phys.falling

    ticks = 0
    while phys.falling and ticks < 600:
        phys._tick()
        ticks += 1
    assert not phys.falling, "pet never landed"
    assert phys._vx == 0.0, f"landing must zero _vx, got {phys._vx}"

    # force a fall from the same grounded spot (standee loss path) — no horizontal kick
    phys._falling = True
    phys._in_flick = False
    x0 = vp.x
    for _ in range(3):
        phys._tick()
    assert abs(vp.x - x0) <= 1, f"fall after landing must not side-launch: x {x0} -> {vp.x}"


def test_standee_loss_starts_a_real_fall_and_settles(world, monkeypatch):
    """Regression: the standee liveness probe once read .get('rect') instead of 'hwnd',
    and on loss it only emitted falling_started without setting _falling — so the pet
    could sit mid-air forever. Loss must start a genuine fall that settles."""
    import vaultsprite.terrain_physics as tp

    class _Win32Dummy:                       # enables the win32-gated liveness block on Linux
        pass

    phys, vp = world
    geo = QApplication.primaryScreen().availableGeometry()
    monkeypatch.setattr(tp, "_win32gui", _Win32Dummy())   # module-level gate for the re-check

    win_y = int(geo.top()) + 250
    wins = [_fake_window(int(geo.left()), win_y)]
    monkeypatch.setattr(phys, "_get_visible_windows", lambda: list(wins))
    phys._stand_on_windows = True
    vp.x = int(geo.left()) + 150
    vp.y = win_y - vp.h                     # perched on the (doomed) window top
    phys._standee = {"top": win_y, "hwnd": 999, "title": "Doomed window"}

    # liveness probe says the window is gone; sweep finds no windows either
    monkeypatch.setattr(phys, "_check_standee_alive", lambda: False)

    landed: list[tuple[int, int]] = []
    phys.landed.connect(lambda x, y: landed.append((x, y)))
    for _ in range(15):                     # force the 15-tick liveness re-check
        wins.clear()                         # window vanished too → nothing to stand on
        phys._tick()
    assert phys.falling, "standee loss must immediately start a real fall"

    ticks = 0
    while not landed and ticks < 600:
        phys._tick()
        ticks += 1
    assert landed, "pet never settled after its window vanished"
    assert abs((landed[-1][1] + vp.h) - _floor()) <= 2, "must settle onto the work-area floor"


def test_no_stuck_state_after_windows_appear_and_vanish(world, monkeypatch):
    """End-to-end settle guarantee (Shijima tick-ladder spirit): a long run where tracked
    windows appear and disappear must never leave the pet airborne-at-rest; it always
    ends grounded — on the floor or on whatever surface is there."""
    phys, vp = world
    geo = QApplication.primaryScreen().availableGeometry()
    win_y = int(geo.top()) + 250
    wins: list[dict] = []
    monkeypatch.setattr(phys, "_get_visible_windows", lambda: list(wins))
    phys._stand_on_windows = True

    vp.x = int(geo.left()) + 150
    landed: list[tuple[int, int]] = []
    phys.landed.connect(lambda x, y: landed.append((x, y)))

    def run_to_settle(limit=600):
        ticks = 0
        while (phys.falling or vp.y + vp.h < _floor() - 2) and ticks < limit:
            phys._tick()
            ticks += 1
        assert not phys.falling, "still falling after a full budget → stuck"

    # phase A: plain drop with no windows → settles on the work-area floor
    vp.y = win_y - 400
    run_to_settle()
    assert abs((vp.y + vp.h) - _floor()) <= 3, "no-window drop must settle on the floor"

    # phase B: a window appears; re-dropping from above it must perch ON the window top
    wins.append(_fake_window(int(geo.left()), win_y))
    vp.y = win_y - 400
    run_to_settle()
    assert abs((vp.y + vp.h) - win_y) <= 2, "drop over a window must land on its top"
    pos_perched = (vp.x, vp.y)
    for _ in range(30):                      # rest while perched: no spurious re-fall
        phys._tick()
    assert not phys.falling and (vp.x, vp.y) == pos_perched

    # phase C: the window vanishes → standee check finds it dead → real fall to floor
    import vaultsprite.terrain_physics as tp
    monkeypatch.setattr(tp, "_win32gui", type("_Win32Dummy", (), {}))
    monkeypatch.setattr(phys, "_check_standee_alive", lambda: False)
    wins.clear()
    for _ in range(15):
        phys._tick()
    run_to_settle()
    assert abs((vp.y + vp.h) - _floor()) <= 3, "after windows vanish the pet must settle on the floor"

