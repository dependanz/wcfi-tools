"""Opt-in visual scaling check using a generated window; never moves the pointer."""

import json
import tkinter as tk
from pathlib import Path

from wcfi_tools.monitor_control.capture import Capture, enable_dpi_awareness


def main():
    enable_dpi_awareness()
    root = tk.Tk()
    root.title("WCFI · Preview quality check")
    root.geometry("800x600+60+60")
    root.attributes("-topmost", True)
    canvas = tk.Canvas(root, bg="white", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_text(32, 30, anchor="w", text="Preview scaling · fine detail",
                       font=("Segoe UI", 22), fill="#14243b")
    for x in range(40, 760):
        canvas.create_line(x, 80, x, 160, fill="black" if x % 2 else "white")
    for index, size in enumerate((10, 12, 16, 20)):
        canvas.create_text(40, 210 + 55 * index, anchor="w", font=("Segoe UI", size),
                           text=f"{size} pt · Church presentation — Display 2 — 0123456789")
    for index, color in enumerate(("#256bc3", "#14926a", "#eea321", "#c24662")):
        canvas.create_rectangle(40 + index * 180, 465, 200 + index * 180, 545,
                                fill=color, outline="")
    result = {"ok": False}

    def check():
        try:
            bounds = (canvas.winfo_rootx(), canvas.winfo_rooty(), 800, 600, 400, 300)
            output = Path(__file__).resolve().parents[1] / "_work/monitor-control"
            output.mkdir(parents=True, exist_ok=True)
            with Capture() as capture:
                filtered = tk.PhotoImage(data=capture.grab(*bounds), format="PPM", master=root)
                capture._gdi32.SetStretchBltMode(capture._memory, 3)
                fast = tk.PhotoImage(data=capture.grab(*bounds), format="PPM", master=root)
            filtered.write(str(output / "preview-filtered.png"), format="png")
            fast.write(str(output / "preview-old-scaling.png"), format="png")
            filtered_pixel, fast_pixel = filtered.get(100, 55), fast.get(100, 55)
            # Alternating 1-pixel black/white strokes should average to gray,
            # rather than lose every other line and collapse to black or white.
            assert all(100 <= channel <= 155 for channel in filtered_pixel), filtered_pixel
            assert all(channel <= 20 or channel >= 235 for channel in fast_pixel), fast_pixel
            print(json.dumps({"filtered_stripes_rgb": filtered_pixel,
                              "old_stripes_rgb": fast_pixel, "artifacts": str(output)}))
            result["ok"] = True
        finally:
            root.destroy()

    root.after(350, check)
    root.mainloop()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
