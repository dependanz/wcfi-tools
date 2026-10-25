"""Preview geometry must keep capture and input on exactly the same rectangle."""

import pytest

pytest.importorskip("tkinter", reason="Monitor viewer requires the optional Tk runtime")

from wcfi_tools.monitor_control.app import fit_preview, overlaps


@pytest.mark.parametrize(
    ("source", "available", "expected"),
    [
        ((1920, 1080), (800, 600), (0, 75, 800, 450)),
        ((1080, 1920), (800, 600), (231, 0, 337, 600)),
        ((1920, 1080), (1600, 1000), (0, 50, 1600, 900)),
        ((1080, 1920), (1600, 1200), (462, 0, 675, 1200)),
        ((3840, 2160), (3200, 1800), (0, 0, 3200, 1800)),
        ((800, 600), (1600, 1200), (400, 300, 800, 600)),
        ((800, 600), (800, 600), (0, 0, 800, 600)),
        ((1, 1), (1, 1), (0, 0, 1, 1)),
    ],
)
def test_preview_uses_available_pixels_without_upscaling(source, available, expected):
    assert fit_preview(source, available) == expected


@pytest.mark.parametrize("source", [(3840, 2160), (2160, 3840), (3440, 1440), (1280, 1024)])
@pytest.mark.parametrize("available", [(319, 177), (1001, 777), (1920, 1080)])
def test_preview_stays_inside_canvas_and_preserves_shape(source, available):
    left, top, width, height = fit_preview(source, available)
    assert 0 <= left < available[0]
    assert 0 <= top < available[1]
    assert 1 <= width <= min(available[0], source[0])
    assert 1 <= height <= min(available[1], source[1])
    # Odd remainders belong to the right/bottom letterbox, at most one pixel.
    assert abs(left - (available[0] - left - width)) <= 1
    assert abs(top - (available[1] - top - height)) <= 1
    # Flooring both dimensions permits less than one pixel of error per axis.
    assert abs(width * source[1] - height * source[0]) < sum(source)


@pytest.mark.parametrize(
    ("source", "available"),
    [((0, 1080), (800, 600)), ((1920, -1), (800, 600)),
     ((1920, 1080), (0, 600)), ((1920, 1080), (800, -1))],
)
def test_preview_rejects_missing_dimensions(source, available):
    with pytest.raises(ValueError, match="positive"):
        fit_preview(source, available)


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ((0, 0, 1920, 1080), (1920, 0, 1920, 1080), False),
        ((0, 0, 1920, 1080), (0, 1080, 1920, 1080), False),
        ((0, 0, 1920, 1080), (1920, 1080, 1920, 1080), False),
        ((-1920, 0, 1920, 1080), (0, 0, 1920, 1080), False),
        ((-1920, -1080, 1920, 1080), (-200, -100, 800, 600), True),
        ((0, 0, 1920, 1080), (1919, 1079, 800, 600), True),
        ((0, 0, 1920, 1080), (200, 200, 800, 600), True),
        ((200, 200, 800, 600), (200, 200, 800, 600), True),
        ((0, 0, 800, 600), (1000, 1000, 800, 600), False),
    ],
)
def test_overlap_handles_negative_monitors_and_touching_edges(first, second, expected):
    assert overlaps(first, second) is expected
    assert overlaps(second, first) is expected
