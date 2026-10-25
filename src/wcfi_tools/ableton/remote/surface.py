"""Small Live-owned timer plus a network queue; no MIDI or UI dependencies."""
import json
from pathlib import Path

from _Framework.ControlSurface import ControlSurface

from .runtime import RuntimeController


class WCFIBridge(ControlSurface):
    def __init__(self, c_instance):
        super().__init__(c_instance)
        self._wcfi_runtime = None
        self._wcfi_closed = False
        try:
            config = json.loads(Path(__file__).with_name("bridge-home.json").read_text(encoding="utf-8"))
            self._wcfi_runtime = RuntimeController(self.song(), self.application(),
                                                   Path(__file__).parent, config["state_dir"])
            self.schedule_message(1, self._wcfi_tick)
            self.log_message("WCFI Bridge ready (no transport controls)")
        except Exception as exc:  # noqa: BLE001 -- an extension must not abort host startup
            if self._wcfi_runtime is not None:
                try:
                    self._wcfi_runtime.close()
                except Exception as cleanup_error:  # noqa: BLE001 -- preserve host startup
                    self.log_message("WCFI Bridge cleanup failed: " + str(cleanup_error))
                self._wcfi_runtime = None
            self.log_message("WCFI Bridge startup failed: " + str(exc))

    def _wcfi_tick(self):
        if not self._wcfi_closed and self._wcfi_runtime is not None:
            try:
                self._wcfi_runtime.tick()
            except Exception as exc:  # noqa: BLE001 -- isolate extension failures from Live
                self.log_message("WCFI Bridge timer failed: " + str(exc))
            finally:
                if not self._wcfi_closed:
                    self.schedule_message(1, self._wcfi_tick)

    def disconnect(self):
        self._wcfi_closed = True
        try:
            if self._wcfi_runtime is not None:
                self._wcfi_runtime.close()
        except Exception as exc:  # noqa: BLE001 -- base class still needs disconnect
            self.log_message("WCFI Bridge cleanup failed: " + str(exc))
        finally:
            super().disconnect()
