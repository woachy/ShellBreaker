"""Render app-icon concepts for the 便捷解压 V2 proposal.

This is a planning artifact only. It writes PNGs under docs/mockups/out/icons/.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "out" / "icons"
OUT.mkdir(parents=True, exist_ok=True)

WHITE = QColor("#F7FDFF")
TOP = QColor("#1290A8")
BOTTOM = QColor("#0A5A76")


def tile(p: QPainter, size: int, top: QColor = TOP, bottom: QColor = BOTTOM) -> None:
    m = size * 0.040
    r = QRectF(m, m, size - 2 * m, size - 2 * m)
    grad = QLinearGradient(r.topLeft(), r.bottomRight())
    grad.setColorAt(0.0, top)
    grad.setColorAt(1.0, bottom)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(r, size * 0.22, size * 0.22)
    # Fluent-like inner rim.
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 38), size * 0.008))
    p.drawRoundedRect(r.adjusted(size * 0.012, size * 0.012, -size * 0.012, -size * 0.012), size * 0.205, size * 0.205)
    # Soft top-left sheen.
    sheen = QLinearGradient(r.topLeft(), r.bottomLeft())
    sheen.setColorAt(0.0, QColor(255, 255, 255, 28))
    sheen.setColorAt(0.55, QColor(255, 255, 255, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(sheen)
    p.drawRoundedRect(r, size * 0.22, size * 0.22)


def draw_unpack(p: QPainter, size: int) -> None:
    """Open-top archive tray with an extraction arrow."""
    s = size
    # Extraction arrow.
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(WHITE)
    shaft = QRectF(s * 0.445, s * 0.150, s * 0.11, s * 0.235)
    p.drawRoundedRect(shaft, s * 0.02, s * 0.02)
    head = QPainterPath()
    head.moveTo(s * 0.50, s * 0.075)
    head.lineTo(s * 0.330, s * 0.270)
    head.lineTo(s * 0.670, s * 0.270)
    head.closeSubpath()
    p.drawPath(head)
    # Open tray: U-shaped stroke, open at the top.
    x0, x1 = s * 0.245, s * 0.755
    y0, y1 = s * 0.410, s * 0.735
    r = s * 0.055
    # Two short lid flaps make it read as an opened box rather than a plain tray.
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(WHITE)
    left_flap = QPainterPath()
    left_flap.moveTo(s * 0.245, s * 0.430)
    left_flap.lineTo(s * 0.140, s * 0.330)
    left_flap.lineTo(s * 0.355, s * 0.430)
    left_flap.closeSubpath()
    p.drawPath(left_flap)
    right_flap = QPainterPath()
    right_flap.moveTo(s * 0.755, s * 0.430)
    right_flap.lineTo(s * 0.860, s * 0.330)
    right_flap.lineTo(s * 0.645, s * 0.430)
    right_flap.closeSubpath()
    p.drawPath(right_flap)
    path = QPainterPath()
    path.moveTo(x0, y0)
    path.lineTo(x0, y1 - r)
    path.quadTo(x0, y1, x0 + r, y1)
    path.lineTo(x1 - r, y1)
    path.quadTo(x1, y1, x1, y1 - r)
    path.lineTo(x1, y0)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(WHITE, s * 0.060, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawPath(path)


def draw_zipper(p: QPainter, size: int) -> None:
    """Compact archive box with a zipper and pull tab."""
    s = size
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(WHITE)
    body = QRectF(s * 0.270, s * 0.230, s * 0.460, s * 0.500)
    p.drawRoundedRect(body, s * 0.055, s * 0.055)
    # Zipper track.
    p.setBrush(QColor("#0A5A76"))
    p.drawRoundedRect(QRectF(s * 0.485, s * 0.245, s * 0.036, s * 0.395), s * 0.010, s * 0.010)
    # Teeth alternate on both sides so it reads as a zipper, not a thermometer.
    for i in range(4):
        y = s * (0.270 + i * 0.088)
        p.drawRoundedRect(QRectF(s * 0.402, y, s * 0.072, s * 0.026), s * 0.010, s * 0.010)
        p.drawRoundedRect(QRectF(s * 0.526, y + s * 0.044, s * 0.072, s * 0.026), s * 0.010, s * 0.010)
    # Pull tab.
    p.setBrush(WHITE)
    p.setPen(QPen(QColor("#0A5A76"), s * 0.030))
    tab = QRectF(s * 0.448, s * 0.640, s * 0.110, s * 0.150)
    p.drawRoundedRect(tab, s * 0.035, s * 0.035)
    p.setBrush(QColor("#0A5A76"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QRectF(s * 0.482, s * 0.672, s * 0.042, s * 0.042))


def draw_layers(p: QPainter, size: int) -> None:
    """Three nested layers lifting apart, optically centered slightly low."""
    s = size
    dy = s * 0.070  # selected-icon correction: lower the mark to balance the tile
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(WHITE)
    # Bottom to top, each offset, with teal gaps.
    layers = [
        QRectF(s * 0.235, s * 0.545 + dy, s * 0.530, s * 0.125),
        QRectF(s * 0.280, s * 0.395 + dy, s * 0.500, s * 0.125),
        QRectF(s * 0.325, s * 0.245 + dy, s * 0.470, s * 0.125),
    ]
    for r in layers:
        p.drawRoundedRect(r, s * 0.045, s * 0.045)
    # Lift chevron at the right of the top layer.
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor("#0A5A76"), s * 0.040, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    chev = QPainterPath()
    chev.moveTo(s * 0.705, s * 0.215 + dy)
    chev.lineTo(s * 0.765, s * 0.155 + dy)
    chev.lineTo(s * 0.825, s * 0.215 + dy)
    p.drawPath(chev)


def render(concept: str, painter_fn, size: int = 512) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if concept != "glyph":
        tile(p, size)
    painter_fn(p, size)
    p.end()
    return pix


def main() -> int:
    app = QApplication([])
    concepts = [
        ("a_unpack", draw_unpack, "A · 开箱取出 · 备选"),
        ("b_zipper", draw_zipper, "B · 拉链解压 · 备选"),
        ("c_layers", draw_layers, "C · 层叠剥离 · 已选"),
    ]
    sheet = QPixmap(1200, 560)
    sheet.fill(QColor("#F7FAFD"))
    sp = QPainter(sheet)
    sp.setRenderHint(QPainter.RenderHint.Antialiasing)
    x = 40
    for key, fn, label in concepts:
        big = render(key, fn, 512)
        big.save(str(OUT / f"{key}.png"))
        for s in (256, 48, 32, 16):
            small = big.scaled(s, s, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            small.save(str(OUT / f"{key}_{s}.png"))
        sp.drawPixmap(x, 60, big.scaled(220, 220, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        sp.setPen(QColor("#16212B"))
        sp.setFont(QFont("Microsoft YaHei UI", 14, QFont.Weight.DemiBold))
        sp.drawText(x, 320, label)
        # Small-size row.
        for i, s in enumerate((48, 32, 16)):
            sm = big.scaled(s, s, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            sp.drawPixmap(x + i * 62, 350, sm)
        x += 380
    sp.end()
    sheet.save(str(OUT / "icon_concepts.png"))
    # Keep the mockup titlebar in sync with the selected concept.
    (ROOT / "docs" / "mockups" / "assets" / "app_icon_v2.png").write_bytes(
        (OUT / "c_layers.png").read_bytes()
    )
    print("icons:", ", ".join(k for k, _, _ in concepts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
