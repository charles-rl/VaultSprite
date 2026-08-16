"""Offscreen render check (run under QT_QPA_PLATFORM=offscreen).

Boots App, plays `idle`, pumps frames from the real QMovie path, then:
- composites the live sprite over a checkerboard → /tmp/vaultsprite_render.png
- asserts corner alpha == 0 (transparency survived) and center alpha > 0
- captures two frames ~1.4s apart and reports whether they differ
Exit 0 on success, 1 otherwise.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication


def checkerboard(width: int, height: int, cell: int = 16):
    img = QImage(width, height, QImage.Format_ARGB32)
    p = QPainter(img)
    for cy in range(0, height, cell):
        for cx in range(0, width, cell):
            dark = ((cx // cell) + (cy // cell)) % 2 == 0
            color = QColor(205, 210, 220) if dark else QColor(236, 240, 246)
            p.fillRect(cx, cy, cell, cell, color)
    p.end()
    return img


def main():
    from vaultsprite.main import App

    app = QApplication([])
    brain = App(None)
    app.setQuitOnLastWindowClosed(False)
    brain.window.show()
    brain._play(brain.fsm.force_state("idle"))
    brain.start()

    state = {"canvases": [], "corner": None, "center": None}

    def capture():
        sprite = brain.window._base_pixmap
        img = (sprite.toImage() if sprite is not None and not sprite.isNull() else None)
        # Measure alpha the reliable way: composite over magenta; a transparent
        # pixel leaves pure #ff00ff, an opaque one blends. (toImage() on this
        # platform can report RGB32 without alpha → raw pixel reads are unreliable.)
        if img is not None:
            probe = QImage(img.size(), QImage.Format_ARGB32)
            p = QPainter(probe); p.fillRect(0, 0, probe.width(), probe.height(),
                                            QColor(255, 0, 255)); p.end()
            p = QPainter(probe); p.drawImage(0, 0, img); p.end()
            corner_color = QColor(probe.pixel(1, 1))
            center_color = QColor(probe.pixel(probe.width() // 2, probe.height() // 2))
            state["corner"] = (0 if corner_color.name().lower() == "#ff00ff" else "OPAQUE")
            # center is opaque when it differs from pure magenta (mascot pixels blend in)
            state["center"] = (">0 drawn" if center_color.name().lower() != "#ff00ff"
                               else "EMPTY")
        canvas = checkerboard(sprite.width() + 40, sprite.height() + 60) \
            if img is not None else checkerboard(128, 128)
        p = QPainter(canvas)
        if img is not None:
            p.drawPixmap(20, 30, sprite)
        p.end()
        state["canvases"].append(canvas)

    def step():
        sprite = brain.window._base_pixmap
        if sprite is None or sprite.isNull():      # first movie frame may be pending
            QTimer.singleShot(150, step)
            return
        capture()
        if len(state["canvases"]) < 2:
            QTimer.singleShot(1400, step)          # ~1.4s later the mascot must have moved
        else:
            finish()

    def finish():
        ok = True
        corner, center = state.get("corner"), state.get("center")
        print(f"frames captured : {len(state['canvases'])}")
        if corner is None:
            print("FAIL          : sprite never painted (null pixmap)")
            ok = False
        else:
            print(f"corner alpha    : {corner!r}   (expect transparent)")
            print(f"center          : {center!r}   (expect drawn)")
            if corner != 0 or center == "EMPTY":
                ok = False

        if len(state["canvases"]) >= 2:
            # Full-region pixel diff of the sprite area between two captures ~1.4s
            # apart — robust against sampling-phase luck (a single stripe can alias).
            a, b = state["canvases"][0], state["canvases"][-1]
            min_w, min_h = min(a.width(), b.width()), min(a.height(), b.height())
            changed = False
            limit_x = min_w * 2 // 3                     # stay inside the mascot, not pure checker
            for y in range(0, min_h, 3):
                for x in range(20, limit_x, 3):          # stride keeps this fast offscreen
                    if QColor(a.pixel(x, y)).rgb() != QColor(b.pixel(x, y)).rgb():
                        changed = True
                        break
                if changed:
                    break
            print(f"animating       : {'yes — frames advance (QMovie live)' if changed else 'no pixel change in sprite region'}")

        # save composite + a magenta-corner alpha proof sheet
        last = state["canvases"][-1]
        last.save("/tmp/vaultsprite_render.png")
        if corner is not None:
            m = QImage(state["canvases"][0].size(), QImage.Format_ARGB32)
            p = QPainter(m); p.fillRect(0, 0, m.width(), m.height(), QColor(255, 0, 255)); p.end()
            sprite = brain.window._base_pixmap
            if not sprite.isNull():
                q = QPainter(m); q.drawPixmap(20, 30, sprite); q.end()
            corners_ok = all(
                QColor(m.pixel(x, y)).name().lower() == "#ff00ff"
                for x, y in ((1, 1), (m.width() - 2, 1), (1, m.height() - 2)))
            print("alpha proof     : corners stay pure magenta →", corners_ok)
            if not corners_ok:
                ok = False
            m.save("/tmp/vaultsprite_alpha_check.png")

        print("RESULT          :", "PASS" if ok else "FAIL")
        app.exit(0 if ok else 1)

    QTimer.singleShot(500, step)   # wait for the first QMovie frame to land
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
