"""ContextDetector: pure classification + poll loop with an injected probe."""
from __future__ import annotations

import time

import pytest

from tests.conftest import FakeConfig, spin
from vaultsprite.context_detector import (CONTEXT_PLAY, CONTEXT_WORK,
                                          ContextDetector)


@pytest.fixture()
def titles():
    """Mutable probe backing: the test sets `titles[0]` to steer classification."""
    return [""]


@pytest.fixture()
def detector(qapp, titles):
    cfg = FakeConfig({"context.poll_ms": 30})   # tight cadence for tests
    det = ContextDetector(cfg, probe=lambda: titles[0])
    yield det
    det.stop()


# -- pure classification boundaries -------------------------------------------------
@pytest.mark.parametrize("title,expected", [
    ("Visual Studio Code - main.py", CONTEXT_WORK),     # outline example
    ("Ubuntu - /home/u/term  —  Terminal", CONTEXT_WORK),
    ("Obsidian - VaultSprite.md", CONTEXT_WORK),
    ("Notepad++ [test.txt*]", CONTEXT_WORK),            # notepad++ is a work keyword
    ("Steam - Store", CONTEXT_PLAY),                    # outline example
    ("Google Chrome - youtube.com", CONTEXT_PLAY),      # play wins (no work keyword)
    # whole-word matching: "word"/"excel" keywords must NOT fire inside other words —
    # the old raw-substring match made e.g. "The World of Tanks" count as WORK
    ("The World of Tanks - Battle", None),              # 'world' contains 'word'? no → not a work hit
    ("Microsoft Word - doc.docx", CONTEXT_WORK),        # real 'Word' still classifies
])
def test_classify_boundaries(detector, title, expected):
    assert detector.classify(title) == expected


def test_classify_empty_and_unknown(detector):
    assert detector.classify("") is None            # unknown → no change (keep last)
    assert detector.classify("Some Random App") is None


# -- context decay: stop sticking to the last bucket forever ("always WORK" fix) -----

@pytest.fixture()
def short_decay(qapp, titles):
    """Detector that decays after just 2 unknown polls (instead of ~6 / 30s)."""
    cfg = FakeConfig({"context.poll_ms": 30, "context.unknown_decay_polls": 2})
    det = ContextDetector(cfg, probe=lambda: titles[0])
    yield det
    det.stop()


def test_unknown_context_decays_to_unknown(short_decay, titles):
    seen = []
    short_decay.context_changed.connect(lambda c: seen.append(c))

    titles[0] = "PyCharm - project"
    short_decay.poll_once()
    assert seen == [CONTEXT_WORK]

    # 1 unknown poll: still holds WORK (grace for transient/short-lived titles)
    titles[0] = "Some Random App"
    short_decay.poll_once()
    assert seen == [CONTEXT_WORK], "single unknown poll must not flip the bucket yet"

    # 2nd consecutive unknown poll → decay out of the stuck bucket
    short_decay.poll_once()
    assert seen == [CONTEXT_WORK, "UNKNOWN"], f"expected decay after N polls; saw {seen}"


def test_work_title_resets_unknown_streak(short_decay, titles):
    seen = []
    short_decay.context_changed.connect(lambda c: seen.append(c))
    titles[0] = "PyCharm - project"
    short_decay.poll_once()

    for _ in range(5):                              # unknown / work interleaving
        titles[0] = "Random App X"
        short_decay.poll_once()                     # streak=1, holds WORK
        titles[0] = "PyCharm - other.py"
        short_decay.poll_once()                     # real match resets the streak

    assert seen == [CONTEXT_WORK], \
        f"a real work title between unknown polls must never decay to UNKNOWN: {seen}"


def test_empty_titles_also_count_as_unknown_polls(short_decay, titles):
    """A blank probe (e.g. minimized/edge windows) is an 'unknown' poll too."""
    seen = []
    short_decay.context_changed.connect(lambda c: seen.append(c))
    titles[0] = "Steam - Store"
    short_decay.poll_once()
    assert seen == [CONTEXT_PLAY]

    for _ in range(3):                              # blank foregrounds in a row
        titles[0] = ""
        short_decay.poll_once()
    assert seen[-1] == "UNKNOWN", f"blank polls must decay the bucket: {seen}"


# -- poll loop: signal only on real changes -------------------------------------------
def test_signal_fires_on_change_only(detector, titles, qapp):
    seen = []
    detector.context_changed.connect(lambda c: seen.append(c))

    titles[0] = "Steam - Store"
    detector.poll_once()
    assert seen == [CONTEXT_PLAY]

    titles[0] = "Chrome - some page"                # still play (chrome keyword)
    detector.poll_once()
    assert seen == [CONTEXT_PLAY], "no re-emit within the same bucket"

    titles[0] = "Some Random App"                   # unknown → keep last-known PLAY
    detector.poll_once()
    assert seen == [CONTEXT_PLAY] and detector.current_context == CONTEXT_PLAY

    titles[0] = "PyCharm - project"
    detector.poll_once()
    assert seen == [CONTEXT_PLAY, CONTEXT_WORK]


def test_polling_thread_runs_and_stops(detector, titles, qapp):
    seen = []
    detector.context_changed.connect(lambda c: seen.append(c))
    detector.start()
    assert spin(qapp, lambda: len(seen) == 0 or True, 0.2)   # give the thread a beat

    titles[0] = "Discord - Home"
    assert spin(qapp, lambda: seen and seen[-1] == CONTEXT_PLAY, timeout_s=3.0), \
        f"thread never classified; seen={seen}"

    detector.stop()
    time.sleep(0.1)
    snapshots = list(seen)
    for _ in range(15):                             # thread must be dead: no more polls
        time.sleep(0.03)
    assert len(seen) == len(snapshots)


def test_linux_no_pwc_still_degrades(qapp):
    """default_title_probe never raises; is_available reflects platform reality."""
    from vaultsprite.context_detector import default_title_probe, _pwc
    raw = default_title_probe()                     # '' on Linux (no probe), a tuple on Windows
    assert isinstance(raw, (str, tuple))
    if _pwc is None:
        det = ContextDetector(FakeConfig())
        assert not det.is_available()


# -- C3: app-name channel wins over title keywords ---------------------------------------
def test_app_name_bucket_overrides_title(qapp):
    cfg = FakeConfig({
        "context.work_apps": ["code", "msedge.exe"],   # .exe tolerated both ways in config
        "context.play_apps": ["chrome"],
    })
    det = ContextDetector(cfg, probe=lambda: ("Chrome — Important Docs Report", "chrome.exe"))
    assert det.poll_once() == CONTEXT_PLAY            # chrome.exe wins over a 'docs' tab title

    det2 = ContextDetector(FakeConfig(
        {"context.work_apps": ["code"], "context.play_apps": []}),
        probe=lambda: ("Visual Studio Code - main.py", ""))   # no app name → title channel still works
    assert det2.poll_once() == CONTEXT_WORK


def test_app_name_no_match_falls_back_to_title(qapp):
    """Unbucketed exe + keyword-matching title still classifies by title."""
    cfg = FakeConfig({"context.work_apps": ["code"], "context.play_apps": []})
    det = ContextDetector(cfg, probe=lambda: ("PyCharm - project", "notepad2.exe"))
    assert det.poll_once() == CONTEXT_WORK


# -- C3: self-window filter — our own overlay never classifies ----------------------------
def test_own_overlay_window_is_ignored(qapp):
    cfg = FakeConfig({"context.unknown_decay_polls": 2})
    box = [None]          # probe result slot, steered per-poll like `titles`
    det = ContextDetector(cfg, probe=lambda: box[0])
    det.set_overlay_winid(12345)

    seen = []
    det.context_changed.connect(lambda c: seen.append(c))

    det._maybe_change("Steam - Store", "")           # baseline bucket via the normal path
    assert seen == [CONTEXT_PLAY]

    box[0] = (12345, "Our Pet Overlay Title", "")     # WE are the foreground window
    det.poll_once()                                  # must be a no-op poll (None probe result)
    assert seen == [CONTEXT_PLAY], f"own overlay leaked into classification: {seen}"

    box[0] = (99999, "Steam - Store", "")            # someone else → normal handling resumes
    det.poll_once()
    assert seen == [CONTEXT_PLAY]                    # same bucket, no re-emit; no UNKNOWN decay either


# -- C3: probe-shape normalization ----------------------------------------------------------
def test_probe_shapes_normalize(qapp):
    from vaultsprite.context_detector import ContextDetector as CD

    # legacy string probes (all pre-existing tests), bare "", 2-tuple, Windows 3-tuple, junk
    assert CD._read_probe_context("Bare title") == ("Bare title", "")
    assert CD._read_probe_context("") == ("", "")
    assert CD._read_probe_context(("Some App", "someapp.exe")) == ("Some App", "someapp.exe")
    assert CD._read_probe_context((42, "Win Title", "win.exe")) == ("Win Title", "win.exe")
    assert CD._read_probe_context(None) == ("", "")


# -- A4: teardown race — a stuck probe must not let stop/start double-spawn the poller ------
def _wait_thread_alive(det, qapp_, timeout=2.0):
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        t = det._thread
        if t is not None and t.is_alive():
            return True
        qapp_.processEvents()
        _t.sleep(0.01)
    return False


def test_stop_with_hung_probe_keeps_reference(qapp):
    """stop() joins for 2s; a probe stuck past that must NOT have its reference nulled,
    or start() would spawn a SECOND poller on top of the stuck one."""
    import threading as _threading

    released = _threading.Event()

    def blocking_probe():
        assert not released.wait(timeout=5.0), "probe never unblocked"
        return ""

    det = ContextDetector(FakeConfig({"context.poll_ms": 30}), probe=blocking_probe)
    det.start()
    assert _wait_thread_alive(det, qapp), "poller thread never came alive"
    # the thread is inside blocking_probe(); stop()'s join times out…
    det.stop()
    assert det._thread is not None, \
        "stuck-thread reference dropped while still alive → double-spawn possible"
    released.set()                          # let it finish; stop_event set → loop exits
    time.sleep(0.2)


def test_start_refuses_second_poller_while_first_alive(qapp):
    import threading as _threading

    stuck = _threading.Event()

    def blocking_probe():
        assert not stuck.wait(timeout=5.0), "probe never unblocked"
        return ""

    det = ContextDetector(FakeConfig({"context.poll_ms": 30}), probe=blocking_probe)
    det.start()
    assert _wait_thread_alive(det, qapp), "poller thread never came alive"
    first = det._thread

    det.start()                             # must be a no-op: guard sees the live thread
    time.sleep(0.25)
    pollers = [t for t in _threading.enumerate() if t.name == "context-detector"]
    assert len(pollers) == 1, f"expected exactly one poller, found {len(pollers)}"

    stuck.set(); time.sleep(0.2)            # unstick + let the thread exit its loop
    det.stop()


def test_emit_context_silenced_after_stop(qapp):
    """A change detected mid-teardown must not emit after stop()."""
    cfg = FakeConfig({"context.poll_ms": 30})
    box = ["Steam - Store"]
    det = ContextDetector(cfg, probe=lambda: box[0])
    seen = []
    det.context_changed.connect(lambda c: seen.append(c))

    det.stop()                                             # stop first…
    det._maybe_change("PyCharm - x")                       # …then a stray poll tries to report
    assert seen == [], f"emitted after stop(): {seen}"
