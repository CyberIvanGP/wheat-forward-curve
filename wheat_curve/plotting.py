"""Static PNG rendering. No browser, server, network or plotting runtime."""
from datetime import date, timedelta
from decimal import Decimal, localcontext
import math
import os
from pathlib import Path

from .domain.curve import interpolate
from .domain.dates import add_months
from .calculator import TENORS


def _line(points, as_of):
    base = date.fromisoformat(as_of)
    curve = {p["tenor"]: Decimal(p["price"]) for p in points if p["price"] is not None}
    start = date.fromisoformat(points[0]["maturity_date"])
    end = date.fromisoformat(points[-1]["maturity_date"])
    dates, values = [], []
    with localcontext() as context:
        context.prec = 28
        for i in range((end - start).days + 1):
            day = start + timedelta(days=i)
            value = interpolate(curve, base, day, TENORS)
            dates.append(day)
            values.append(float(value) if value is not None else None)
    return dates, values


def _font(size):
    from PIL import ImageFont
    candidates = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
                  Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                  Path("/System/Library/Fonts/Supplemental/Arial.ttf")]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def save_images(result: dict, directory: Path) -> list[Path]:
    from PIL import Image, ImageDraw

    previous, current = result["previous_curve"], result["curve"]
    old_x, old_y = _line(previous, result["previous_date"])
    new_x, new_y = _line(current, result["calculation_date"])
    start, end = min(old_x[0], new_x[0]), max(old_x[-1], new_x[-1])
    all_prices = [v for v in old_y + new_y if v is not None]
    lo, hi = (min(all_prices), max(all_prices)) if all_prices else (0, 1)
    margin = max((hi - lo) * .15, hi * .002, .01)
    lo, hi = lo - margin, hi + margin
    rough = (hi - lo) / 6
    power = 10 ** math.floor(math.log10(rough))
    step = next(v * power for v in (1, 2, 2.5, 5, 10) if v * power >= rough)
    lo, hi = math.floor(lo / step) * step, math.ceil(hi / step) * step
    scale, width, height = 2, 1400, 800
    left, right, top, bottom = 125, 1335, 165, 625
    fonts = {size: _font(size * scale) for size in (13, 14, 16, 27)}

    def xy(day, value):
        return ((left + (day - start).days / (end - start).days * (right - left)) * scale,
                (bottom - (value - lo) / (hi - lo) * (bottom - top)) * scale)

    paths = []
    for filename, title, show_old, show_new in (
        ("comparison.png", "Форвардная кривая пшеницы: вчера и сегодня", True, True),
        ("yesterday.png", "Форвардная кривая пшеницы: вчера", True, False),
        ("today.png", "Форвардная кривая пшеницы: сегодня", False, True),
    ):
        image = Image.new("RGB", (width * scale, height * scale), "#ffffff")
        draw = ImageDraw.Draw(image)

        def text(x, y, value, size=14, color="#475569", anchor="lt"):
            draw.text((x * scale, y * scale), value, font=fonts[size], fill=color, anchor=anchor)

        def line(coords, color, thickness=1):
            draw.line([(x * scale, y * scale) for x, y in coords], fill=color, width=thickness * scale)

        text(65, 40, title, 27, "#0f172a")
        text(65, 86, f"Вчера: {result['previous_date']}    Сегодня: {result['calculation_date']}", 16)
        text(left, 132, "Цена, руб./т", 14)
        for i in range(round((hi - lo) / step) + 1):
            value = lo + i * step
            y = xy(start, value)[1] / scale
            line([(left, y), (right, y)], "#e2e8f0")
            label = f"{value:,.0f}".replace(",", " ") if step >= 1 else f"{value:.3g}"
            text(left - 16, y, label, anchor="rm")
        tick = date(start.year, ((start.month - 1) // 3) * 3 + 1, 1)
        while tick <= end:
            if tick >= start:
                x = xy(tick, lo)[0] / scale
                line([(x, top), (x, bottom)], "#f1f5f9")
                text(x, bottom + 18, tick.strftime("%m.%Y"), 13, anchor="mt")
            tick = add_months(tick, 3)
        line([(left, top), (left, bottom), (right, bottom)], "#cbd5e1")
        text(right, bottom + 48, "Дата экспирации", 14, anchor="rt")

        def curve(xs, ys, color, dashed=False):
            previous_pixel = None
            for i, (day, value) in enumerate(zip(xs, ys)):
                if value is None:
                    previous_pixel = None
                    continue
                pixel = xy(day, value)
                if previous_pixel is not None and (not dashed or i % 10 < 6):
                    draw.line([previous_pixel, pixel], fill=color, width=3 * scale)
                previous_pixel = pixel

        if show_old:
            curve(old_x, old_y, "#94a3b8", dashed=True)
        if show_new:
            curve(new_x, new_y, "#2563eb")
            for point in current:
                if point["price"] is None:
                    continue
                x, y = xy(date.fromisoformat(point["maturity_date"]), float(point["price"]))
                radius = 4 * scale
                if point["source"] == "FALLBACK_PREVIOUS_DAY":
                    draw.line([(x-radius, y-radius), (x+radius, y+radius)], fill="#d97706", width=2*scale)
                    draw.line([(x-radius, y+radius), (x+radius, y-radius)], fill="#d97706", width=2*scale)
                else:
                    draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill="#2563eb", outline="white", width=scale)
        if show_old:
            line([(left, 713), (left + 32, 713)], "#94a3b8", 3)
            text(left + 43, 704, "Вчерашняя кривая", 14)
        if show_new:
            line([(400, 713), (432, 713)], "#2563eb", 3)
            text(443, 704, "Сегодняшняя кривая", 14)
            text(715, 704, "● По данным дня", 14, "#2563eb")
            text(970, 704, "× Перенос, не измерение", 14, "#b45309")
        text(left, 755, "Между соседними узлами: log-linear. Без экстраполяции и соединения пропусков.", 13, "#64748b")
        path = directory / filename
        image.resize((width, height), Image.Resampling.LANCZOS).save(path, "PNG")
        paths.append(path)
    return paths
