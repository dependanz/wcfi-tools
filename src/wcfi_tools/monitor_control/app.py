"""Local Windows monitor viewer. Tk owns the UI; native hooks own input routing."""

from __future__ import annotations

import queue
import time
import tkinter as tk
from dataclasses import replace
from tkinter import ttk
from types import SimpleNamespace

from .capture import (
    Monitor,
    enable_dpi_awareness,
    enumerate_monitors,
)
from .catalog import APPLICATIONS, DISPLAYS, PRACTICE, TargetCatalog, WindowDiscovery, identity
from .input import InputController
from .pacing import PreviewTimer
from .placement import keep_viewer_visible, move_window_to_display
from .stream import CaptureStream, Frame
from .window_scene import WindowGuard, discover_scene
from .windows import (
    AppWindow,
    ViewerPassThrough,
    activate_window,
    arrange_window,
    enumerate_windows,
    idle_pointer,
    restore_window,
    window_rect,
)

BG = "#101827"
PANEL = "#1c293c"
TEXT = "#edf3fc"
MUTED = "#a5b6ce"
ACCENT = "#57b8ff"
Rect = tuple[int, int, int, int]


def fit_preview(source: tuple[int, int], available: tuple[int, int]) -> Rect:
    """Aspect-preserving fit in physical pixels, including the letterbox offset."""
    sw, sh = source
    aw, ah = available
    if min(sw, sh, aw, ah) <= 0:
        raise ValueError("Preview dimensions must be positive")
    # Use the available physical pixels, but never enlarge: upscaling cannot
    # add detail to a smaller source.
    scale = min(aw / sw, ah / sh, 1.0)
    width, height = max(1, int(sw * scale)), max(1, int(sh * scale))
    return (aw - width) // 2, (ah - height) // 2, width, height


def overlaps(first: Rect, second: Rect) -> bool:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


class PracticeWindow:
    """A real local window that can be safely operated through its captured image."""

    def __init__(self, owner: tk.Tk, geometry: str, on_close) -> None:
        self.window = tk.Toplevel(owner)
        self.window.title("WCFI · Practice display")
        self.window.geometry(geometry)
        self.window.minsize(300, 440)
        self.window.attributes("-topmost", True)
        self.window.configure(bg="#eaf0f8")
        self.window.protocol("WM_DELETE_WINDOW", on_close)
        self.count = tk.IntVar(value=0)
        self.checked = tk.BooleanVar(value=False)
        self.text = tk.StringVar()
        tk.Label(
            self.window, text="Practice display", font=("Segoe UI", 18, "bold"),
            bg="#eaf0f8", fg="#14243b", anchor="w",
        ).pack(fill="x", padx=20, pady=(18, 3))
        tk.Label(
            self.window, text="Operate these controls through the viewer.",
            bg="#eaf0f8", fg="#465b77", anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 12))
        self.counter = tk.Button(
            self.window, text="Click me · 0", command=self.increment,
            font=("Segoe UI", 12), bg="#d4eaff", fg="#132c4b", relief="flat", pady=8,
        )
        self.counter.pack(fill="x", padx=20)
        tk.Checkbutton(
            self.window, text="Test a checkbox", variable=self.checked,
            bg="#eaf0f8", activebackground="#eaf0f8", fg="#14243b",
            font=("Segoe UI", 11), anchor="w",
        ).pack(fill="x", padx=16, pady=5)
        tk.Label(
            self.window, text="Click below, then type:", bg="#eaf0f8", fg="#465b77",
            anchor="w",
        ).pack(fill="x", padx=20)
        self.entry = tk.Entry(
            self.window, textvariable=self.text, font=("Segoe UI", 12),
            bg="white", fg="#14243b", relief="solid", borderwidth=1,
        )
        self.entry.pack(fill="x", padx=20, pady=(4, 12), ipady=5)
        self.drag_canvas = tk.Canvas(
            self.window, height=125, background="#dbe5f2", highlightthickness=0,
        )
        self.drag_canvas.pack(fill="x", padx=20)
        self.drag_canvas.create_text(
            12, 14, text="Drag the blue card", anchor="w", fill="#465b77",
        )
        self.drag_canvas.create_rectangle(
            30, 38, 185, 105, fill="#256bc3", outline="", tags="card",
        )
        self.drag_canvas.create_text(
            107, 71, text="Move me", fill="white", font=("Segoe UI", 12), tags="card",
        )
        self._drag_at: tuple[int, int] | None = None
        self.drag_canvas.tag_bind("card", "<ButtonPress-1>", self.drag_begin)
        self.drag_canvas.bind("<B1-Motion>", self.drag_move)
        self.drag_canvas.bind("<ButtonRelease-1>", lambda _: self.drag_end())
        self.scroll_list = tk.Listbox(
            self.window, height=4, font=("Segoe UI", 11), bg="white", fg="#14243b",
            relief="flat", activestyle="none",
        )
        for index in range(1, 31):
            self.scroll_list.insert("end", f"Scroll test · item {index:02}")
        self.scroll_list.pack(fill="both", expand=True, padx=20, pady=(12, 18))

    def increment(self) -> None:
        self.count.set(self.count.get() + 1)
        self.counter.configure(text=f"Click me · {self.count.get()}")

    def drag_begin(self, event) -> None:
        self._drag_at = (event.x, event.y)

    def drag_move(self, event) -> None:
        if self._drag_at is not None:
            x, y = self._drag_at
            self.drag_canvas.move("card", event.x - x, event.y - y)
            self._drag_at = (event.x, event.y)

    def drag_end(self) -> None:
        self._drag_at = None

    def rect(self) -> Rect:
        w = self.window
        return w.winfo_rootx(), w.winfo_rooty(), w.winfo_width(), w.winfo_height()


class MonitorApp:
    def __init__(
        self, root: tk.Tk, *, practice: bool = False, fps: int = 60,
        monitor: int | None = None,
    ) -> None:
        self.root = root
        self.fps = fps
        self.preview_timer = PreviewTimer()
        self.monitors = enumerate_monitors()
        if not self.monitors:
            raise RuntimeError("Windows did not report any connected displays")
        if monitor is not None and not 1 <= monitor <= len(self.monitors):
            raise ValueError(f"Choose a monitor from 1 to {len(self.monitors)}")
        self.events: queue.Queue = queue.Queue()
        self.input = InputController(self.events)
        self.capture = CaptureStream(fps)
        self.practice: PracticeWindow | None = None
        self.closed = False
        self.targets: list[Monitor | AppWindow | None] = []
        self.windows = enumerate_windows()
        self.catalog = TargetCatalog(self.monitors, self.windows)
        self.rendered_entries = None
        self.image: tk.PhotoImage | None = None
        self.preview: Rect | None = None
        self.last_target: Rect | None = None
        self.window_scene = None
        self.presented_scene = None
        self.window_guard = None
        self.scene_checked_at = 0.0
        self.frame_ready = False
        self.active_geometry: tuple[Rect, Rect] | None = None
        self.last_topology = 0.0
        self.topology_error = None
        self.last_frame_at = 0.0
        self.last_capture_at = 0.0
        self._reset_recovery()
        self.guard_feedback = ""
        self.hover_blocked = False
        self.pending_preview_click = None
        self.idle_pointer_position = None
        self.settle_until = 0.0
        self.root_geometry = None
        self.cursor_position: tuple[float, float] | None = None
        self.status = tk.StringVar(value="Starting display capture…")
        self.details = tk.StringVar()
        self.selection = tk.StringVar()
        self.mode = tk.StringVar(value=APPLICATIONS)
        self.search = tk.StringVar()
        self.list_hint = tk.StringVar()
        self.primary = next((m for m in self.monitors if m.primary), self.monitors[0])
        self._build(practice)
        self.pass_through = ViewerPassThrough(root.winfo_id())
        if practice:
            self._open_practice()
        self._populate(monitor=monitor)
        root.update_idletasks()
        self.root.after(10, self._tick)
        self.root.after(10, self._poll_input)
        self.discovery = WindowDiscovery()
        self.root.after(200, self._poll_windows)

    def _build(self, practice: bool) -> None:
        root, screen = self.root, self.primary
        root.title("WCFI · Monitor Control")
        width = min(1100, max(540, int(screen.width * (0.57 if practice else 0.7))))
        height = min(800, max(440, screen.height - 110))
        root.geometry(f"{width}x{height}{screen.left + 20:+}{screen.top + 45:+}")
        root.minsize(520, 500)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.attributes("-topmost", False)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 7))
        style.configure("TCombobox", font=("Segoe UI", 10), padding=6)
        style.configure("Mode.Toolbutton", font=("Segoe UI", 11), padding=(16, 8),
                        background=PANEL, foreground=TEXT)
        style.map("Mode.Toolbutton", background=[("selected", ACCENT)],
                  foreground=[("selected", BG)])
        style.configure("TEntry", padding=(8, 7))
        header = self.header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=12, pady=8)
        self.switch_button = ttk.Button(header, text="Choose app / display",
                                        command=self.toggle_selector)
        self.switch_button.pack(side="left", padx=(0, 10))
        self.target_title = tk.StringVar(value="Choose an application or display")
        tk.Label(header, textvariable=self.target_title, bg=BG, fg=TEXT,
                 font=("Segoe UI", 11), anchor="w", width=1).pack(
                     side="left", fill="x", expand=True)
        self.selector_open = True
        self.selector = tk.Frame(root, bg=BG)
        self.selector.pack(fill="x")
        modes = tk.Frame(self.selector, bg=BG)
        modes.pack(fill="x", padx=22, pady=(0, 8))
        for name in (APPLICATIONS, DISPLAYS):
            ttk.Radiobutton(modes, text=name, value=name, variable=self.mode,
                            style="Mode.Toolbutton", command=self._switch_view).pack(side="left")
        ttk.Button(modes, text="Practice", command=self.show_practice).pack(side="right")
        searchbar = tk.Frame(self.selector, bg=BG)
        searchbar.pack(fill="x", padx=22, pady=(0, 8))
        tk.Label(searchbar, text="Search", bg=BG, fg=MUTED).pack(side="left", padx=(0, 8))
        self.search_entry = ttk.Entry(searchbar, textvariable=self.search, font=("Segoe UI", 10))
        self.search_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(searchbar, text="Clear", command=lambda: self.search.set("")).pack(side="left")
        self.search.trace_add("write", self._search_changed)
        self.list_hint_label = tk.Label(self.selector, textvariable=self.list_hint, bg=BG, fg=MUTED,
                                       anchor="w", justify="left", wraplength=950)
        self.list_hint_label.pack(fill="x", padx=22, pady=(0, 6))
        toolbar = tk.Frame(self.selector, bg=BG)
        toolbar.pack(fill="x", padx=22, pady=(0, 10))
        self.picker = ttk.Combobox(
            toolbar, textvariable=self.selection, state="readonly", width=40,
        )
        self.picker.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.picker.bind("<<ComboboxSelected>>", self._select)
        ttk.Button(toolbar, text="Refresh", command=self.refresh).pack(side="left")
        actions = tk.Frame(self.selector, bg=BG)
        actions.pack(fill="x", padx=22, pady=(0, 10))
        self.arrange_button = ttk.Button(
            actions, text="Arrange side by side", command=self.arrange,
        )
        self.arrange_button.pack(side="right", padx=(8, 0))
        self.details_label = tk.Label(
            actions, textvariable=self.details, bg=BG, fg=MUTED,
            font=("Segoe UI", 10), anchor="w", justify="left", wraplength=340,
        )
        self.details_label.pack(side="left", fill="x", expand=True)
        tk.Label(self.selector, text="Emergency stop: Ctrl + Alt + Q", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=22, pady=(0, 6))
        self.placement_bar = tk.Frame(self.selector, bg=BG)
        self.placement_bar.pack(fill="x", padx=22, pady=(0, 8))
        self.move_button = ttk.Button(self.placement_bar, text="Move to display…",
                                      command=self._show_destinations)
        self.move_button.pack(side="left")
        self.bring_button = ttk.Button(self.placement_bar, text="Bring to laptop",
                                       command=self.move_selected)
        self.bring_button.pack(side="left", padx=(8, 0))
        self.destination_menu = tk.Menu(root, tearoff=False)
        self.canvas = tk.Canvas(root, bg="#070c14", highlightthickness=1,
                                highlightbackground="#34455e")
        self.image_item = self.canvas.create_image(0, 0, anchor="nw")
        self.placeholder = self.canvas.create_text(
            30, 30, text="Waiting for a frame…", fill=MUTED, anchor="nw",
            font=("Segoe UI", 12),
        )
        self.cursor_ring = self.canvas.create_oval(
            0, 0, 0, 0, outline="#ffe887", width=3, state="hidden",
        )
        self.cursor_dot = self.canvas.create_oval(
            0, 0, 0, 0, fill="#111827", outline="white", width=1, state="hidden",
        )
        footer = self.footer = tk.Frame(root, bg=BG)
        tk.Label(footer, text="Hover to interact · Leave image to return",
                 bg=BG, fg=TEXT, font=("Segoe UI", 10)).pack(side="left")
        self.recover_button = ttk.Button(
            footer, text="Recover selected", command=self.recover,
        )
        self.recover_button.pack(side="right")
        self.status_label = tk.Label(
            root, textvariable=self.status, bg=BG, fg=MUTED,
            font=("Segoe UI", 10), anchor="w", justify="left", wraplength=950,
        )
        # Reserve the controls before allocating the flexible preview cavity.
        self.status_label.pack(side="bottom", fill="x", padx=12, pady=(0, 6))
        footer.pack(side="bottom", fill="x", padx=12, pady=4)
        self.canvas.pack(fill="both", expand=True, padx=8)
        self.root.bind("<Configure>", self._configured)
        self.root.bind("<Motion>", self._preview_hover)
        for button in (1, 2, 3):
            self.canvas.bind(f"<ButtonPress-{button}>", self._preview_press)
            self.canvas.bind(f"<ButtonRelease-{button}>", self._preview_click)

    def toggle_selector(self) -> None:
        if self.selector_open and self.catalog.current() is None:
            self.status.set("Choose an application or display first.")
            return
        self._set_selector(not self.selector_open)
        if self.selector_open:
            self.search_entry.focus_set()

    def _set_selector(self, opened: bool) -> None:
        if self.selector_open == opened:
            return
        # Changing the canvas layout must never reuse the old input rectangle.
        self.release("Choose a target below." if opened else "Move into the preview to interact.")
        self.hover_blocked = opened
        self.frame_ready = False
        self.preview = None
        self._clear_preview("Waiting for a frame…")
        self.selector_open = opened
        if opened:
            self.selector.pack(fill="x", before=self.canvas)
        else:
            self.selector.pack_forget()
        self.switch_button.configure(text="Back to preview" if opened else "Switch app / display")
        self.root.update_idletasks()

    def _preview_hover(self, event) -> None:
        """Enter only on real motion delivered to our visible canvas.

        Do not poll the redirected OS cursor: it is physically over the target,
        not over the preview. The input worker handles motion until exit.
        """
        if self.closed or self.input.active or self.active_geometry is not None:
            return
        if self.preview is None:
            return
        preview = self._preview_rect()
        point = event.x_root - preview[0], event.y_root - preview[1]
        inside = (event.widget is self.canvas and 0 <= point[0] < preview[2]
                  and 0 <= point[1] < preview[3])
        if not inside:
            self.hover_blocked = False
            return
        if self.hover_blocked or not self.frame_ready or event.state & 0x1F00:
            return  # Never take over a drag that began outside the preview.
        self.control(point)

    def _poll_hover(self) -> None:
        # Windows does not reliably deliver Tk Motion while this topmost viewer
        # lacks focus (including some remote sessions). Poll only while idle,
        # with an actual native hit test; never infer motion from a warped cursor.
        if self.input.active or self.active_geometry is not None or self.root.state() not in {
            'normal', 'zoomed',
        }:
            return
        try:
            point, over_viewer, held = idle_pointer(self.root.winfo_id())
        except (OSError, RuntimeError):
            return
        previous, self.idle_pointer_position = self.idle_pointer_position, point
        if previous is not None and point != previous:
            self._preview_hover(SimpleNamespace(
                x_root=point[0], y_root=point[1], state=0x100 if held else 0,
                widget=self.canvas if over_viewer else None,
            ))

    def _preview_press(self, event) -> None:
        self.pending_preview_click = None
        if self.closed or not self.frame_ready or self.preview is None:
            return
        try:
            self.pending_preview_click = (
                event.num, event.x_root, event.y_root, self._preview_rect(), self._selected(),
            )
        except RuntimeError:
            return

    def _preview_click(self, event) -> None:
        # A very quick first click can arrive before the Motion callback installs
        # the hook. Forward it only after the physical button has been released.
        pressed, self.pending_preview_click = self.pending_preview_click, None
        if (not pressed or self.closed
                or (not self.input.active and self.active_geometry is not None)):
            return
        if not self.frame_ready or self.preview is None:
            return
        preview = self._preview_rect()
        try:
            if (pressed[0] != event.num or pressed[3] != preview or pressed[4] != self._selected()
                    or abs(pressed[1] - event.x_root) > 5 or abs(pressed[2] - event.y_root) > 5):
                return
        except RuntimeError:
            return
        point = event.x_root - preview[0], event.y_root - preview[1]
        if not (0 <= point[0] < preview[2] and 0 <= point[1] < preview[3]):
            return
        self.hover_blocked = False
        if not self.input.active:
            self.control(point)
        if self.input.active:
            try:
                self.input.click(event.num, point=point)
            except (OSError, RuntimeError, ValueError) as exc:
                self.release(f"Click unavailable: {exc}")

    def _configured(self, event) -> None:
        if event.widget is self.root:
            geometry = event.x, event.y, event.width, event.height
            if geometry == self.root_geometry:
                return
            self.root_geometry = geometry
            self.status_label.configure(wraplength=max(300, event.width - 44))
            self.list_hint_label.configure(wraplength=max(300, event.width - 44))
            self.details_label.configure(wraplength=max(160, min(340, event.width - 260)))
            # Keep presenting while resizing. Commit new input geometry only
            # after its matching frame has been displayed.
            self.frame_ready = False
            if self.input.active and self.window_guard is not None:
                self.window_guard.presented = None
            elif self.input.active:
                self.release("Released because the viewer moved/resized")

    def _open_practice(self) -> None:
        if self.practice is not None:
            return
        self.root.update_idletasks()
        screen = self.primary
        left = self.root.winfo_rootx() + self.root.winfo_width() + 20
        width = max(300, min(500, screen.left + screen.width - left - 20))
        height = min(700, max(440, screen.height - 110))
        # A small display may need a narrower viewer to make both windows visible.
        if left + width > screen.left + screen.width:
            viewer_width = max(520, screen.width - width - 70)
            self.root.geometry(f"{viewer_width}x{height}{screen.left + 15:+}{screen.top + 45:+}")
            self.root.update_idletasks()
            left = self.root.winfo_rootx() + self.root.winfo_width() + 18
        geometry = f"{width}x{height}{left:+}{screen.top + 45:+}"
        self.practice = PracticeWindow(self.root, geometry, self._close_practice)
        self.root.update_idletasks()

    def _close_practice(self) -> None:
        if self.catalog.mode == PRACTICE:
            self.release("Practice window closed")
        if self.practice is not None:
            self.practice.window.destroy()
            self.practice = None
        if self.catalog.mode == PRACTICE:
            self.mode.set(APPLICATIONS)
            self._switch_view()

    def show_practice(self) -> None:
        self.release()
        self._open_practice()
        self.mode.set(PRACTICE)
        self._switch_view()
        self._set_selector(False)

    def _switch_view(self) -> None:
        self.catalog.mode = self.mode.get()
        if (self.catalog.mode == DISPLAYS and DISPLAYS not in self.catalog.selections
                and self.monitors):
            self.catalog.select(next((m for m in self.monitors if not m.primary), self.monitors[0]))
        elif self.catalog.mode == PRACTICE:
            self.catalog.select(None)
        self.search.set(self.catalog.searches[self.catalog.mode])
        self._populate()
        self._selection_changed()

    def _search_changed(self, *_args) -> None:
        self.catalog.searches[self.catalog.mode] = self.search.get()
        self._populate()

    def _populate(
        self, monitor: int | None = None, device: str | None = None,
        window_key: tuple[int, int] | None = None,
    ) -> None:
        """Update visible rows without restarting capture or changing the controlled target."""
        self.catalog.monitors, self.catalog.windows = self.monitors, self.windows
        explicit = monitor is not None or device is not None or window_key is not None
        if monitor is not None or device is not None:
            self.catalog.mode = DISPLAYS
            target = (self.monitors[monitor - 1] if monitor is not None else
                      next((m for m in self.monitors if m.device == device), None))
            if target is not None:
                self.catalog.select(target)
        elif window_key is not None:
            self.catalog.mode = APPLICATIONS
            target = next((w for w in self.windows if w.key == window_key), None)
            if target is not None:
                self.catalog.select(target)
        elif self.practice is not None and not self.catalog.selections:
            self.catalog.mode = PRACTICE
            self.catalog.select(None)
            explicit = True
        self.mode.set(self.catalog.mode)
        entries = self.catalog.entries()
        selected = self.catalog.current()
        self.target_title.set(selected.label if selected else "Choose an application or display")
        if self.catalog.mode == APPLICATIONS:
            self.placement_bar.pack(fill="x", padx=22, pady=(0, 8))
        else:
            self.placement_bar.pack_forget()
        movable = selected is not None and selected.available and isinstance(selected.target, AppWindow)
        for button in (self.move_button, self.bring_button):
            button.configure(state="normal" if movable else "disabled")
        signature = (self.catalog.mode, identity(selected.target) if selected else None,
                     tuple((identity(e.target), e.label, e.available) for e in entries))
        # A posted native dropdown must keep its row indices stable until it closes.
        popup = self.picker.tk.call("ttk::combobox::PopdownWindow", self.picker)
        posted = self.picker.tk.getboolean(self.picker.tk.call("winfo", "ismapped", popup))
        if not posted and signature != self.rendered_entries:
            self.targets = [e.target for e in entries]
            self.picker.configure(values=[e.label for e in entries])
            self.picker.set("")
            if selected is not None:
                index = next((i for i, e in enumerate(entries)
                              if identity(e.target) == identity(selected.target)), None)
                if index is not None:
                    self.picker.current(index)
            self.rendered_entries = signature
        hint = f"{len(entries)} results · Updates automatically"
        if not entries:
            hint = "No matches · Clear search or wait for an application to open"
        if selected is not None:
            label = selected.label if len(selected.label) <= 90 else selected.label[:87] + "…"
            hint += " · Selected: " + label
        if self.list_hint.get() != hint:
            self.list_hint.set(hint)
        if explicit:
            self._selection_changed()
            if selected is not None:
                self._set_selector(False)

    def _poll_windows(self) -> None:
        if self.closed:
            return
        # Keep device discovery alive even when preview recovery is exhausted or
        # the selected application is minimized/unavailable.
        if time.monotonic() - self.last_topology > 1:
            self.last_topology = time.monotonic()
            try:
                self._update_monitors()
            except (OSError, RuntimeError) as exc:
                self.list_hint.set(f"Display list unavailable: {exc}")
        result = self.discovery.take()
        if isinstance(result, list):
            self.windows = result
        elif isinstance(result, Exception):
            self.list_hint.set(f"Window list unavailable: {result} · Retrying automatically")
        if not isinstance(result, Exception):
            self._populate()
        self.root.after(200, self._poll_windows)

    def _selected(self) -> Monitor | AppWindow | None:
        selected = self.catalog.current()
        if selected is None:
            raise RuntimeError(f"Choose a target in {self.catalog.mode} above.")
        if not selected.available:
            if isinstance(selected.target, Monitor):
                raise RuntimeError("Selected display disconnected. Waiting for it to reconnect; "
                                   "you can choose another target meanwhile.")
            raise RuntimeError("Selected target is unavailable. Choose another target above.")
        return selected.target

    def _select(self, _event=None) -> None:
        index = self.picker.current()
        if 0 <= index < len(self.targets):
            self.catalog.select(self.targets[index])
            self._selection_changed()
            self._populate()
            self._set_selector(False)

    def _selection_changed(self) -> None:
        self.release("Selection changed")
        self.hover_blocked = False
        self.capture.pause()
        self._reset_recovery()
        self.frame_ready = False
        self.window_scene = self.presented_scene = None
        self.scene_checked_at = 0.0
        self._clear_preview("Waiting for a frame…")
        try:
            target = self._selected()
        except RuntimeError as exc:
            self.arrange_button.configure(state="disabled")
            self.details.set(str(exc))
            self.status.set(str(exc))
            return
        is_window = isinstance(target, AppWindow)
        self.arrange_button.configure(state="normal" if is_window else "disabled")
        self.details.set(
            "Move into the preview to use the practice window."
            if target is None else
            "This window can stay behind the viewer. Interact directly with its preview."
            if is_window else
            f"{target.device}  ·  Position {target.left}, {target.top}  ·  {self.fps} fps target"
        )
        self.status.set("Move into the preview to interact.")

    def _reset_recovery(self) -> None:
        self.capture_started_at = None
        self.healthy_since = None
        self.recovery_attempts = 0
        self.retry_at = 0.0
        self.recovery_exhausted = False
        self.recovering = False

    def recover(self) -> None:
        """Rebuild capture and deliberately restore focus without restarting the viewer."""
        self.release("Recovering selected target…")
        self.capture.reset()
        self._reset_recovery()
        self.frame_ready = False
        self.window_scene = self.presented_scene = None
        self.scene_checked_at = 0.0
        self._clear_preview("Reconnecting preview…")
        try:
            selected = self._selected()
            if isinstance(selected, AppWindow):
                restore_window(selected)
                self.pass_through.raise_viewer()
            self.settle_until = time.monotonic() + .5
            self.recovering = True
            self.status.set("Reconnecting preview · Control can resume after a fresh frame.")
        except (OSError, RuntimeError, ValueError) as exc:
            self.status.set(f"Could not recover: {exc}")

    def _clear_preview(self, message: str) -> None:
        self.image = None
        self.canvas.itemconfigure(self.image_item, image="")
        self.canvas.itemconfigure(self.placeholder, state="normal", text=message, width=400)

    def _retry_capture(self, reason: str) -> None:
        # Never resume redirected input or steal focus during automatic recovery.
        if self.input.active:
            self.release(f"Released · {reason}")
        self.capture.reset()
        self.frame_ready = False
        self.capture_started_at = self.healthy_since = None
        self.recovering = True
        self._clear_preview(reason)
        if self.recovery_attempts >= 3:
            self.recovery_exhausted = True
            self.retry_at = time.perf_counter() + 15
            self.status.set(f"{reason} · Retrying in 15 seconds. Recover selected retries now.")
            return
        self.recovery_attempts += 1
        self.retry_at = time.perf_counter() + .5 * 2 ** (self.recovery_attempts - 1)
        self.status.set(f"{reason} · Reconnecting preview ({self.recovery_attempts}/3)…")

    def refresh(self) -> None:
        # Manual refresh shares the background discovery path. A title/list change
        # must never release an active session or reset the selected application's camera.
        self.discovery.request()
        self._update_monitors()
        self._populate()

    def _update_monitors(self) -> None:
        try:
            monitors = enumerate_monitors()
            self.topology_error = None
        except (OSError, RuntimeError) as exc:
            # Do not continue routing input with a topology we could not verify.
            monitors = []
            self.topology_error = str(exc)
        if monitors != self.monitors:
            self.release("Display configuration changed")
            self.capture.reset()
            self._reset_recovery()
            self.frame_ready = False
            self.window_scene = self.presented_scene = None
            self.scene_checked_at = 0.0
            self.last_target = None
            self.monitors = monitors
            self.catalog.monitors = monitors
            self.settle_until = time.monotonic() + .5
            self.recovering = True
            self._clear_preview("Display configuration changed · Reconnecting preview…")
            if monitors:
                self.primary = next((m for m in monitors if m.primary), monitors[0])
                keep_viewer_visible(self.root.winfo_id(), monitors)
            self._populate()

    def arrange(self) -> None:
        try:
            selected = self._selected()
        except RuntimeError as exc:
            self.status.set(str(exc))
            return
        if not isinstance(selected, AppWindow):
            return
        self.release()
        self.capture.pause()
        self.frame_ready = False
        try:
            arrange_window(selected, self.root.winfo_id())
            self.settle_until = time.monotonic() + .5
            self.status.set("Windows arranged · Move into the preview to interact.")
        except (OSError, RuntimeError, ValueError) as exc:
            self.status.set(str(exc))

    def _show_destinations(self):
        # Build on opening, using device identities rather than unstable row numbers.
        try:
            monitors = enumerate_monitors()
            self.destination_menu.delete(0, "end")
            for index, monitor in enumerate(monitors, 1):
                self.destination_menu.add_command(
                    label=f"Display {index} · {monitor.label}",
                    command=lambda device=monitor.key: self.move_selected(device),
                )
            if not monitors:
                self.status.set("No connected displays. Reconnect a display and try again.")
                return
            self.destination_menu.tk_popup(self.move_button.winfo_rootx(),
                                           self.move_button.winfo_rooty() +
                                           self.move_button.winfo_height())
        except (OSError, RuntimeError, tk.TclError) as exc:
            self.status.set(f"Could not list destinations: {exc}")
        finally:
            self.destination_menu.grab_release()

    def move_selected(self, device=None):
        """Relocate without requiring a usable preview (including stranded windows)."""
        try:
            selected = self._selected()
            if not isinstance(selected, AppWindow):
                return
            self.release("Moving selected application…")
            self.capture.reset()
            self._reset_recovery()
            self.frame_ready = False
            self.window_scene = self.presented_scene = None
            self.scene_checked_at = 0.0
            self._clear_preview("Waiting for the moved window…")
            move_window_to_display(selected, self.root.winfo_id(), device)
            self.settle_until = time.monotonic() + .5
            self.recovering = True
            self.status.set("Window moved · Waiting for a fresh preview.")
        except (OSError, RuntimeError, ValueError) as exc:
            self.status.set(f"Could not move window: {exc}")
        finally:
            self.pass_through.raise_viewer()

    def _target_rect(self) -> Rect:
        if not self.monitors:
            if self.topology_error:
                raise RuntimeError(f"Display detection interrupted: {self.topology_error}. "
                                   "Retrying automatically.")
            raise RuntimeError("No connected displays. Waiting for a display to reconnect.")
        target = self._selected()
        if isinstance(target, AppWindow):
            rect = window_rect(target)
            displays = tuple((m.left, m.top, m.width, m.height) for m in self.monitors)
            now = time.perf_counter()
            if (self.window_scene is None or self.window_scene.layers[0].bounds != rect
                    or self.window_scene.displays != displays
                    or now - self.scene_checked_at >= .03):
                self.window_scene = replace(discover_scene(target, rect), displays=displays)
                self.scene_checked_at = now
            return self.window_scene.bounds
        if target is None:
            if self.practice is None or self.practice.window.state() not in {"normal", "zoomed"}:
                raise RuntimeError("Restore the practice window to continue")
            return self.practice.rect()
        return target.left, target.top, target.width, target.height

    def _viewer_rect(self) -> Rect:
        # Include title bar and resize borders when preventing control of our own window.
        return (self.root.winfo_rootx() - 10, self.root.winfo_rooty() - 40,
                self.root.winfo_width() + 20, self.root.winfo_height() + 50)

    def _preview_rect(self) -> Rect:
        if self.preview is None:
            raise RuntimeError("Wait for the first preview frame")
        x, y, width, height = self.preview
        return self.canvas.winfo_rootx() + x, self.canvas.winfo_rooty() + y, width, height

    def control(self, point=None) -> None:
        if self.input.active or not self.frame_ready or time.monotonic() < self.settle_until:
            return
        try:
            target = self._target_rect()
            if self._selected() is None and not any(
                target[0] >= m.left and target[1] >= m.top
                and target[0] + target[2] <= m.left + m.width
                and target[1] + target[3] <= m.top + m.height
                for m in self.monitors
            ):
                self.status.set("Move the entire practice window onto one display first.")
                return
            if not isinstance(self._selected(), AppWindow) and overlaps(target, self._viewer_rect()):
                self.status.set(
                    "Choose Arrange side by side, then enter the preview."
                    if isinstance(self._selected(), AppWindow) else
                    "Move the viewer off the selected display, or use Practice on one screen."
                )
                return
            preview = self._preview_rect()
            selected = self._selected()
            if isinstance(selected, AppWindow):
                if self.window_scene != self.presented_scene:
                    return  # Wait for the first complete popup scene before accepting input.
                self.pass_through.enable()
                activate_window(selected)
                self.pass_through.raise_viewer()
            self.active_geometry = target, preview
            point = point if point is not None else (preview[2] // 2, preview[3] // 2)
            if isinstance(selected, AppWindow):
                self.window_guard = WindowGuard(selected, self.presented_scene)
                self.input.start(target, preview, guard=self.window_guard,
                                 initial_point=point, seamless=True,
                                 on_release=self.pass_through.disable)
            else:
                self.input.start(target, preview, initial_point=point, seamless=True)
            self.input.heartbeat()
            self.cursor_position = point
            self.status.set(
                "Interacting · Click a field to type. Move out of the image to return."
            )
            self.status_label.configure(fg="#ffe887")
            self.canvas.configure(highlightbackground=ACCENT)
        except (OSError, RuntimeError, ValueError) as exc:
            self.release(f"Could not start control: {exc}")

    def release(self, reason: str = "Move into the preview to interact.") -> None:
        self.hover_blocked = reason != "Pointer left the preview"
        self.pending_preview_click = None
        try:
            self.input.stop(reason)
        except (OSError, RuntimeError) as exc:
            reason = f"{reason} · {exc}"
        try:
            self.pass_through.disable()
        except (OSError, RuntimeError) as exc:
            reason = f"{reason} · {exc}"
        self.active_geometry = None
        self.window_guard = None
        self.guard_feedback = ""
        self.cursor_position = None
        self.canvas.itemconfigure(self.cursor_ring, state="hidden")
        self.canvas.itemconfigure(self.cursor_dot, state="hidden")
        self.canvas.configure(highlightbackground="#34455e")
        self.status_label.configure(fg=MUTED)
        self.status.set(reason)

    def _draw_cursor(self) -> None:
        if self.cursor_position is None or self.preview is None or not self.input.active:
            return
        px, py = self.cursor_position
        x, y = self.preview[0] + px, self.preview[1] + py
        self.canvas.coords(self.cursor_ring, x - 9, y - 9, x + 9, y + 9)
        self.canvas.coords(self.cursor_dot, x - 2, y - 2, x + 2, y + 2)
        for item in (self.cursor_ring, self.cursor_dot):
            self.canvas.itemconfigure(item, state="normal")
            self.canvas.tag_raise(item)

    def _poll_input(self) -> None:
        if self.closed:
            return
        self.input.heartbeat()
        # Drain a bounded batch so heavy movement cannot starve Tk's event loop.
        for _ in range(500):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "move":
                self.cursor_position = value
            elif kind in {"released", "error"} and not self.input.active:
                # A queued release from an earlier session must not stop a newer one.
                self.release(str(value))
        if self.input.active and self.window_guard is not None:
            reason = self.window_guard.reason
            if reason != self.guard_feedback:
                self.guard_feedback = reason
                self.status.set(reason or "Interacting · Move out of the image to return.")
        self._draw_cursor()
        self.recover_button.configure(state="disabled" if self.input.active else "normal")
        self._poll_hover()
        self.root.after(10, self._poll_input)

    def _present_frame(self, frame: Frame, target: Rect) -> None:
        # All Tk work stays on the UI thread; capture and conversion are already done.
        photo = tk.PhotoImage(data=frame.ppm, format="PPM", master=self.root)
        self.canvas.itemconfigure(self.image_item, image=photo)
        self.canvas.coords(self.image_item, self.preview[0], self.preview[1])
        self.image = photo
        self.last_target = target
        self.frame_ready = True
        self.last_frame_at = time.monotonic()
        self.last_capture_at = frame.captured_at
        self.canvas.itemconfigure(self.placeholder, state="hidden")
        self._draw_cursor()

    def _tick(self) -> None:
        if self.closed:
            return
        began = time.monotonic()
        visible = self.root.state() in {"normal", "zoomed"}
        self.preview_timer.set_active(visible)
        if not visible:
            self.capture.pause()
            self.capture_started_at = self.healthy_since = None
            self.frame_ready = False
            if self.input.active:
                self.release("Released because the viewer was hidden")
            self.root.after(100, self._tick)
            return
        if began < self.settle_until:
            self.capture.pause()
            self.capture_started_at = self.healthy_since = None
            self.root.after(50, self._tick)
            return
        if time.perf_counter() < self.retry_at:
            self.root.after(100, self._tick)
            return
        capturing = False
        try:
            if began - self.last_topology > 1:
                self.last_topology = began
                self._update_monitors()
                if began < self.settle_until:
                    self.root.after(50, self._tick)
                    return
            target = self._target_rect()
            selected = self._selected()
            if not isinstance(selected, AppWindow) and overlaps(target, self._viewer_rect()):
                # Never hide the viewer with WDA_EXCLUDEFROMCAPTURE: that also
                # removes it from remote-desktop clients. Pause this recursive
                # display preview instead; window capture still works here.
                self.capture.pause()
                self.capture_started_at = self.healthy_since = None
                self.frame_ready = False
                if self.input.active:
                    self.release("Released because the viewer overlaps the selected display")
                self.image = None
                self.canvas.itemconfigure(self.image_item, image="")
                self.canvas.itemconfigure(
                    self.placeholder, state="normal", width=max(300, self.canvas.winfo_width() - 60),
                    text="Choose Applications above and select a window\n"
                    "to control it on this screen.\n\n"
                    "To view a whole display, move this viewer to a different display.",
                )
                self.status.set("Preview paused · Viewer is on the selected display.")
                self.recovering = True
                self.root.after(100, self._tick)
                return
            available = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
            next_preview = fit_preview((target[2], target[3]), available)
            old_preview = self.preview
            self.preview = next_preview
            preview = self._preview_rect()
            scene = self.window_scene if isinstance(selected, AppWindow) else None
            if scene is not None and self.input.active:
                if self.window_guard.presented != scene:
                    self.window_guard.presented = None
                self.preview = old_preview  # Keep cursor and old image aligned until presentation.
            if self.input.active and (
                (scene is None and self.active_geometry != (target, preview))
                or (not isinstance(selected, AppWindow) and overlaps(target, self._viewer_rect()))
                or self.root.state() not in {"normal", "zoomed"}
            ):
                self.release("Released because the display or viewer moved/resized")
            if self.root.state() in {"normal", "zoomed"}:
                capturing = True
                if self.capture_started_at is None:
                    self.capture_started_at = time.perf_counter()
                if self.capture.request(
                    (*target, next_preview[2], next_preview[3]),
                    window=selected.key if isinstance(selected, AppWindow) else None,
                    scene=scene,
                ):
                    self.frame_ready = False
                frame = self.capture.take()
                if frame is not None and time.perf_counter() - frame.captured_at < .5:
                    self.preview = next_preview
                    self._present_frame(frame, target)
                    if scene is not None and scene != self.presented_scene and self.input.active:
                        self.pass_through.raise_viewer()
                    self.presented_scene = frame.scene
                    self.capture_started_at = time.perf_counter()
                    if self.recovering:
                        self.status.set("Preview recovered · Move out and back into the image to resume.")
                        self.recovering = False
                    self.recovery_exhausted = False
                    if self.healthy_since is None:
                        self.healthy_since = time.perf_counter()
                    elif time.perf_counter() - self.healthy_since >= 2:
                        self.recovery_attempts = 0
                    if scene is not None and self.input.active:
                        if self.active_geometry != (target, preview):
                            self.input.retarget(target, preview)
                            self.active_geometry = target, preview
                        self.window_guard.presented = frame.scene
                if ((self.frame_ready or self.input.active)
                        and time.perf_counter() - self.last_capture_at > .5):
                    self.frame_ready = False
                    self.healthy_since = None
                    self.release("Released because the preview stopped updating")
                # Initial WGC session setup can take several seconds. Input still
                # releases after .5s of stale pixels; allow 5s before rebuilding.
                if time.perf_counter() - self.capture_started_at > 5:
                    self._retry_capture("Preview stopped updating")
        except (OSError, RuntimeError, ValueError, tk.TclError) as exc:
            if capturing:
                self._retry_capture(f"Capture unavailable: {exc}")
                self.root.after(100, self._tick)
                return
            self.capture.pause()
            self.capture_started_at = self.healthy_since = None
            self.frame_ready = False
            self.recovering = True
            if self.input.active or self.active_geometry is not None:
                self.release(f"Capture unavailable: {exc}")
            else:
                self.status.set(f"Capture unavailable: {exc}")
            self._clear_preview(str(exc))
            self.root.after(200, self._tick)
            return
        # Poll only for completed frames. Never block Tk on desktop readback, and
        # never replay a backlog when the UI was busy with another window/event.
        self.root.after(2, self._tick)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.discovery.close()
        try:
            self.input.close()
        finally:
            try:
                self.pass_through.disable()
            finally:
                try:
                    self.capture.close()
                finally:
                    try:
                        self.preview_timer.close()
                    finally:
                        self.root.destroy()


def launch(*, practice: bool = False, fps: int = 60, monitor: int | None = None) -> None:
    """Run the viewer. Enter a ready preview to interact; leave it to return."""
    if not 1 <= fps <= 60:
        raise ValueError("fps must be between 1 and 60")
    enable_dpi_awareness()
    root = tk.Tk()
    app: MonitorApp | None = None
    try:
        app = MonitorApp(root, practice=practice, fps=fps, monitor=monitor)
        root.mainloop()
    except KeyboardInterrupt:
        pass
    except tk.TclError as exc:
        raise RuntimeError(f"The Windows viewer could not open: {exc}") from exc
    finally:
        if app is not None:
            app.close()
        else:
            root.destroy()
