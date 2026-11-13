"""Desktop Duplication output routing without native graphics resources."""

from unittest.mock import Mock

import pytest

from wcfi_tools.monitor_control.capture import (
    Capture,
    composite_bgra,
    local_capture_region,
    monitor_identity,
)


def test_monitor_identity_uses_active_interface_not_adapter_number():
    user = Mock()
    def enum(device, index, address, flags):
        assert device == 'DISPLAY2' and flags == 1
        if index >= 2:
            return False
        address._obj.StateFlags = 1 if index == 1 else 0
        address._obj.DeviceID = 'Monitor-Interface' if index == 1 else 'Detached-Interface'
        return True
    user.EnumDisplayDevicesW.side_effect = enum
    assert monitor_identity(user, 'DISPLAY2') == 'monitor-interface'
    user.EnumDisplayDevicesW.side_effect = OSError('Not supported')
    assert monitor_identity(user, 'DISPLAY2') == ''


def test_popup_transparency_shadow_and_black_content_are_distinct():
    np = pytest.importorskip('numpy')
    background = np.full((1, 4, 4), (200, 160, 120, 255), dtype=np.uint8)
    popup = np.array([[
        (0, 0, 0, 0),       # Transparent padding: preserve parent.
        (0, 0, 0, 128),     # Half-opacity shadow: darken parent once.
        (40, 20, 10, 128),  # Premultiplied color: do not multiply it twice.
        (0, 0, 0, 255),     # Opaque black text/content: preserve black.
    ]], dtype=np.uint8)
    original = popup.copy()
    composite_bgra(background, popup)
    assert background.tolist() == [[
        [200, 160, 120, 255], [100, 80, 60, 255],
        [140, 100, 70, 255], [0, 0, 0, 255],
    ]]
    assert np.array_equal(popup, original)


def test_transparent_single_window_uses_scene_background(monkeypatch):
    np = pytest.importorskip('numpy')
    from wcfi_tools.monitor_control.window_scene import WindowLayer, WindowScene
    from wcfi_tools.monitor_control.windows import AppWindow

    capture, _, _ = backend(monkeypatch)
    capture._window_cameras = {}
    capture._scale_pixels = lambda pixels, *args: pixels.copy()
    layer = WindowLayer(AppWindow(1, 99, 'Transparent'), (0, 0, 2, 2))
    monkeypatch.setattr('wcfi_tools.monitor_control.windows.window_rect', lambda _: layer.bounds)
    camera = Mock()
    camera.grab.return_value = np.zeros((2, 2, 4), dtype=np.uint8)
    monkeypatch.setattr('wcfi_tools.monitor_control.window_capture.create_window_camera',
                        Mock(return_value=camera))
    image = capture.grab_scene(WindowScene((layer,)), *layer.bounds, 2, 2)
    assert np.all(image == (20, 12, 7, 255))


@pytest.mark.parametrize(
    "target, output, expected",
    [
        ((0, 0, 1920, 1080), (0, 0, 1920, 1080), (0, 0, 1920, 1080)),
        ((-1800, 50, 600, 500), (-1920, 0, 1920, 1080), (120, 50, 720, 550)),
        ((100, -1000, 600, 500), (0, -1080, 1920, 1080), (100, 80, 700, 580)),
        ((1900, 50, 600, 500), (0, 0, 1920, 1080), None),
        ((0, 0, 1920, 1080), (1920, 0, 1920, 1080), None),
    ],
)
def test_capture_routes_by_desktop_coordinates_not_display_order(target, output, expected):
    assert local_capture_region(target, output) == expected


def backend(monkeypatch):
    capture = object.__new__(Capture)
    capture._closed = False
    capture._camera = None
    capture._binding = None
    capture._outputs = [
        ("device-left", "output-left", (-1920, 0, 1920, 1080)),
        ("device-main", "output-main", (0, 0, 1920, 1080)),
    ]
    first, second = Mock(), Mock()
    first.grab.return_value = second.grab.return_value = None
    capture._camera_type = Mock(side_effect=[first, second])
    monkeypatch.setattr("wcfi_tools.monitor_control.capture.time.sleep", Mock())
    return capture, first, second


def test_switch_outputs_releases_previous_camera_and_translates_crop(monkeypatch):
    capture, first, second = backend(monkeypatch)
    assert capture.grab(-1800, 50, 600, 500, 300, 250) is None
    first.grab.assert_called_once_with(region=(120, 50, 720, 550), copy=True,
                                      new_frame_only=False)
    capture.grab(-1600, 50, 400, 500, 300, 250)
    assert capture._camera_type.call_count == 1
    capture.grab(100, 100, 600, 500, 300, 250)
    first.release.assert_called_once()
    assert capture._camera_type.call_args.kwargs["device"] == "device-main"
    second.grab.assert_called_once_with(region=(100, 100, 700, 600), copy=True,
                                       new_frame_only=False)


def test_offscreen_crop_cannot_reuse_last_display_pixels(monkeypatch):
    capture, _, _ = backend(monkeypatch)
    with pytest.raises(RuntimeError, match="outside"):
        capture.grab(1900, 50, 600, 500, 300, 250)
    capture._camera_type.assert_not_called()


def test_scene_keeps_popup_pixels_outside_root_and_reuses_root_camera(monkeypatch):
    np = pytest.importorskip("numpy")
    from wcfi_tools.monitor_control.window_scene import WindowLayer, WindowScene
    from wcfi_tools.monitor_control.windows import AppWindow

    capture, _, _ = backend(monkeypatch)
    capture._window_cameras = {}
    capture._scale_pixels = lambda pixels, *args: pixels.copy()
    root = WindowLayer(AppWindow(1, 99, "Root"), (-10, 10, 4, 4))
    popup = WindowLayer(AppWindow(2, 99, ""), (-7, 9, 3, 3))
    scene = WindowScene((root, popup))
    rects = {1: root.bounds, 2: popup.bounds}
    monkeypatch.setattr("wcfi_tools.monitor_control.windows.window_rect",
                        lambda window: rects[window.hwnd])
    cameras = {}
    def camera(device, hwnd, size):
        result = Mock()
        result.grab.return_value = np.full((size[1], size[0], 4), hwnd * 50, dtype=np.uint8)
        result.grab.return_value[:, :, 3] = 255
        cameras[hwnd] = result
        return result
    factory = Mock(side_effect=camera)
    monkeypatch.setattr("wcfi_tools.monitor_control.window_capture.create_window_camera", factory)
    image = capture.grab_scene(scene, *scene.bounds, 6, 5)
    assert image.shape == (5, 6, 4)
    assert list(image[0, 5]) == [100, 100, 100, 255]  # Outside root but inside the menu.
    assert list(image[1, 3]) == [100, 100, 100, 255]  # Menu covers parent in native Z order.
    assert list(image[4, 0]) == [50, 50, 50, 255]
    assert list(image[0, 0]) == [20, 12, 7, 255]  # No unrelated desktop pixels in gaps.
    popup_camera = cameras[2]
    capture.grab_scene(WindowScene((root,)), *root.bounds, 4, 4)
    assert factory.call_count == 2
    popup_camera.release.assert_called_once()
    cameras[1].release.assert_not_called()
    capture._close_window_cameras()
    cameras[1].release.assert_called_once()


def test_scene_does_not_publish_until_all_popup_pixels_are_ready(monkeypatch):
    from wcfi_tools.monitor_control.window_scene import WindowLayer, WindowScene
    from wcfi_tools.monitor_control.windows import AppWindow

    capture, _, _ = backend(monkeypatch)
    capture._window_cameras = {}
    layer = WindowLayer(AppWindow(1, 99, "App"), (0, 0, 400, 300))
    monkeypatch.setattr("wcfi_tools.monitor_control.windows.window_rect", lambda _: layer.bounds)
    camera = Mock()
    camera.grab.return_value = None
    monkeypatch.setattr("wcfi_tools.monitor_control.window_capture.create_window_camera",
                        Mock(return_value=camera))
    assert capture.grab_scene(WindowScene((layer,)), *layer.bounds, 400, 300) is None
    monkeypatch.setattr("wcfi_tools.monitor_control.windows.window_rect",
                        lambda _: (10, 10, 400, 300))
    assert capture.grab_scene(WindowScene((layer,)), *layer.bounds, 400, 300) is None
    assert camera.grab.call_count == 1


def test_resize_reuses_session_and_rejects_pixels_with_old_dimensions(monkeypatch):
    np = pytest.importorskip("numpy")
    from wcfi_tools.monitor_control.window_scene import WindowLayer, WindowScene
    from wcfi_tools.monitor_control.windows import AppWindow
    capture, _, _ = backend(monkeypatch)
    capture._window_cameras = {}
    capture._scale_pixels = lambda pixels, *args: pixels.copy()
    window = AppWindow(1, 99, "App")
    rect = [(-10, 10, 4, 4)]
    monkeypatch.setattr("wcfi_tools.monitor_control.windows.window_rect", lambda _: rect[0])
    camera = Mock()
    camera.grab.return_value = np.zeros((4, 4, 4), dtype=np.uint8)
    factory = Mock(return_value=camera)
    monkeypatch.setattr("wcfi_tools.monitor_control.window_capture.create_window_camera", factory)
    def grab():
        return capture.grab_scene(WindowScene((WindowLayer(window, rect[0]),)), *rect[0], 6, 5)
    assert grab() is not None
    rect[0] = (-8, 11, 6, 5)
    assert grab() is None
    camera.grab.return_value = np.zeros((5, 6, 4), dtype=np.uint8)
    assert grab().shape == (5, 6, 4)
    factory.assert_called_once()
    camera.release.assert_not_called()


def scene_backend(monkeypatch):
    np = pytest.importorskip("numpy")
    capture, _, _ = backend(monkeypatch)
    capture._window_cameras = {}
    capture._scale_pixels = lambda pixels, *args: pixels.copy()
    cameras, rects, devices = {}, {}, {}
    def camera(device, hwnd, size):
        devices[hwnd] = device
        result = Mock()
        def pixels(**_):
            image = np.full((rects[hwnd][3], rects[hwnd][2], 4), hwnd * 50, dtype=np.uint8)
            image[:, :, 3] = 255
            return image
        result.grab.side_effect = pixels
        cameras[hwnd] = result
        return result
    factory = Mock(side_effect=camera)
    monkeypatch.setattr("wcfi_tools.monitor_control.window_capture.create_window_camera", factory)
    monkeypatch.setattr("wcfi_tools.monitor_control.windows.window_rect",
                        lambda w: rects[w.hwnd])
    return capture, cameras, rects, devices, factory


def test_cross_display_composite_uses_each_windows_adapter_and_retains_moving_session(monkeypatch):
    from wcfi_tools.monitor_control.window_scene import WindowLayer, WindowScene
    from wcfi_tools.monitor_control.windows import AppWindow
    capture, cameras, rects, devices, factory = scene_backend(monkeypatch)
    capture._outputs = [("left", None, (-8, 0, 8, 8)), ("right", None, (0, -4, 12, 12))]
    rects.update({1: (-6, 1, 4, 4), 2: (3, -2, 3, 3)})
    def view():
        return WindowScene(tuple(WindowLayer(AppWindow(h, 99, ""), r) for h, r in rects.items()),
                           tuple(output[2] for output in capture._outputs))
    scene = view()
    pixels = capture.grab_scene(scene, *scene.bounds, 12, 8)
    assert devices == {1: "left", 2: "right"}
    assert list(pixels[0, 9]) == [100, 100, 100, 255]  # Dialog on other display.
    assert list(pixels[3, 0]) == [50, 50, 50, 255]
    assert list(pixels[0, 0]) == [20, 12, 7, 255]  # Gap does not expose desktop.
    for moved in [(-2, 1, 4, 4), (5, 1, 4, 4)]:
        rects[1] = moved
        scene = view()
        assert capture.grab_scene(scene, *scene.bounds, 12, 8) is not None
    assert factory.call_count == 2
    cameras[1].release.assert_not_called()


def test_straddling_window_masks_l_shaped_desktop_gap_and_chooses_largest_overlap(monkeypatch):
    from wcfi_tools.monitor_control.window_scene import WindowLayer, WindowScene
    from wcfi_tools.monitor_control.windows import AppWindow
    capture, _, rects, devices, _ = scene_backend(monkeypatch)
    capture._outputs = [("left", None, (-4, 0, 4, 4)), ("right", None, (0, 2, 8, 4))]
    rects[1] = (-2, 0, 7, 4)
    scene = WindowScene((WindowLayer(AppWindow(1, 99, ""), rects[1]),),
                        tuple(output[2] for output in capture._outputs))
    pixels = capture.grab_scene(scene, *scene.bounds, 7, 4)
    assert devices[1] == "right"
    assert list(pixels[0, 2]) == [20, 12, 7, 255]
    assert list(pixels[0, 1]) == [50, 50, 50, 255]
    assert list(pixels[2, 2]) == [50, 50, 50, 255]


def test_fully_offscreen_parent_does_not_block_a_visible_owned_dialog(monkeypatch):
    from wcfi_tools.monitor_control.window_scene import WindowLayer, WindowScene
    from wcfi_tools.monitor_control.windows import AppWindow
    capture, _, rects, devices, _ = scene_backend(monkeypatch)
    rects.update({1: (4000, 0, 4, 4), 2: (10, 10, 3, 3)})
    scene = WindowScene(tuple(WindowLayer(AppWindow(h, 99, ""), r) for h, r in rects.items()),
                        ((0, 0, 1920, 1080),))
    pixels = capture.grab_scene(scene, *scene.bounds, 3, 3)
    assert devices.keys() == {2}
    assert list(pixels[0, 0]) == [100, 100, 100, 255]


def test_large_multi_display_canvas_rejected_before_allocating_cameras(monkeypatch):
    from wcfi_tools.monitor_control.window_scene import WindowLayer, WindowScene
    from wcfi_tools.monitor_control.windows import AppWindow
    capture, _, rects, _, factory = scene_backend(monkeypatch)
    rects[1] = (0, 0, 100000, 100000)
    scene = WindowScene((WindowLayer(AppWindow(1, 99, ""), rects[1]),))
    with pytest.raises(ValueError, match="too large"):
        capture.grab_scene(scene, *scene.bounds, 1000, 1000)
    factory.assert_not_called()


def test_popup_transparent_margin_preserves_parent_and_clipped_scene_gap(monkeypatch):
    np = pytest.importorskip('numpy')
    from wcfi_tools.monitor_control.window_scene import WindowLayer, WindowScene
    from wcfi_tools.monitor_control.windows import AppWindow

    capture, cameras, rects, _, _ = scene_backend(monkeypatch)
    rects.update({1: (-2, 0, 4, 4), 2: (0, -1, 3, 3)})
    scene = WindowScene(tuple(WindowLayer(AppWindow(h, 99, ''), r) for h, r in rects.items()),
                        ((-2, -1, 5, 5),))
    capture.grab_scene(scene, *scene.bounds, 5, 5)
    popup = np.zeros((3, 3, 4), dtype=np.uint8)
    popup[1, 1] = (0, 0, 0, 128)
    popup[2, 1] = (0, 0, 0, 255)
    cameras[2].grab.side_effect = None
    cameras[2].grab.return_value = popup
    pixels = capture.grab_scene(scene, *scene.bounds, 5, 5)
    assert list(pixels[0, 2]) == [20, 12, 7, 255]  # Outside parent, transparent margin.
    assert list(pixels[1, 2]) == [50, 50, 50, 255]  # Parent shows through popup padding.
    assert list(pixels[1, 3]) == [25, 25, 25, 255]  # Soft shadow over parent.
    assert list(pixels[2, 3]) == [0, 0, 0, 255]  # Real black stays black.
