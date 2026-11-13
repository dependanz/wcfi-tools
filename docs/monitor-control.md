# Monitor control (Windows prototype)

Monitor control puts a live view of a connected display inside a window on your laptop.
You can switch displays and use the preview to click, drag, and scroll on the selected
display. This is useful when a projector or another screen is difficult to see directly.

The prototype runs locally on Windows using Python, Tk, and DXcam's Desktop Duplication
backend for displays and Windows Graphics Capture for application windows. GDI scales
captured memory images with HALFTONE filtering; the live preview does
not use GDI screen readback, which can make the system cursor flicker. It opens no network
server and needs no remote access account, API keys, `wcfi setup`, or installed driver.
Python must include Tcl/Tk support, which is normally included in the Windows installer.

## Launch

From an installed checkout:

```powershell
pip install -e ".[monitor-control]"
wcfi monitor-control launch
```

If `wcfi` is not yet on your PATH, run the checkout's installed executable directly:

```powershell
& 'C:\dev\wcfi-tools\.venv\Scripts\wcfi.exe' monitor-control launch
```

The command stays attached to the desktop application until it closes.

```powershell
wcfi monitor-control list                 # one-based display numbers, sizes, and positions
wcfi monitor-control launch --monitor 2   # initially select display 2
wcfi monitor-control launch --fps 10      # request fewer preview updates
wcfi monitor-control launch --practice    # open an interactive practice target
```

`--fps` accepts 1–60 and defaults to 60. The capture rate is a request; large displays and
slower computers may update less often. `--monitor` numbers follow the current `list`
output and can change after reconnecting displays. Use `--practice` or `--monitor`
individually.

The preview uses filtered downscaling to retain text and fine lines. In windowed mode it uses the
available window pixels, up to the source's native size; it no longer caps previews
at 1280 × 900 or enlarges small sources. Enlarge the viewer to see more detail.
Capture runs on its own worker using high-resolution performance-counter deadlines.
The UI presents only the newest completed frame; it never waits for desktop readback
or replays a queue of old pictures. A temporary high-resolution Windows timer request
is active while the viewer is visible. Minimizing it pauses capture and releases that
request; closing it also restores normal timer settings. Target changes discard any
in-flight old frames, and a preview that stops updating releases input control.

## Use the controller

Choose an **Application** or **Display** in the picker. Once selected, the picker folds
away and the preview fills the available window, preserving the target's proportions.
**Switch app / display** always remains in the top bar; open it to search, switch views,
select another target, or access **Move to display**, **Bring to laptop**, and layout
controls. **Back to preview** closes the picker without changing your selection.
The target title and **Recover selected** remain visible while viewing.

The viewer is a normal window while idle: other applications can cover it. During
application interaction it temporarily stays above the controlled app so activating
that app does not hide the preview. Leaving the image, stopping control, or closing
the viewer restores normal stacking without taking focus from another application.

1. Keep the controller on the laptop display. Choose **Displays**, then another display.
2. Move into the live image to interact with the target. No Control button is needed.
3. Move within the preview to move the target pointer. Click, right-click, drag, or scroll
   as you would on that display. The preview shows the pointer's position.
4. Click a target window before typing. Keyboard input follows ordinary Windows focus;
   it goes to the focused application, which may now be on the selected display.
5. Move outside the image to use the viewer or another app normally. Leaving during a drag
   releases its held buttons. Re-enter to continue. **Ctrl+Alt+Q** is an emergency stop;
   after using it, move out and back in (or click the image) to resume.

Entry starts at the point under the pointer, without jumping to the image center.
Letterbox margins do not control anything. A drag begun outside the image is not taken
over, and a quick first click is forwarded only if the selected target and preview
geometry have not changed. Focus loss or a capture failure requires a new entry or click;
the viewer never automatically takes focus back merely because capture recovers.

The controller releases held mouse buttons when control stops. Switching targets or views,
or moving the viewer/practice window during display control, releases control. Display
connections are checked about once per second; a change releases control and updates the
list. **Refresh** requests an immediate window-list scan and checks display connections.
Refreshing an unchanged display configuration does not interrupt control.

When the selected display disconnects, its selection stays visible as **Disconnected ·
Waiting to reconnect**. The viewer discards its old pixels and input coordinates. When
the same monitor identity returns, capture is rebuilt and the preview resumes after a
short settling interval, without restarting the viewer or reselecting the monitor.
Input stays released until you enter the fresh preview again. Choosing another target
while waiting is respected; reconnecting does not switch you back.

Selection uses the monitor interface identity exposed by Windows through
[`EnumDisplayDevicesW`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-enumdisplaydevicesw)
when available, rather than a display number. This follows a monitor even when its GDI
name changes, and does not select a different monitor that reuses that name. Some virtual
drivers expose only a GDI name; a different port/dock or changed driver identity can
require selecting the display again. Temporary enumeration failures and periods with no
connected displays pause input and keep checking automatically.

If disconnecting a display strands Monitor Control's own title bar offscreen, the viewer
returns to a connected screen without taking focus. A deliberately minimized viewer stays
minimized. Other applications are not automatically repositioned; use Bring to laptop when
needed. A reconnected display that now contains the viewer still pauses full-display
capture to avoid a recursive preview; move the viewer or choose Applications.

Windows still has one system pointer. During control, the app maps your movements to the
target display, hides that native pointer, and draws the pointer inside the preview.
Leaving restores the visible pointer one pixel outside the first image edge crossed,
even when a raw/remote movement packet overshoots far beyond it. Diagonal exits use the
crossing point on that edge. Interrupted
control also returns to the current preview position rather than the old entry point.
This is not a second independent mouse or a remote user session. Closing the app releases control.

Native cursor hiding uses a temporary input-queue attachment and balanced `ShowCursor`
calls owned by the input worker. It does not replace system cursor images or change cursor
settings. The worker pumps Windows messages while attached, follows mouse-capture owners
during drags, and undoes its visibility changes on every release path. The preview marker
remains visible throughout interaction. Remote clients can have their own cursor rendering;
cursor suppression was confirmed through Chrome Remote Desktop. The user reported that
the return was inside the viewer but away from the image edge; the updated first-edge
return mapping passes native checks on all four sides and awaits the remote recheck.

Movement comes from Windows Raw Input, including precision touchpad packets without a
device handle. The app's generated cursor movements are excluded, so they cannot feed
back into physical movement. Raw relative motion can feel different from Windows pointer
acceleration. If Windows reports cursor movement but supplies no raw device motion, the
controller releases with an explanation instead of guessing a movement delta.

## Try it with one display

Normal launch opens **Applications**, listing titled windows including Chrome and other
open apps. Search by window title, select a result, then move into its preview. The application
can remain completely behind the viewer: its preview comes from Windows Graphics Capture,
not the desktop pixels covering it. During control the viewer stays visible but lets
redirected mouse input reach the target behind it. Click a field to type normally.
Moving outside the image restores normal pointer use. **Ctrl+Alt+Q** is the emergency stop.
The selected app receives keyboard focus. Switching focus to an unrelated app releases
control, with a message explaining how to resume. Blank space between captured windows
and unrelated windows covering an input location do not receive redirected mouse input;
the status line explains when input is paused. The viewer's temporary
input style is restored before unhooking and returning the pointer, including after a UI
stall, so a fast exit click cannot pass through the viewer while waiting for Tk to recover.

**Arrange side by side** is optional. It restores and moves only the selected application
and the viewer onto the viewer's display. It does not restore their old positions afterward.
**Move to display…** lists the currently connected displays and moves the selected
application and its visible owned dialogs to the chosen display. The destination is
checked again when clicked, so an unplugged display cannot redirect the action to an
unrelated display number. **Bring to laptop** uses the display containing Monitor Control;
keep the viewer on the laptop screen when using this shortcut. It does not assume that
Windows' primary display is the laptop.

Both placement controls work without a working preview, including windows stranded
entirely offscreen after unplugging a monitor. They restore minimized windows, retain
ordinary size when it fits, and shrink oversized windows to the destination's usable
area (excluding the taskbar). Apps enforcing larger minimum sizes keep a reachable
top-left/title bar. Maximized windows are restored for placement, then maximized again.
The viewer itself stays in place, selection is retained, and input remains released
until a fresh preview is available and you re-enter it. Hidden or closed windows still
cannot be moved through this tool. Placement does not automatically move personal
windows just because a display disconnects; choose the target and use either control.
Applications are discovered in the background about every 1.5 seconds, including new
windows, changed titles, and minimized state. **Applications** and **Displays** each retain
their own search and selection. Search filters the list without changing the selected
preview; the selected title stays visible above the picker. If the selected window closes,
it is marked unavailable instead of silently switching to another app. Duplicate titles
have distinct entries, and list updates wait until an open dropdown closes before
reordering its rows. **Refresh** requests an earlier scan.
Application windows must remain restored. Covered windows are supported; minimized,
hidden, or closed windows are not. An application and its owned dialogs may span connected
displays, including negative coordinates and displays above/below one another. Moving/resizing an app
keeps its capture session and control session alive: input waits for pixels matching the
new geometry, then updates its coordinates. Resizing the viewer continues presenting
frames instead of pausing until the resize ends. Focus loss, minimization, closure, or a
stalled preview still releases input. Restore a minimized or stranded window with
**Recover selected** (preserves layout) or **Arrange side by side** (moves it onto this display).
Window capture requires Windows 10 version 1903 or newer.
Owned dialogs, context menus, and cascading submenus appear automatically in the same
preview. Each window is captured independently and composed in desktop coordinates and
native stacking order before scaling. The preview expands when a menu/dialog extends
beyond the parent, then contracts when it closes. It keeps the same control session and
preserves the pointer's desktop position when the preview layout changes. New popup input
waits for its first complete preview frame; clicks are not queued for later replay.
Closing a dialog returns to the parent without selecting it again.

If focus or capture gets stuck, release with **Ctrl+Alt+Q**, then choose **Recover selected**.
This rebuilds capture, restores a minimized application, and activates its current owned
dialog when one is open. The selected window stays selected. After a fresh frame appears,
move into the preview to resume. Closed or replaced windows must
be selected again from the updated list; a reused native window handle is never trusted.

Missing preview frames trigger three quick automatic capture rebuilds, with increasing
retry delays, then continue retrying at 15-second intervals. Capture gets five seconds to
deliver a frame before rebuilding, including initial startup. Active input releases after
half a second of stale preview. Automatic recovery never resumes input or takes focus;
Recover selected retries immediately. A detected topology change clears the retry delay
and rebuilds capture for the new layout, even after an extended outage.

In **Applications**, a window can straddle displays or move from one to another without
reselecting it. A dialog on another display joins the same preview automatically. The
preview expands to their desktop positions, so widely separated windows appear smaller.
Coordinates remain physical pixels; a resize associated with a DPI change waits for
matching captured pixels before input resumes. Physical mixed-DPI and multi-GPU behavior
still needs hardware validation.

Parts outside connected displays are clipped. Gaps in an offset display layout are dark
and reject input, while the preview pointer can travel across them to reach a dialog.
Entirely offscreen windows cannot receive input. A visible owned dialog can still be used
when its parent is offscreen. If the entire family is offscreen, use **Bring to laptop**
or **Move to display…** to recover it without needing its preview first.
For bounded memory use, the source scene and each captured window are limited to about
33.5 million pixels; a larger spread asks you to move the windows closer or reduce their size.
The **Displays** view still controls one selected display at a time; use **Applications**
when moving one app across displays through its preview.

Windows must expose an owner
relationship to the selected window, or a native menu associated with its active GUI
thread. Separate unrelated app windows are not automatically included merely because
they share a process. Custom unowned popups may need display control on a second monitor.
The implementation does not depend on Windows 11's
[`IncludeSecondaryWindows` option](https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.graphicscapturesession.includesecondarywindows),
which clips secondary windows at the parent's edges.

**Practice**, or `launch --practice`, opens an optional test window alongside
the controller. Practice mode captures and controls only that window's rectangle. It
lets you try pointer movement, clicking, scrolling, and dragging without another monitor.
The practice window stays on top so it can be captured. Keep it visible, entirely within
one display, and separate from the controller. Control is blocked while the windows
overlap or the practice target crosses a display boundary.

Use `--practice` to request this mode even after connecting other displays. Use `--monitor 1`
to preview the actual display instead. Preview pauses when the viewer overlaps the selected
display, with instructions to move it to another display or choose an application window.
The viewer does not exclude itself from screen capture, so it is no longer intentionally
hidden from remote-desktop clients. Viewer visibility was confirmed by a user connected
through Chrome Remote Desktop on 2026-10-24. Tagged CRD absolute raw mouse events now map
screen positions directly into the preview rather than being treated as touchpad deltas.
Move into the image to interact; leaving it again releases control to avoid
redirecting clicks on the toolbar into the application. Local touchpad motion retains
its independent raw-delta path. Older CRD builds without the source tag and sessions
that do not deliver raw input still require validation; this is not a blanket remote-input
compatibility claim. The user confirmed pointer movement and selecting Bravo in Chrome's
dropdown through CRD before the hover-entry change. Diagnostics showed untagged absolute
raw events in that session, so that result does not validate the newer tagged-event branch.
The user also confirmed automatic hover entry/exit and, subsequently, hidden native cursor
behavior with return into the viewer. The return was away from the image edge; the updated
first-edge mapping needs a live CRD check, along with right-click menus and the file picker.
On one display, choose a real application window;
Practice is an optional test.

## Prototype limits

- Displays use Desktop Duplication; application windows use Windows Graphics Capture.
  Both request 60 frames per second by default. The optional `monitor-control` dependency
  extra supplies DXcam 0.3 and WinRT bindings; capture devices are
  owned by the worker and rebuilt after display changes. The first frame can take a few
  seconds while the graphics backend starts. Actual refresh
  can be lower because capture, conversion, and UI scheduling take time. It is intended for managing
  windows and dialogs, rather than judging video playback quality or color accuracy.
- Protected video, the UAC secure desktop, and some exclusive fullscreen applications may
  appear blank or may not accept input. Applications running as administrator can reject
  input from a controller running with ordinary privileges.
- Keep the controller and target on separate displays for normal monitor control. The
  preview pauses while the controller overlaps the selected display.
- Practice mode exercises the capture and input path on one physical display. Physical
  multi-monitor behavior, including mixed DPI layouts and reconnecting projectors, still
  needs validation on the intended hardware.

For a first hardware trial, use an ordinary test window on an extended desktop, verify that
**Ctrl+Alt+Q** returns control, then check clicking, dragging, scrolling, and typing before
using the tool during an event.

## Developer validation

The regular unit tests use fake desktop input and UI stubs. They do not install native
input hooks, open windows, or move the pointer, so they can run in normal CI:

```powershell
python -m pytest tests/test_monitor_catalog.py tests/test_monitor_control_cli.py tests/test_monitor_input.py tests/test_monitor_preview.py tests/test_monitor_ui_lifecycle.py tests/test_monitor_raw_input.py tests/test_monitor_motion_regression.py tests/test_monitor_scroll_regression.py tests/test_monitor_pacing.py tests/test_monitor_stream.py tests/test_monitor_windows.py tests/test_monitor_window_scene.py tests/test_monitor_capture.py
```

The separate [Windows integration script](../scripts/test-monitor-control-windows.py)
is an opt-in test that intentionally opens and controls the application's own practice
windows. It moves the system pointer, clicks, types, drags, scrolls, and tests the
**Ctrl+Alt+Q** release shortcut. It also checks rendering after maximizing/restoring the
viewer and deliberately stalls the UI to exercise the independent release watchdog.
It is outside pytest discovery and must not run in unattended CI. Leave the mouse and
keyboard alone while it runs; it closes its windows and restores the original pointer
position and foreground window afterward.
Movement samples are simulated at the raw-motion callback; clicks, typing, wheel events,
cursor output, hook installation, and release are native. This cannot validate physical
touchpad or mouse delivery: also test small movements with the actual pointing device.

```powershell
& '.venv\Scripts\python.exe' scripts/test-monitor-control-windows.py
```

The script writes its viewer screenshot to `_work/monitor-control/practice-smoke.png`.
`scripts/test-monitor-app-window-windows.py` additionally launches a disposable application
in a separate process. It covers that app completely with the viewer and checks live
pixels after interaction, clicking, typing, scrolling, a context menu extending beyond
the parent, a cascading submenu, and an owned dialog that accepts typing and clicks.
It also checks Ctrl+Alt+Q, watchdog recovery, focus loss and explicit resume, recovery with
an owned dialog open, and restoring a minimized target without changing its layout or
picker selection. It verifies automatic discovery/title updates, search and view selection
memory, compact layout, movement/resizing while control remains active, and a native
title-bar drag through the preview. Closing the
target releases control. This test
also briefly takes pointer control and restores it afterward; it does not operate the
user's existing applications.
Native hover entry, edge exit, re-entry, click, typing, drag, scroll, emergency release,
and the GUI-stall release watchdog
have been exercised on the single-display Windows development laptop. This does not
validate physical touchpad motion, multi-monitor
layouts, mixed DPI displays, or hotplug behavior on external hardware.

The isolated Chrome fixture (`scripts/launch-monitor-chrome-fixture.py`) serves a generated
page from loopback and records fixed test labels in
`_work/monitor-control/chrome-fixture/events.jsonl`: load, animation-frame callback,
context-menu request, dropdown choice, and selection of the disposable fixture file.
These reports do not prove that a native popup was visible or captured; verify that
visually in the viewer as well. No selected file contents are read or uploaded.
The initial Chrome test page was blank in Chrome itself. The remote user reloaded it,
and the blue page, dropdown, and file button were subsequently visible in the viewer.
This did not establish a capture-only defect. After a restart, the user confirmed pointer
movement and selection of Bravo through the Chrome dropdown over CRD. That session
reported untagged absolute raw events. The subsequent hover-entry build was confirmed to
enter and exit control over CRD, with a remaining visible-pointer jump reported by the user.
The cursor-suppression build passes native hidden/visible cursor checks at entry/exit and
across popup changes. One native run stalled in WGC after closing a dialog; two subsequent
full suite runs passed. The user confirmed hidden cursor behavior over CRD, with return
into the viewer but away from the image edge. The subsequent first-edge return mapping
passes native tests on all four sides; its remote behavior remains to be confirmed.
During the edge-return work, broader native runs also intermittently stopped at the
obscured-target guard after a title-bar drag or simulated display move. These failures
remain a separate moving-window/capture-timing issue; the edge checks passed in both runs.
A final run with the original release ordering passed edge, menu, dialog, and watchdog
checks but timed out restoring the minimized fixture. All 246 automated checks pass;
the full native lifecycle suite is not consistently green.

Follow-up handoff tracing reproduced a different failure: the native cursor initially
reached the correct viewer edge, then immediately returned to a recent target-app mouse
position. The preview rectangle did not change. Cursor return now uses marked absolute
`SendInput`, ordered in the input stream, instead of the out-of-band `SetCursorPos` call.
Ordering alone still reproduced the jump while the hook was removed too early. The
handoff now restores hit testing, returns the pointer, restores visibility, and only
then removes the input hook. In-flight physical packets remain suppressed during that
transition, with a 250 ms fail-open deadline if native cleanup stalls.
Regression tests model previously queued movement overwriting the return and physical
packets arriving during each cleanup step, including the fail-open deadline; the
native `--edge-only` test checks the displayed image after idle layout settles at three
viewer sizes. There are 249 automated checks with this change.

The user confirmed the complete handoff fix while deliberately trying to reproduce the
teleport. The live diagnostic run recorded 82 edge exits; 80 inactive post-release samples
at 20 ms had no large (>250 px) jump back toward target-app coordinates (two samples were
already controlling again). Native edge tests passed at 1100×800, 700×700, and 520×540,
along with the practice-window click/type/drag/scroll, emergency stop, and UI-stall watchdog
checks. This verifies the reported intermittent return issue on the tested laptop/session,
without extending the claim to untested remote clients or physical multi-monitor setups.

For a local diagnostic reproduction, `scripts/trace-monitor-exit.py` runs the normal
CLI and writes `_work/monitor-control/exit-trace.jsonl`. It records recent pointer
coordinates/input flags, preview rectangles, release reasons, and post-release cursor
positions, without recording keystrokes, window titles, or captured pixels. Each run
replaces that diagnostic file. Normal CLI launches do not record this trace.

Chrome popup follow-up (2026-10-24): the real Chrome context menu, native select list,
and Open file dialog were each visibly composed into the selected application's preview,
with the input session still active and no separate dialog selection. Opening and closing
the context menu restored the root preview. Completing dropdown selection and the file
picker through uninterrupted CRD mouse input still needs the remote user's confirmation;
the desktop automation changes foreground focus between actions, so it cannot establish
that result. The fixture's generated file is
`_work/monitor-control/chrome-fixture/web/wcfi-popup-test.txt`; no contents are uploaded.

Input startup failures now retain their underlying error in the queued release event,
instead of overwriting it with "Input unavailable". The regression checks both the
diagnostic and a subsequent successful start. All 250 automated monitor tests pass.
The native `--recovery-only` mode isolates three repeated focus-loss/minimize/Recover
cycles, asserts that the real recovery button received its click, and checks selection
and layout preservation. Two consecutive runs passed, followed by a successful full
native suite including drag, menus, modal recovery, watchdog, minimize/restore, and close.
An earlier recovery run failed while duplicate test viewers were open; this does not
establish the cause of every earlier intermittent failure. Close other test viewers before
running input-driving tests: their input hooks and Ctrl+Alt+Q reservations can interfere.

The remote user subsequently confirmed the Chrome interactions work, but reported a
large black border around the context menu. Scene composition had copied popup BGRA
rectangles without applying alpha before conversion to opaque PPM. It now blends
premultiplied popup pixels over the underlying app/background before scaling. Opaque
pixels retain a copy path, and blending arithmetic is limited to nonopaque pixels.
Regression checks distinguish transparent margins, partial shadows, and truly black
content, including popup regions outside the parent and single-window transparency.
Live confirmation of the border fix and its scrolling smoothness is pending; the desktop
automation could not inspect the relaunched viewer due to a foreground-process lookup error.

Remaining UX validation: completing Chrome's menu commands, file picker and dropdown
selection under uninterrupted hover control through CRD;
Ableton's intermittent track-label double-click failure; and a full Chrome Remote Desktop
session. The disposable native tests do not establish compatibility with those apps.
Cross-display composition/input is covered by tests for negative coordinates, offset
display gaps, partial offscreen clipping, per-window adapter selection, and preserving the
pointer when a remote dialog expands the preview. Run the native fixture with
`--cross-display` to exercise crossing and a remote dialog using two simulated display
rectangles inside the laptop's existing display. It changes only the test viewer's layout
model, not Windows display settings, and verifies real WGC pixels and native clicks/typing.
This is not a substitute for physical mixed-DPI monitors, hotplug, or multi-GPU validation.

Placement validation: `scripts/test-monitor-app-window-windows.py --placement-only`
strands a disposable app and owned dialog outside the real display bounds, clicks
Bring to laptop, and checks both return inside the viewer display's usable area. It also
checks moving a minimized app and rejecting a disconnected destination while preserving
selection and releasing input. This native test passed, alongside 267 automated monitor
tests covering work-area clipping, negative coordinates, maximized/minimized windows,
minimum-size constraints, stale handles, and destination changes during hotplug. Physical
second-display and mixed-DPI placement still needs hardware validation.

Disconnect/reconnect validation: `scripts/test-monitor-app-window-windows.py --hotplug-only`
simulates two displays inside the real desktop without changing Windows settings. Three
cycles remove the selected display, report no displays, then restore it under a changed
GDI name but the same monitor identity. Native capture resumed each time without restarting,
input remained released, and application preview remained usable afterward. All 277 monitor
tests pass, including identity reuse, late recovery, enumeration failures, stranded-viewer
placement, and rejection of in-flight old pixels when a reconnected screen has the same
bounds. Real cable/dock disconnects, sleep/wake, mixed DPI, and GPU-driver resets still need
hardware validation; the simulation does not establish those results.

For scroll timing, run `scripts/profile-monitor-control-windows.py` with the same Python
environment. This opt-in script also takes temporary pointer control. It compares native
wheel delivery directly to the practice widget with delivery through the controller,
both with preview capture paused and running. It records capture/conversion times,
capture and fresh-image presentation intervals, input delivery times, and missing wheel units in
`_work/monitor-control/profile.json`. Simulated wheel events exercise the real Windows
hooks and SendInput; they do not reproduce a physical touchpad driver or measure when
pixels become visible on the panel.
The scrolling phase defaults to ten seconds; use `--seconds 30` for a longer run.
Use `--window` to measure Windows Graphics Capture instead of Desktop Duplication.
Use `--dialog` to capture a parent and owned scrolling dialog together.
On the development laptop, the ten-second parent-plus-dialog scrolling run averaged
60.0 fresh-image presentations per second (18.69 ms p95 interval), with no missing wheel
units. This is a local test result, not a guarantee for every application or display size.
After the target-browser and resize changes, a ten-second parent-window run averaged
54.89 presentations per second (34.31 ms p95 interval), again with no missing wheel units.
The source/preview sizes and system load vary between these runs; they are not a controlled
before/after performance comparison. The 60 FPS target and lossless capture path are unchanged.
The report records source/preview sizes, filtering and the clock implementation.
Use presentation FPS to judge throughput, and the interval percentiles to assess
frame-time variation; a 60 FPS average does not guarantee every frame hits its deadline.

Keep the native hook/message pump on its own thread. Input injection runs on a separate
worker: SendInput can block while Windows needs the hook thread to service another event.
Combining those jobs caused hundreds of milliseconds of wheel delay even with capture
paused. The routing worker wakes on input arrival, while the hook thread waits for Windows
messages instead of polling on a timer.

`scripts/test-monitor-preview-windows.py` opens a generated fine-detail chart without
moving the pointer. It checks that reduced one-pixel stripes retain their average
color and saves filtered/old-scaling PNGs in `_work/monitor-control` for visual review.
The preview uses GDI HALFTONE filtering with a matching brush origin, following
[Microsoft's scaling documentation](https://learn.microsoft.com/en-us/windows/win32/api/wingdi/nf-wingdi-setstretchbltmode).
Display capture uses [Desktop Duplication](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api).
Application capture uses [CreateForWindow](https://learn.microsoft.com/en-us/windows/win32/api/windows.graphics.capture.interop/nf-windows-graphics-capture-interop-igraphicscaptureiteminterop-createforwindow),
with a free-threaded frame pool and owned pixel buffers. The viewer's temporary layered,
mouse-transparent style follows [Windows hit-testing rules](https://learn.microsoft.com/en-us/windows/win32/winmsg/window-features#layered-windows).
Both sources use memory-only GDI scaling. A legacy GDI
screen reader remains for one-off diagnostic screenshots, not the live preview.
Successful timer requests are paired with releases as required by
[timeBeginPeriod](https://learn.microsoft.com/en-us/windows/win32/api/timeapi/nf-timeapi-timebeginperiod).
