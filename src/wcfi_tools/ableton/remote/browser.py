"""Bounded audio-effect discovery and guarded insertion on Live's main thread.

Only descendants of Live's audio_effects root can be loaded. No paths, arbitrary
URIs, samples, instruments, track presets, generic deletion or transport calls.
"""
import copy
import uuid

from .adapter import BridgeError, readable
from .insertion_context import context_snapshot


class DeviceBrowser:
    MAX_ITEMS = 4096
    MAX_RECEIPTS = 64

    def __init__(self, adapter):
        self.adapter = adapter
        self.items = {}
        self.paths = {}
        self.receipts = {}
        self.uncertain = False
        self.recoverable_receipt = None

    def _browser(self):
        browser = readable(self.adapter.application, "browser")
        if browser is None:
            raise BridgeError("unsupported", "Live does not expose a device browser")
        return browser

    def capabilities(self):
        browser = readable(self.adapter.application, "browser")
        return {"browser_audio_effects": readable(browser, "audio_effects") is not None,
                "insert_device": callable(readable(browser, "load_item")),
                "insertion_roots": ["audio_effects"],
                "insertion_requires_stopped_transport": True,
                "remove_inserted_device": True,
                "insertion_context_diagnostics": True,
                "insertion_outcome_unresolved": self.uncertain}

    def _root(self, root):
        if root != "audio_effects":
            raise BridgeError("invalid_argument", "Only the audio_effects browser root is supported")
        item = readable(self._browser(), root)
        if item is None:
            raise BridgeError("unsupported", "Live's audio effects browser is unavailable")
        return item

    @staticmethod
    def _signature(item):
        return (str(readable(item, "name", "")), str(readable(item, "uri", "")))

    def _resolve(self, record):
        item = self._root(record["root"])
        for index, name, uri in record["path"]:
            children = tuple(readable(item, "children", ()) or ())
            if index >= len(children) or self._signature(children[index]) != (name, uri):
                raise BridgeError("stale_browser_item", "Browser contents changed; browse again")
            item = children[index]
        return item

    def _item(self, identifier):
        if not isinstance(identifier, str) or identifier not in self.items:
            raise BridgeError("stale_browser_item", "Use a browser item ID from this bridge session")
        record = self.items[identifier]
        return record, self._resolve(record)

    def list_items(self, session_id, root="audio_effects", parent_id=None, offset=0, limit=100):
        self.adapter._refresh(session_id)
        self._root(root)
        if (type(offset) is not int or offset < 0 or type(limit) is not int
                or not 1 <= limit <= 100):
            raise BridgeError("invalid_argument", "Use a nonnegative offset and limit 1..100")
        path = ()
        if parent_id is not None:
            record, parent = self._item(parent_id)
            if record["root"] != root:
                raise BridgeError("invalid_argument", "Parent belongs to another browser root")
            path = record["path"]
        else:
            parent = self._root(root)
        if len(path) >= 12:
            raise BridgeError("browser_limit", "Browser nesting exceeds 12 levels")
        children = tuple(readable(parent, "children", ()) or ())
        result = []
        for index in range(offset, min(offset + limit, len(children))):
            item = children[index]
            name, uri = self._signature(item)
            child_path = path + ((index, name, uri),)
            key = (root, child_path)
            identifier = self.paths.get(key)
            if identifier is None:
                if len(self.items) >= self.MAX_ITEMS:
                    raise BridgeError("browser_limit", "Browser item limit reached; reconnect the bridge")
                identifier = "browser_" + uuid.uuid4().hex
                self.items[identifier] = {"root": root, "path": child_path}
                self.paths[key] = identifier
            result.append({"id": identifier, "name": name, "uri": uri,
                "is_loadable": bool(readable(item, "is_loadable", False)),
                "is_folder": bool(readable(item, "is_folder", False)),
                "path": [part[1] for part in child_path]})
        next_offset = offset + len(result)
        return {"session_id": session_id, "root": root, "parent_id": parent_id,
                "items": result, "total": len(children),
                "next_offset": next_offset if next_offset < len(children) else None}

    def _stopped(self, allow_uncertain=False):
        song = self.adapter.song
        if readable(song, "is_playing") is not False or readable(song, "record_mode") is not False:
            raise BridgeError("transport_active", "Device insertion/removal requires stopped Play and Record")
        if self.uncertain and not allow_uncertain:
            raise BridgeError("insertion_unresolved", "A previous load had an uncertain outcome; inspect the set and restart the bridge before another insertion")

    def _orders(self):
        return {key: list(value["device_ids"]) for key, value in self.adapter.containers.items()}

    def _track(self, target_id):
        target = self.adapter.registry.get(target_id, "track")
        tracks = tuple(self.adapter.song.tracks) + tuple(self.adapter.song.return_tracks)
        tracks += (self.adapter.song.master_track,)
        if target not in tracks:
            raise BridgeError("invalid_target", "Insertion currently targets a track, return or Master")
        return target

    @staticmethod
    def _device_state(snapshot, identifier):
        def descend(container):
            for device in container["devices"]:
                if device["id"] == identifier:
                    return copy.deepcopy(device)
                for chain in device["chains"]:
                    found = descend(chain)
                    if found is not None:
                        return found
            return None
        for track in snapshot["tracks"]:
            found = descend(track)
            if found is not None:
                return found
        return None

    @staticmethod
    def _child_chains(device):
        result = set()
        for chain in device.get("chains", ()):
            result.add(chain["id"])
            for child in chain["devices"]:
                result.update(DeviceBrowser._child_chains(child))
        return result

    def _insertion_structure(self, before_orders, target_id, order, device):
        after = self._orders()
        expected = dict(before_orders)
        expected[target_id] = order
        return (set(after) == set(expected) | self._child_chains(device)
                and all(after.get(key) == value for key, value in expected.items()))

    def inspect_context(self, session_id, target_id):
        self.adapter._refresh(session_id)
        return {"session_id": session_id, "context": context_snapshot(
            self.adapter, self._track(target_id))}

    def probe_context(self, session_id, target_id, expected_target_order, dry_run=False):
        if not isinstance(expected_target_order, list):
            raise BridgeError("invalid_argument", "Expected the inspected destination device order")
        return self._insert(session_id, None, target_id, len(expected_target_order),
                            expected_target_order, dry_run, context_only=True)

    def insert(self, session_id, item_id, target_id, index, expected_target_order, dry_run=False):
        return self._insert(session_id, item_id, target_id, index, expected_target_order, dry_run)

    def _insert(self, session_id, item_id, target_id, index, expected_target_order,
                dry_run=False, context_only=False):
        self.adapter._refresh(session_id)
        self._stopped()
        target = self._track(target_id)
        before_orders = self._orders()
        before = before_orders[target_id]
        if before != expected_target_order:
            raise BridgeError("conflict", "Destination device order changed since inspection")
        if type(index) is not int or not 0 <= index <= len(before):
            raise BridgeError("out_of_range", "Index must be a zero-based insertion position")
        record, item = None, None
        if not context_only:
            record, item = self._item(item_id)
            if not readable(item, "is_loadable", False):
                raise BridgeError("invalid_target", "Choose a loadable audio effect browser item")
        browser, song = self._browser(), self.adapter.song
        view, target_view = readable(song, "view"), readable(target, "view")
        loader, mover, remover = (readable(browser, "load_item"),
            readable(song, "move_device"), readable(target, "delete_device"))
        if (not all(callable(fn) for fn in (loader, mover, remover)) or view is None
                or target_view is None or not hasattr(target_view, "device_insert_mode")
                or not callable(readable(view, "select_device"))
                or not hasattr(browser, "hotswap_target")):
            raise BridgeError("unsupported", "Live lacks the guarded loading, placement or removal API")
        if not context_only and len(self.receipts) >= self.MAX_RECEIPTS:
            raise BridgeError("insertion_limit", "Insertion receipt limit reached")
        # Hot-swap can replace an existing device. Never repurpose an active user operation.
        if browser.hotswap_target is not None:
            raise BridgeError("browser_busy", "Exit Live's Hot-Swap mode before inserting an effect")
        selected_track = view.selected_track
        selected_is_target = bool(selected_track == target)
        selected_view = readable(selected_track, "view")
        selected_device = readable(selected_view, "selected_device")
        has_selected_mode = hasattr(selected_view, "device_insert_mode")
        selected_insert_mode = selected_view.device_insert_mode if has_selected_mode else None
        target_device = readable(target_view, "selected_device")
        has_chain = hasattr(view, "selected_chain")
        selected_chain = readable(view, "selected_chain")
        insert_mode = target_view.device_insert_mode
        # Live 11.3.35 can return bool here, despite the three-valued setter.
        # A boolean readback is lossy and must NEVER be written back as a mode.
        preserve_mode = type(insert_mode) is bool or (
            has_selected_mode and type(selected_insert_mode) is bool)
        anchor = target.devices[-1] if target.devices else None
        if preserve_mode and target.devices:
            leaves = [device for device in target.devices
                      if readable(device, "can_have_chains", True) is False]
            if not leaves:
                raise BridgeError("unsupported", "Boolean insertion-mode readback requires a top-level non-rack anchor; rack-only targets are not supported yet")
            anchor = leaves[-1]
        if preserve_mode and not has_chain:
            raise BridgeError("unsupported", "Cannot verify top-level selection without Live's selected_chain readback")
        plan = {"session_id": session_id, "item_id": item_id,
                "item_name": self._signature(item)[0] if item is not None else None,
                "item_path": [p[1] for p in record["path"]] if record is not None else None,
                "target_id": target_id, "requested_index": index,
                "before_order": before, "dry_run": bool(dry_run),
                "context_only": context_only,
                "insertion_mode_policy": "preserve_host_mode" if preserve_mode else "set_and_restore_exact_mode",
                "insertion_mode_written": False,
                "temporary_track_selection": True, "stopped_transport_required": True}
        trace = []
        plan["context_trace"] = trace

        def trace_context(stage):
            try:
                context = context_snapshot(self.adapter, target, selected_track)
            except Exception as exc:  # noqa: BLE001 - diagnostics must never interrupt restoration.
                context = {"errors": ["Unexpected diagnostic failure: " + type(exc).__name__]}
            trace.append({"stage": stage, "context": context})

        def set_mode(track_view, mode):
            track_view.device_insert_mode = mode
            plan["insertion_mode_written"] = True

        trace_context("before")
        if dry_run:
            return plan
        new_id, failure, selection_errors = None, None, []
        song.begin_undo_step()
        try:
            view.selected_track = target
            trace_context("select target track")
            if anchor is not None:
                # Match Live's own Push load path: select a TOP-LEVEL anchor,
                # not a previously selected device inside a rack chain.
                view.select_device(anchor, False)
                trace_context("select top-level anchor")
            if preserve_mode:
                if (not (view.selected_track == target) or view.selected_chain is not None
                        or (anchor is not None and not (target_view.selected_device == anchor))):
                    raise BridgeError("unsupported", "Cannot establish an unambiguous top-level insertion context")
                trace_context("preserve host insertion mode")
            elif anchor is not None:
                # Preserve Live's native enum type when its setter requires it.
                set_mode(target_view, readable(type(insert_mode), "selected_right", 2))
            else:
                set_mode(target_view, readable(type(insert_mode), "default", 0))
            if not preserve_mode:
                trace_context("set loading insertion mode")
            self._stopped()
            if not context_only:
                loader(item)
                trace_context("load item")
            snapshot = self.adapter.inspect_session()
            after_orders = self._orders()
            after = after_orders.get(target_id, [])
            if context_only:
                if after_orders != before_orders:
                    raise BridgeError("context_probe_unverified", "Device graph changed during the context-only check")
                self._stopped()
                plan.update(changed=False, verified=True, after_order=after)
            else:
                self._verify_loaded_device(plan, snapshot, before_orders, before, after,
                    target_id, target, index, mover)
                new_id = plan.get("device_id")
        except Exception as exc:  # noqa: BLE001 - preserve uncertain load outcomes, never blindly retry.
            failure = exc
            self.uncertain = True
        finally:
            # Restoration attempts are independent: one failed selected-device
            # setter must not prevent restoring the original track/context.
            actions = []
            if target_device is not None:
                actions.append(("target device", lambda: view.select_device(target_device, False)))
            actions.append(("selected track", lambda: setattr(view, "selected_track", selected_track)))
            if selected_device is not None:
                actions.append(("selected device", lambda: view.select_device(selected_device, False)))
            if has_chain:
                def restore_chain():
                    if view.selected_chain != selected_chain:
                        view.selected_chain = selected_chain
                actions.append(("selected chain", restore_chain))
            # Selecting a device can reset insertion mode in Live. Restore mode
            # only after every selected-track/device/chain operation has finished.
            actions.append(("hot-swap", lambda: setattr(browser, "hotswap_target", None)))
            if not preserve_mode:
                actions.append(("insertion mode", lambda: set_mode(target_view, insert_mode)))
                if has_selected_mode and not selected_is_target:
                    actions.append(("original track insertion mode", lambda:
                        set_mode(selected_view, selected_insert_mode)))
            try:
                for label, action in actions:
                    try:
                        action()
                    except Exception as exc:  # noqa: BLE001 - best-effort independent cleanup.
                        selection_errors.append(label + ": " + str(exc))
                    trace_context("restore " + label)
                checks = [("selected track", lambda: view.selected_track == selected_track),
                          ("insertion mode", lambda: target_view.device_insert_mode == insert_mode),
                          ("hot-swap", lambda: browser.hotswap_target is None)]
                if target_device is not None:
                    checks.append(("target device", lambda: target_view.selected_device == target_device))
                if selected_device is not None:
                    checks.append(("selected device", lambda: selected_track.view.selected_device == selected_device))
                if has_chain:
                    checks.append(("selected chain", lambda: view.selected_chain == selected_chain))
                if has_selected_mode and not selected_is_target:
                    checks.append(("original track insertion mode", lambda:
                        selected_view.device_insert_mode == selected_insert_mode))
                for label, check in checks:
                    try:
                        if not check():
                            selection_errors.append(label + " did not restore")
                    except Exception as exc:  # noqa: BLE001 - getters can fail after a load too.
                        selection_errors.append(label + " readback: " + str(exc))
            finally:
                try:
                    song.end_undo_step()
                except Exception as exc:  # noqa: BLE001 - surface uncertain undo closure.
                    selection_errors.append("undo closure: " + str(exc))
        if selection_errors:
            self.uncertain = True
            # A view error does not make a known device unsafe to remove. Permit
            # only this receipt's guarded cleanup, without clearing the view-error
            # latch afterward. Unknown load outcomes still have no exception.
            receipt_id = plan.get("insertion_id")
            if receipt_id and (plan.get("verified") or self.recoverable_receipt == receipt_id):
                self.recoverable_receipt = receipt_id
                self.receipts[receipt_id]["selection_unresolved"] = True
            else:
                self.recoverable_receipt = None
            plan.update(selection_restored=False, selection_errors=selection_errors, verified=False)
        else:
            plan["selection_restored"] = True
        plan["previously_empty_target"] = not before
        plan["target_device_selection_restored"] = readable(target_view, "selected_device") == target_device
        plan["selected_device_restored"] = readable(readable(selected_track, "view"), "selected_device") == selected_device
        if selected_device is None and not plan["selected_device_restored"]:
            plan["selection_restored"] = False
            plan["selection_note"] = "Previously empty selected track now selects its first device; Live exposes selected_device as read-only"
        if failure is not None:
            raise BridgeError("insertion_outcome_unknown", "Device operation did not finish with verified state; inspect before retrying",
                              {"reason": str(failure), "selection_errors": selection_errors,
                               "device_id": plan.get("device_id", new_id), "context_trace": trace})
        return plan

    def _verify_loaded_device(self, plan, snapshot, before_orders, before, after,
                              target_id, target, index, mover):
        added = [identifier for identifier in after if identifier not in before]
        # Loading must add exactly one top-level device and preserve all old objects.
        unchanged = (len(added) == 1 and len(after) == len(before) + 1
                     and [identifier for identifier in after if identifier not in added] == before)
        if unchanged:
            new_id = added[0]
            unchanged = self._insertion_structure(before_orders, target_id, after,
                self._device_state(snapshot, new_id))
        if not unchanged:
            self.uncertain = True
            raise BridgeError("insertion_unverified", "Load did not produce exactly one effect on the intended track; inspect before any further write", {"before_orders": before_orders, "after_orders": self._orders()})
        plan["device_id"] = new_id
        actual = self.adapter.locations[new_id][1]
        if actual != index:
            device = self.adapter.registry.get(new_id, "device")
            host_index = index + (1 if actual < index else 0)
            mover(device, target, host_index)
            snapshot = self.adapter.inspect_session()
        expected_order = list(before)
        expected_order.insert(index, new_id)
        verified = self._insertion_structure(before_orders, target_id, expected_order,
            self._device_state(snapshot, new_id))
        self._stopped()
        receipt_id = "insertion_" + uuid.uuid4().hex
        self.receipts[receipt_id] = {"target_id": target_id, "device_id": new_id,
            "before_order": list(before), "after_order": self._orders()[target_id],
            "device_state": self._device_state(snapshot, new_id), "removed": False}
        plan.update(insertion_id=receipt_id, device_id=new_id, changed=True,
                    actual_index=self.adapter.locations[new_id][1], verified=verified,
                    after_order=self._orders()[target_id])
        if not verified:
            self.uncertain = True
            if self._insertion_structure(before_orders, target_id, self._orders()[target_id],
                    self._device_state(snapshot, new_id)):
                self.recoverable_receipt = receipt_id

    def remove(self, session_id, insertion_id, dry_run=False):
        self.adapter._refresh(session_id)
        self._stopped(allow_uncertain=True)
        receipt = self.receipts.get(insertion_id)
        if receipt is None:
            raise BridgeError("stale_insertion", "Only devices inserted by this running bridge can be removed")
        if self.uncertain and self.recoverable_receipt != insertion_id:
            raise BridgeError("insertion_unresolved", "This unresolved operation has no safely removable receipt")
        if receipt["removed"]:
            return {"insertion_id": insertion_id, "verified": True, "changed": False,
                    "dry_run": bool(dry_run), "already_removed": True}
        target = self._track(receipt["target_id"])
        snapshot = self.adapter.inspect_session()
        current = self._orders().get(receipt["target_id"])
        if (current != receipt["after_order"] or
                self._device_state(snapshot, receipt["device_id"]) != receipt["device_state"]):
            raise BridgeError("conflict", "Inserted device or chain changed; removal withheld")
        remover = readable(target, "delete_device")
        if not callable(remover):
            raise BridgeError("unsupported", "Live does not expose device removal")
        result = {"insertion_id": insertion_id, "device_id": receipt["device_id"],
                  "target_id": receipt["target_id"], "dry_run": bool(dry_run)}
        if dry_run:
            return result
        before_orders = self._orders()
        child_chains = self._child_chains(receipt["device_state"])
        removal_errors = {}
        self.adapter.song.begin_undo_step()
        try:
            remover(current.index(receipt["device_id"]))
        except Exception as exc:  # noqa: BLE001 - any host failure makes deletion outcome uncertain.
            removal_errors["deletion_error"] = str(exc)
        finally:
            try:
                self.adapter.song.end_undo_step()
            except Exception as exc:  # noqa: BLE001 - closure may fail after deletion already succeeded.
                removal_errors["undo_closure_error"] = str(exc)
        if removal_errors:
            self.uncertain = True
            self.recoverable_receipt = None
            details = {"reason": "; ".join(removal_errors.values()),
                       "device_id": receipt["device_id"]}
            details.update(removal_errors)
            raise BridgeError("removal_outcome_unknown",
                              "Removal or undo closure raised an error; inspect before any further write",
                              details)
        try:
            self.adapter.inspect_session()
            expected = {key: value for key, value in before_orders.items() if key not in child_chains}
            expected[receipt["target_id"]] = receipt["before_order"]
            verified = self._orders() == expected
        except Exception as exc:  # noqa: BLE001 - reject any unverifiable post-deletion state.
            self.uncertain = True
            self.recoverable_receipt = None
            raise BridgeError("removal_outcome_unknown", "Cannot verify removal; inspect before further writes",
                              {"reason": str(exc)})
        receipt["removed"] = verified
        if not verified:
            self.uncertain = True
            self.recoverable_receipt = None
        elif self.recoverable_receipt == insertion_id:
            self.uncertain = bool(receipt.get("selection_unresolved"))
            self.recoverable_receipt = None
        result.update(verified=verified, changed=True,
                      after_order=self._orders().get(receipt["target_id"]))
        return result
