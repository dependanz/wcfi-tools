"""Guard device loading with a stateful fake of Live's browser and selection APIs.

These tests exercise the real adapter/RPC. They never connect to a running Live
instance, install a remote script, use audio, or control the desktop.
"""
import json
import threading
import urllib.request
from types import SimpleNamespace

import pytest
from test_ableton_bridge import Device, Parameter, Song, Track, pump

from wcfi_tools.ableton.remote.adapter import BridgeError, LiveAdapter
from wcfi_tools.ableton.remote.server import BridgeServer


class TrackView:
    def __init__(self, device=None):
        self.selected_device = device
        self._insert_mode = 2
        self.writes = []

    @property
    def device_insert_mode(self):
        return self._insert_mode

    @device_insert_mode.setter
    def device_insert_mode(self, value):
        self.writes.append(value)
        self._insert_mode = value


class BooleanTrackView(TrackView):
    """Live 11's boolean mode must be preserved without invoking its setter."""

    @property
    def device_insert_mode(self):
        return True

    @device_insert_mode.setter
    def device_insert_mode(self, value):
        self.writes.append(value)
        raise AssertionError("Do not write a boolean insertion-mode property")


class LoadingTrack(Track):
    def __init__(self, name, devices=()):
        super().__init__(name, devices)
        self.view = TrackView(self.devices[-1] if self.devices else None)
        self.deletions = []
        self.delete_behavior = "normal"

    def delete_device(self, index):
        self.deletions.append((threading.get_ident(), index))
        if self.delete_behavior == "throw":
            raise RuntimeError("host removal failed")
        if self.delete_behavior == "wrong":
            index = 0
        self.devices.pop(index)


class SongView:
    def __init__(self, song):
        self.song = song
        self._selected_track = song.tracks[0]
        self.writes = []

    @property
    def selected_track(self):
        return self._selected_track

    @selected_track.setter
    def selected_track(self, value):
        self.writes.append(("track", value))
        self._selected_track = value

    def select_device(self, device, appoint=True):
        self.writes.append(("device", device, appoint))
        if device is None:
            self._selected_track.view.selected_device = None
            return
        for track in self.song.all_tracks:
            if any(device in container.devices for container in self.song._containers(track)):
                self._selected_track = track
                track.view.selected_device = device
                return
        raise RuntimeError("device is no longer in this song")


class LoadingSong(Song):
    def __init__(self):
        super().__init__()
        self.tracks = [LoadingTrack("RED", [Device("Before"), Device("After")]),
                       LoadingTrack("KIT", [Device("Before"), Device("After")])]
        self.return_tracks = [LoadingTrack("A Reverb")]
        self.master_track = LoadingTrack("Master", [Device("Before"), Device("After")])
        self.record_mode = False
        self.playing = False
        self.view = SongView(self)

    @property
    def all_tracks(self):
        return self.tracks + self.return_tracks + [self.master_track]

    @property
    def is_playing(self):
        return self.playing

    @is_playing.setter
    def is_playing(self, _value):
        raise AssertionError("Transport must never be changed")


def browser_item(name, *, children=(), loadable=False):
    return SimpleNamespace(name=name, uri="fake:audio-effects/" + name,
                           children=list(children), is_loadable=loadable,
                           is_folder=not loadable)


class LoadingBrowser:
    def __init__(self, song):
        self.song = song
        self.audio_effects = browser_item("Audio Effects", children=[
            browser_item("Utility", loadable=True),
            browser_item("Dynamics", children=[browser_item("Compressor", loadable=True)]),
            browser_item("EQ Eight", loadable=True),
        ])
        self._hotswap_target = None
        self.hotswap_writes = []
        self.calls = []
        self.behavior = "normal"

    @property
    def hotswap_target(self):
        return self._hotswap_target

    @hotswap_target.setter
    def hotswap_target(self, value):
        self.hotswap_writes.append(value)
        self._hotswap_target = value

    def load_item(self, item):
        self.calls.append((threading.get_ident(), item))
        target = self.song.view.selected_track
        container = target
        # Live's current device/chain context can route a load into a nested rack.
        # A guarded loader must select a top-level anchor before invoking load_item.
        anchor = target.view.selected_device
        for possible in self.song._containers(target):
            if anchor in possible.devices:
                container = possible
                break
        if self.behavior == "wrong_target":
            container = self.song.tracks[1]
        new = Device(item.name)
        new.parameters.append(Parameter("Stereo Width", 1, 0, 4))
        if self.behavior == "replace":
            container.devices[-1] = new
        else:
            container.devices.append(new)
        if self.behavior == "multiple":
            container.devices.append(Device("Unexpected second effect"))
        if self.behavior == "collateral":
            self.song.tracks[1].devices.reverse()
        if self.behavior == "transport_started":
            self.song.playing = True
        if self.behavior == "rack":
            new.chains = [LoadingTrack("New chain", [Device("New child")])]
            new.can_have_chains = True
        if self.behavior == "throw":
            raise RuntimeError("load failed after adding an effect")
        target.view.selected_device = new


@pytest.fixture
def insertion_host():
    song = LoadingSong()
    browser = LoadingBrowser(song)
    adapter = LiveAdapter(song, SimpleNamespace(browser=browser))
    return song, browser, adapter


def inspect(insertion_host):
    return insertion_host[2].inspect_session()


def listing(insertion_host, **kwargs):
    adapter = insertion_host[2]
    return adapter.execute("list_browser_items", {"session_id": adapter.session_id, **kwargs})


def insertion_arguments(insertion_host, target_index=0, index=1):
    state = inspect(insertion_host)
    target = state["tracks"][target_index]
    item = listing(insertion_host)["items"][0]
    return {"session_id": state["session_id"], "item_id": item["id"],
            "target_id": target["id"], "index": index,
            "expected_target_order": [device["id"] for device in target["devices"]]}


def insert(insertion_host, target_index=0, index=1, **overrides):
    args = insertion_arguments(insertion_host, target_index, index)
    args.update(overrides)
    return insertion_host[2].execute("insert_device", args)


def remove(insertion_host, result, **kwargs):
    adapter = insertion_host[2]
    return adapter.execute("remove_inserted_device", {
        "session_id": adapter.session_id, "insertion_id": result["insertion_id"], **kwargs})


def assert_error(code, function):
    with pytest.raises(BridgeError) as exc:
        function()
    assert exc.value.code == code


def view_state(song):
    return (song.view.selected_track,
            [(track.view.selected_device, track.view.device_insert_mode)
             for track in song.all_tracks])


def context_arguments(insertion_host, target_index=3):
    state = inspect(insertion_host)
    target = state["tracks"][target_index]
    return {"session_id": state["session_id"], "target_id": target["id"],
            "expected_target_order": [device["id"] for device in target["devices"]]}


def probe_context(insertion_host, target_index=3, **overrides):
    args = context_arguments(insertion_host, target_index)
    args.update(overrides)
    return insertion_host[2].execute("probe_insertion_context", args)


def assert_no_audio_mutations(insertion_host):
    song, browser, _adapter = insertion_host
    assert not browser.calls and not song.moves
    for track in song.all_tracks:
        assert not track.deletions
        assert not track.mixer_device.volume.writes
        assert not track.mixer_device.panning.writes
        for container in song._containers(track):
            for device in container.devices:
                assert all(not parameter.writes for parameter in device.parameters)
    assert not song.is_playing and not song.record_mode


def test_context_inspection_is_read_only_even_when_mutations_are_latched(insertion_host):
    song, browser, adapter = insertion_host
    args = context_arguments(insertion_host)
    del args["expected_target_order"]
    before = view_state(song)
    adapter.device_browser.uncertain = True
    result = adapter.execute("inspect_insertion_context", args)
    assert result["session_id"] == adapter.session_id
    assert isinstance(result["context"], dict)
    json.dumps(result, allow_nan=False)
    assert view_state(song) == before
    assert not song.view.writes and not browser.hotswap_writes
    assert not any(track.view.writes for track in song.all_tracks)
    assert not adapter.device_browser.receipts
    assert_no_audio_mutations(insertion_host)


@pytest.mark.parametrize("target_index", [0, 2, 3])
def test_context_probe_restores_view_without_loading_or_changing_audio(insertion_host, target_index):
    song, _browser, adapter = insertion_host
    before = inspect(insertion_host)
    old_view = view_state(song)
    result = probe_context(insertion_host, target_index)
    assert result["context_only"] and result["changed"] is False
    assert result["verified"] and result["selection_restored"]
    assert inspect(insertion_host)["tracks"] == before["tracks"]
    assert view_state(song) == old_view
    assert not adapter.device_browser.receipts and song.undo_steps == 0
    trace = result["context_trace"]
    assert 3 <= len(trace) <= 24
    assert trace[0]["stage"] == "before"
    assert all(isinstance(entry["context"], dict) for entry in trace)
    stages = [entry["stage"] for entry in trace]
    assert "select target track" in stages
    assert "set loading insertion mode" in stages
    assert "restore insertion mode" in stages
    assert "load item" not in stages
    json.dumps(result, allow_nan=False)
    assert_no_audio_mutations(insertion_host)


def test_context_probe_dry_run_has_no_selection_or_undo_writes(insertion_host):
    song, browser, adapter = insertion_host
    before = view_state(song)
    result = probe_context(insertion_host, dry_run=True)
    assert result["dry_run"] and result["context_only"]
    assert result["context_trace"][0]["stage"] == "before"
    assert view_state(song) == before
    assert not song.view.writes and not browser.hotswap_writes
    assert not any(track.view.writes for track in song.all_tracks)
    assert not adapter.device_browser.receipts and song.undo_steps == 0
    assert_no_audio_mutations(insertion_host)


@pytest.mark.parametrize("flag", ["playing", "record_mode"])
def test_context_probe_requires_stopped_transport(insertion_host, flag):
    song, browser, _adapter = insertion_host
    setattr(song, flag, True)
    assert_error("transport_active", lambda: probe_context(insertion_host))
    assert_error("transport_active", lambda: probe_context(insertion_host, dry_run=True))
    assert not song.view.writes and not browser.calls


def test_context_probe_rejects_conflicts_stale_session_and_uncertainty(insertion_host):
    song, browser, adapter = insertion_host
    assert_error("conflict", lambda: probe_context(insertion_host, expected_target_order=[]))
    assert_error("invalid_argument", lambda: probe_context(
        insertion_host, expected_target_order=None))
    assert_error("stale_session", lambda: probe_context(insertion_host, session_id="old"))
    adapter.device_browser.uncertain = True
    assert_error("insertion_unresolved", lambda: probe_context(insertion_host))
    assert_error("insertion_unresolved", lambda: probe_context(insertion_host, dry_run=True))
    assert not song.view.writes and not browser.calls


def test_context_probe_retains_insertion_mode_reset_regression(insertion_host):
    song, _browser, _adapter = insertion_host
    before = view_state(song)
    select = song.view.select_device

    def select_and_reset_mode(device, appoint=True):
        select(device, appoint)
        song.view.selected_track.view.device_insert_mode = 0

    song.view.select_device = select_and_reset_mode
    result = probe_context(insertion_host)
    assert result["verified"] and result["selection_restored"]
    assert view_state(song) == before
    assert_no_audio_mutations(insertion_host)


def test_context_probe_never_invokes_audio_mutation_apis(insertion_host):
    song, browser, _adapter = insertion_host

    def forbidden(*_args, **_kwargs):
        pytest.fail("A context-only probe invoked an audio graph mutation")

    browser.load_item = forbidden
    song.move_device = forbidden
    for track in song.all_tracks:
        track.delete_device = forbidden
    result = probe_context(insertion_host)
    assert result["verified"] and result["changed"] is False
    assert_no_audio_mutations(insertion_host)


def configure_boolean_mode_context(insertion_host, target_index, boolean_on):
    song, _browser, _adapter = insertion_host
    target = song.all_tracks[target_index]
    original = song.view.selected_track
    if boolean_on in ("target", "both"):
        target.view = BooleanTrackView(target.view.selected_device)
    if boolean_on in ("original", "both"):
        original.view = BooleanTrackView(original.view.selected_device)
    song.view.selected_chain = None
    select = song.view.select_device

    def select_with_chain_context(device, appoint=True):
        select(device, appoint)
        song.view.selected_chain = None
        for track in song.all_tracks:
            for container in song._containers(track):
                if container is not track and device in container.devices:
                    song.view.selected_chain = container

    song.view.select_device = select_with_chain_context
    return target


@pytest.mark.parametrize("target_index,boolean_on,host_position,index", [
    (0, "target", "before", 0),
    (0, "target", "after", 4),
    (0, "target", "end", 2),
    (3, "target", "before", 4),
    (3, "target", "after", 0),
    (3, "original", "end", 2),
    (3, "original", "before", 4),
    (3, "both", "after", 2),
    (3, "both", "end", 0),
])
def test_boolean_mode_preserves_both_modes_and_verifies_host_placement(
        insertion_host, target_index, boolean_on, host_position, index):
    song, browser, _adapter = insertion_host
    target = configure_boolean_mode_context(insertion_host, target_index, boolean_on)
    nested = LoadingTrack("Nested", [Device("Inner effect")])
    trailing_nested = LoadingTrack("Trailing nested", [Device("Inner trailing effect")])
    target.devices = [Device("First leaf"), Device("Rack", [nested]),
                      Device("Last leaf"), Device("Trailing rack", [trailing_nested])]
    target.view.selected_device = trailing_nested.devices[0]
    if song.view.selected_track is target:
        song.view.selected_chain = trailing_nested
    original_tracks = inspect(insertion_host)["tracks"]
    original_view = view_state(song)
    original_chain = song.view.selected_chain
    load = browser.load_item
    observed_anchors = []

    def load_at_host_position(item):
        anchor = target.view.selected_device
        observed_anchors.append(anchor)
        assert anchor is target.devices[2] and anchor.name == "Last leaf"
        assert song.view.selected_chain is None
        anchor_index = target.devices.index(anchor)
        load(item)
        new = target.devices.pop()
        offset = {"before": anchor_index, "after": anchor_index + 1,
                  "end": len(target.devices)}[host_position]
        target.devices.insert(offset, new)

    browser.load_item = load_at_host_position
    probe = probe_context(insertion_host, target_index)
    assert probe["verified"] and probe["changed"] is False
    assert probe["insertion_mode_written"] is False
    assert probe["insertion_mode_policy"] == "preserve_host_mode"
    assert not browser.calls and view_state(song) == original_view
    result = insert(insertion_host, target_index=target_index, index=index)
    assert result["verified"] and result["selection_restored"]
    assert result["insertion_mode_written"] is False
    assert result["insertion_mode_policy"] == "preserve_host_mode"
    assert target.devices[index].name == "Utility" and len(observed_anchors) == 1
    assert view_state(song) == original_view and song.view.selected_chain is original_chain
    assert all(not track.view.writes for track in song.all_tracks)
    assert remove(insertion_host, result)["verified"]
    assert inspect(insertion_host)["tracks"] == original_tracks
    assert all(not track.view.writes for track in song.all_tracks)


@pytest.mark.parametrize("target_index,boolean_on", [(0, "target"), (3, "original")])
def test_boolean_mode_dry_run_does_not_write_any_view(insertion_host, target_index, boolean_on):
    song, browser, _adapter = insertion_host
    configure_boolean_mode_context(insertion_host, target_index, boolean_on)
    before = view_state(song)
    for result in (probe_context(insertion_host, target_index, dry_run=True),
                   insert(insertion_host, target_index=target_index, dry_run=True)):
        assert result["dry_run"] and result["insertion_mode_written"] is False
        assert result["insertion_mode_policy"] == "preserve_host_mode"
    assert view_state(song) == before
    assert not song.view.writes and not browser.hotswap_writes
    assert all(not track.view.writes for track in song.all_tracks)
    assert_no_audio_mutations(insertion_host)


@pytest.mark.parametrize("operation", ["insert", "probe"])
def test_boolean_mode_rack_only_target_rejected_before_view_writes(insertion_host, operation):
    song, browser, _adapter = insertion_host
    target = configure_boolean_mode_context(insertion_host, 3, "target")
    nested = LoadingTrack("Nested", [Device("Inner effect")])
    target.devices = [Device("Only rack", [nested])]
    target.view.selected_device = nested.devices[0]
    before = view_state(song)
    if operation == "insert":
        assert_error("unsupported", lambda: insert(insertion_host, target_index=3))
    else:
        assert_error("unsupported", lambda: probe_context(insertion_host, 3))
    assert view_state(song) == before
    assert not song.view.writes and not browser.hotswap_writes
    assert all(not track.view.writes for track in song.all_tracks)
    assert_no_audio_mutations(insertion_host)


def test_boolean_mode_empty_target_can_insert_without_any_mode_write(insertion_host):
    song, browser, _adapter = insertion_host
    target = configure_boolean_mode_context(insertion_host, 2, "target")
    assert not target.devices and song.view.selected_chain is None
    assert probe_context(insertion_host, 2)["verified"]
    result = insert(insertion_host, target_index=2, index=0)
    assert result["verified"] and result["insertion_mode_written"] is False
    assert result["insertion_mode_policy"] == "preserve_host_mode"
    assert len(browser.calls) == 1 and target.devices[0].name == "Utility"
    assert all(not track.view.writes for track in song.all_tracks)
    assert remove(insertion_host, result)["verified"] and not target.devices


@pytest.mark.parametrize("bad_context", ["selected_chain", "nested_selection"])
def test_boolean_mode_unresolved_nested_context_never_loads(insertion_host, bad_context):
    song, browser, adapter = insertion_host
    target = configure_boolean_mode_context(insertion_host, 3, "target")
    nested = LoadingTrack("Unresolved nested", [Device("Inner effect")])
    target.devices.append(Device("Trailing rack", [nested]))
    select = song.view.select_device

    def select_without_resolving_context(device, appoint=True):
        select(device, appoint)
        if song.view.selected_track is target:
            if bad_context == "selected_chain":
                song.view.selected_chain = nested
            else:
                target.view.selected_device = nested.devices[0]

    song.view.select_device = select_without_resolving_context
    with pytest.raises(BridgeError):
        insert(insertion_host, target_index=3)
    assert not browser.calls
    assert adapter.status()["capabilities"]["insertion_outcome_unresolved"]
    assert all(not track.view.writes for track in song.all_tracks)


def test_discovery_paginates_stable_opaque_ids_and_descends(insertion_host):
    page = listing(insertion_host, limit=2)
    assert page["total"] == 3 and page["next_offset"] == 2
    assert [item["name"] for item in page["items"]] == ["Utility", "Dynamics"]
    assert all(item["id"].startswith("browser_") for item in page["items"])
    assert all(item["id"] != item["uri"] for item in page["items"])
    assert listing(insertion_host, limit=2)["items"] == page["items"]
    assert listing(insertion_host, offset=2)["next_offset"] is None
    child = listing(insertion_host, parent_id=page["items"][1]["id"])["items"][0]
    assert child["name"] == "Compressor" and child["is_loadable"]
    assert child["path"] == ["Dynamics", "Compressor"]
    assert not insertion_host[1].calls


@pytest.mark.parametrize("kwargs", [
    {"root": "instruments"}, {"root": "plugins"}, {"offset": -1},
    {"offset": True}, {"limit": 0}, {"limit": 101}, {"limit": True},
])
def test_discovery_rejects_unbounded_or_wrong_roots(insertion_host, kwargs):
    assert_error("invalid_argument", lambda: listing(insertion_host, **kwargs))


def test_stale_browser_identity_and_changed_paths_are_rejected(insertion_host):
    args = insertion_arguments(insertion_host)
    browser, adapter = insertion_host[1:]
    assert_error("stale_browser_item", lambda: insert(insertion_host, item_id="fake:Utility"))
    browser.audio_effects.children.reverse()
    assert_error("stale_browser_item", lambda: adapter.execute("insert_device", args))
    assert not browser.calls
    assert_error("stale_session", lambda: listing(insertion_host, session_id="old"))


def test_browser_item_ids_do_not_cross_bridge_sessions(insertion_host):
    args = insertion_arguments(insertion_host)
    song, browser, _adapter = insertion_host
    other = LiveAdapter(song, SimpleNamespace(browser=browser))
    state = other.inspect_session()
    args.update(session_id=state["session_id"], target_id=state["tracks"][0]["id"],
                expected_target_order=[d["id"] for d in state["tracks"][0]["devices"]])
    assert_error("stale_browser_item", lambda: other.execute("insert_device", args))
    assert not browser.calls


def test_nonloadable_folder_and_order_conflict_never_load(insertion_host):
    folder = listing(insertion_host)["items"][1]["id"]
    assert_error("invalid_target", lambda: insert(insertion_host, item_id=folder))
    assert_error("conflict", lambda: insert(insertion_host, expected_target_order=[]))
    for index in (-1, 3, True):
        assert_error("out_of_range", lambda index=index: insert(insertion_host, index=index))
    assert not insertion_host[1].calls


@pytest.mark.parametrize("flag", ["playing", "record_mode"])
def test_transport_must_be_stopped_even_for_dry_runs(insertion_host, flag):
    setattr(insertion_host[0], flag, True)
    assert_error("transport_active", lambda: insert(insertion_host, dry_run=True))
    assert_error("transport_active", lambda: insert(insertion_host))
    assert not insertion_host[1].calls


def test_missing_record_state_fails_closed(insertion_host):
    del insertion_host[0].record_mode
    assert_error("transport_active", lambda: insert(insertion_host))
    assert not insertion_host[1].calls


def test_dry_run_has_no_selection_loader_or_undo_writes(insertion_host):
    song, browser, _adapter = insertion_host
    before = view_state(song)
    result = insert(insertion_host, target_index=3, dry_run=True)
    assert result["dry_run"] and view_state(song) == before
    assert not browser.calls and not browser.hotswap_writes and not song.view.writes
    assert not any(track.view.writes for track in song.all_tracks)
    assert song.undo_steps == 0


@pytest.mark.parametrize("target_index,index", [(0, 0), (0, 1), (0, 2), (2, 0), (3, 1)])
def test_loads_one_effect_at_exact_index_and_restores_selection(insertion_host, target_index, index):
    song, browser, _adapter = insertion_host
    before = view_state(song)
    target = song.all_tracks[target_index]
    old_devices = list(target.devices)
    result = insert(insertion_host, target_index=target_index, index=index)
    assert result["verified"] and result["selection_restored"]
    assert target.devices[index].name == "Utility"
    assert target.devices[:index] + target.devices[index + 1:] == old_devices
    if old_devices:
        assert view_state(song) == before
    else:
        assert result["previously_empty_target"]
        assert result["target_device_selection_restored"] is False
        assert song.view.selected_track is before[0]
        assert target.view.selected_device is target.devices[index]
        assert target.view.device_insert_mode == before[1][target_index][1]
    assert browser.hotswap_target is None and song.undo_steps == 0
    assert len(browser.calls) == 1 and browser.calls[0][0] == threading.get_ident()
    if index != len(old_devices):
        assert song.moves[-1][3] == index


def test_nested_selected_device_cannot_redirect_top_level_insertion(insertion_host):
    song, _browser, _adapter = insertion_host
    nested = LoadingTrack("Inner", [Device("Nested effect")])
    rack = Device("Rack", [nested])
    song.tracks[0].devices.insert(1, rack)
    song.tracks[0].view.selected_device = nested.devices[0]
    before = view_state(song)
    result = insert(insertion_host, index=2)
    assert result["verified"]
    assert [device.name for device in nested.devices] == ["Nested effect"]
    assert song.tracks[0].devices[2].name == "Utility"
    assert view_state(song) == before


def test_loading_currently_selected_empty_track_reports_selection_limit(insertion_host):
    song, _browser, adapter = insertion_host
    target = song.return_tracks[0]
    song.view.selected_track = target
    assert target.view.selected_device is None
    result = insert(insertion_host, target_index=2, index=0)
    assert result["verified"]
    assert result["previously_empty_target"]
    assert result["target_device_selection_restored"] is False
    assert result["selected_device_restored"] is False
    assert result["selection_restored"] is False
    assert result["selection_note"]
    assert song.view.selected_track is target
    assert target.view.selected_device is target.devices[0]
    # A known host limitation must not be falsely reported as an uncertain load.
    assert not adapter.status()["capabilities"]["insertion_outcome_unresolved"]


def test_selection_restore_failure_still_attempts_other_cleanup(insertion_host):
    song, browser, adapter = insertion_host
    original_track = song.view.selected_track
    old_device = song.master_track.view.selected_device
    old_mode = song.master_track.view.device_insert_mode
    select = song.view.select_device

    def fail_restore(device, appoint=True):
        if browser.calls and device is old_device:
            raise RuntimeError("device selection restoration failed")
        return select(device, appoint)

    song.view.select_device = fail_restore
    result = insert(insertion_host, target_index=3)
    assert not result["verified"] and not result["selection_restored"]
    assert result["selection_errors"]
    assert song.view.selected_track is original_track
    assert song.master_track.view.device_insert_mode == old_mode
    assert browser.hotswap_target is None and song.undo_steps == 0
    assert adapter.status()["capabilities"]["insertion_outcome_unresolved"]
    assert_error("insertion_unresolved", lambda: insert(insertion_host))


def test_insertion_modes_restore_after_selection_changes_them(insertion_host):
    song, _browser, _adapter = insertion_host
    original = view_state(song)
    select = song.view.select_device

    def select_and_reset_mode(device, appoint=True):
        select(device, appoint)
        # Observed host behavior: selection can reset the track's insertion mode.
        song.view.selected_track.view.device_insert_mode = 0

    song.view.select_device = select_and_reset_mode
    result = insert(insertion_host, target_index=3, index=0)
    assert result["verified"] and result["selection_restored"]
    assert view_state(song) == original
    assert song.undo_steps == 0


def test_cleanup_getter_exception_closes_undo_and_latches_uncertainty(insertion_host):
    song, browser, adapter = insertion_host

    class FailingReadView(SongView):
        fail_reads = False

        @property
        def selected_track(self):
            if self.fail_reads:
                raise RuntimeError("selected-track wrapper expired")
            return self._selected_track

        @selected_track.setter
        def selected_track(self, value):
            self.writes.append(("track", value))
            self._selected_track = value

    song.view = FailingReadView(song)
    load = browser.load_item

    def load_then_invalidate_view(item):
        load(item)
        song.view.fail_reads = True

    browser.load_item = load_then_invalidate_view
    try:
        result = insert(insertion_host)
    except BridgeError as exc:
        assert exc.code == "insertion_outcome_unknown"
    else:
        assert not result["verified"] and not result["selection_restored"]
        assert result["selection_errors"]
    assert [device.name for device in song.tracks[0].devices] == ["Before", "Utility", "After"]
    assert song.undo_steps == 0
    assert adapter.status()["capabilities"]["insertion_outcome_unresolved"]
    assert_error("insertion_unresolved", lambda: insert(insertion_host))


def test_selection_failure_allows_only_owned_cleanup_and_keeps_view_latch(insertion_host):
    song, browser, adapter = insertion_host
    target = song.master_track
    original = list(target.devices)
    old_device = target.view.selected_device
    select = song.view.select_device

    def fail_target_device_restore(device, appoint=True):
        if browser.calls and device is old_device:
            raise RuntimeError("cannot restore target device selection")
        return select(device, appoint)

    song.view.select_device = fail_target_device_restore
    result = insert(insertion_host, target_index=3)
    assert not result["verified"] and result["selection_errors"]
    assert adapter.status()["capabilities"]["insertion_outcome_unresolved"]
    assert_error("insertion_unresolved", lambda: insert(insertion_host))
    state = inspect(insertion_host)
    parameter = state["tracks"][0]["devices"][0]["parameters"][1]
    mutation = {"session_id": adapter.session_id, "parameter_id": parameter["id"],
                "value": -1, "expected_value": parameter["value"]}
    assert_error("insertion_unresolved", lambda: adapter.execute("set_parameter", mutation))
    assert remove(insertion_host, result, dry_run=True)["dry_run"]
    assert not target.deletions
    assert remove(insertion_host, result)["verified"]
    assert target.devices == original
    # Removing the known device does not repair the failed selection API.
    assert adapter.status()["capabilities"]["insertion_outcome_unresolved"]
    assert_error("insertion_unresolved", lambda: insert(insertion_host))
    assert_error("insertion_unresolved", lambda: adapter.execute("set_parameter", mutation))


def test_hotswap_busy_does_not_disrupt_user_operation(insertion_host):
    song, browser, _adapter = insertion_host
    target = song.tracks[0].devices[0]
    browser.hotswap_target = target
    before = view_state(song)
    assert_error("browser_busy", lambda: insert(insertion_host))
    assert browser.hotswap_target is target and view_state(song) == before
    assert not browser.calls


@pytest.mark.parametrize("behavior", ["wrong_target", "replace", "multiple", "throw", "collateral"])
def test_uncertain_load_is_not_retryable_or_reported_as_success(insertion_host, behavior):
    song, browser, adapter = insertion_host
    browser.behavior = behavior
    assert_error("insertion_outcome_unknown", lambda: insert(insertion_host))
    assert adapter.status()["capabilities"]["insertion_outcome_unresolved"]
    assert_error("insertion_unresolved", lambda: insert(insertion_host))
    assert len(browser.calls) == 1 and song.undo_steps == 0


def test_transport_change_during_load_is_uncertain_and_not_undone_by_bridge(insertion_host):
    song, browser, adapter = insertion_host
    browser.behavior = "transport_started"
    assert_error("insertion_outcome_unknown", lambda: insert(insertion_host))
    assert song.is_playing  # Bridge must never stop Play to make its operation pass.
    assert adapter.status()["capabilities"]["insertion_outcome_unresolved"]
    assert song.undo_steps == 0


def test_new_rack_may_add_its_own_chains_but_remains_receipt_owned(insertion_host):
    song, browser, _adapter = insertion_host
    original = list(song.tracks[0].devices)
    browser.behavior = "rack"
    result = insert(insertion_host)
    assert result["verified"]
    assert song.tracks[0].devices[1].chains[0].devices[0].name == "New child"
    assert remove(insertion_host, result)["verified"]
    assert song.tracks[0].devices == original


def test_nearest_move_is_not_exact_success_but_can_be_rolled_back(insertion_host):
    song, _browser, adapter = insertion_host
    original = list(song.tracks[0].devices)
    song.nearest = True
    result = insert(insertion_host, index=1)
    assert not result["verified"] and result["actual_index"] == 0
    assert adapter.status()["capabilities"]["insertion_outcome_unresolved"]
    assert_error("insertion_unresolved", lambda: insert(insertion_host))
    assert remove(insertion_host, result)["verified"]
    assert song.tracks[0].devices == original
    assert not adapter.status()["capabilities"]["insertion_outcome_unresolved"]
    song.nearest = False
    assert insert(insertion_host)["verified"]


def test_uncertain_insertion_blocks_parameter_fader_and_move(insertion_host):
    song, browser, adapter = insertion_host
    browser.behavior = "throw"
    assert_error("insertion_outcome_unknown", lambda: insert(insertion_host))
    state = inspect(insertion_host)
    target = state["tracks"][0]
    parameter = target["devices"][0]["parameters"][1]
    order = [device["id"] for device in target["devices"]]
    operations = [
        ("set_parameter", {"parameter_id": parameter["id"], "value": -1,
                           "expected_value": parameter["value"]}),
        ("set_fader", {"track_id": target["id"], "db": -6,
                       "expected_value": song.tracks[0].mixer_device.volume.value}),
        ("move_device", {"device_id": target["devices"][0]["id"],
                         "target_id": target["id"], "index": 1,
                         "expected_source_order": order, "expected_target_order": order}),
    ]
    for operation, arguments in operations:
        assert_error("insertion_unresolved", lambda operation=operation, arguments=arguments:
                     adapter.execute(operation, {
            "session_id": adapter.session_id, **arguments}))


def test_receipt_removal_is_narrow_dry_run_and_idempotent(insertion_host):
    song, _browser, adapter = insertion_host
    original = list(song.tracks[0].devices)
    result = insert(insertion_host)
    assert remove(insertion_host, result, dry_run=True)["dry_run"]
    assert not song.tracks[0].deletions
    assert_error("stale_insertion", lambda: adapter.execute("remove_inserted_device", {
        "session_id": adapter.session_id,
        "insertion_id": inspect(insertion_host)["tracks"][0]["devices"][0]["id"]}))
    removed = remove(insertion_host, result)
    assert removed["verified"] and removed["changed"]
    assert song.tracks[0].devices == original
    repeated = remove(insertion_host, result)
    assert repeated["already_removed"] and not repeated["changed"]
    assert len(song.tracks[0].deletions) == 1 and song.undo_steps == 0


@pytest.mark.parametrize("edit", ["parameter", "order", "name"])
def test_receipt_removal_preserves_concurrently_edited_device(insertion_host, edit):
    result = insert(insertion_host)
    track = insertion_host[0].tracks[0]
    if edit == "parameter":
        track.devices[1].parameters[1].value = -1
    elif edit == "order":
        track.devices.reverse()
    else:
        track.devices[1].name = "User changed this"
    assert_error("conflict", lambda: remove(insertion_host, result))
    assert not track.deletions


@pytest.mark.parametrize("behavior", ["throw", "wrong"])
def test_uncertain_removal_blocks_further_insertions(insertion_host, behavior):
    result = insert(insertion_host)
    song, _browser, adapter = insertion_host
    song.tracks[0].delete_behavior = behavior
    try:
        removal = remove(insertion_host, result)
    except (BridgeError, RuntimeError):
        pass
    else:
        assert not removal["verified"]
    assert adapter.status()["capabilities"]["insertion_outcome_unresolved"]
    assert_error("insertion_unresolved", lambda: insert(insertion_host))
    assert song.undo_steps == 0


@pytest.mark.parametrize("deletion_behavior", ["normal", "throw"])
@pytest.mark.parametrize("closure_completed", [False, True])
def test_removal_undo_failure_preserves_errors_and_never_retries(
        insertion_host, deletion_behavior, closure_completed):
    result = insert(insertion_host)
    song, _browser, adapter = insertion_host
    target = song.tracks[0]
    target.delete_behavior = deletion_behavior
    end = song.end_undo_step
    closure_attempts = []

    def fail_end():
        closure_attempts.append(True)
        if closure_completed:
            end()
        raise RuntimeError("undo closure failed")

    song.end_undo_step = fail_end
    with pytest.raises(BridgeError) as exc:
        remove(insertion_host, result)
    assert exc.value.code == "removal_outcome_unknown"
    assert exc.value.details["undo_closure_error"] == "undo closure failed"
    if deletion_behavior == "throw":
        assert exc.value.details["deletion_error"] == "host removal failed"
        assert any(device.name == "Utility" for device in target.devices)
    else:
        assert "deletion_error" not in exc.value.details
        assert all(device.name != "Utility" for device in target.devices)
    assert closure_attempts == [True]
    assert song.undo_steps == (0 if closure_completed else 1)
    assert adapter.status()["capabilities"]["insertion_outcome_unresolved"]
    assert adapter.device_browser.recoverable_receipt is None
    assert_error("insertion_unresolved", lambda: insert(insertion_host))
    assert_error("insertion_unresolved", lambda: remove(insertion_host, result))
    state = inspect(insertion_host)
    parameter = state["tracks"][0]["devices"][0]["parameters"][1]
    assert_error("insertion_unresolved", lambda: adapter.execute("set_parameter", {
        "session_id": adapter.session_id, "parameter_id": parameter["id"],
        "value": -1, "expected_value": parameter["value"]}))
    assert len(target.deletions) == 1 and closure_attempts == [True]


def test_real_http_duplicate_insertion_executes_once_on_main_thread(insertion_host, tmp_path):
    _song, browser, adapter = insertion_host
    arguments = insertion_arguments(insertion_host)
    server = BridgeServer(adapter, tmp_path)
    try:
        body = json.dumps({"id": "insert-unique-1234567890", "operation": "insert_device",
                           "arguments": arguments}).encode("utf-8")

        def request():
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server.server_port}/v1/rpc",
                data=body, headers={"Authorization": "Bearer " + server.server.token,
                                    "Content-Type": "application/json"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=2) as response:
                return json.load(response)

        first = pump(server, request)
        second = pump(server, request)
        assert first["ok"] and first == second
        assert first["result"]["verified"]
        assert len(browser.calls) == 1
        assert browser.calls[0][0] == threading.get_ident()
    finally:
        server.close()


@pytest.mark.parametrize("target_index", [0, 2])
def test_native_insertion_mode_enum_is_preserved(insertion_host, target_index):
    class Mode:
        pass

    Mode.default, Mode.selected_right = Mode(), Mode()
    song = insertion_host[0]
    view = song.all_tracks[target_index].view
    view._insert_mode = Mode.default
    result = insert(insertion_host, target_index=target_index, index=0)
    assert result["verified"]
    assert all(isinstance(value, Mode) for value in view.writes)
    assert view.device_insert_mode is Mode.default


def test_unexpected_diagnostic_failure_cannot_interrupt_context_cleanup(insertion_host, monkeypatch):
    song, _browser, _adapter = insertion_host
    before = view_state(song)

    def fail_trace(*_args):
        raise RuntimeError("unexpected diagnostic failure")

    monkeypatch.setattr("wcfi_tools.ableton.remote.browser.context_snapshot", fail_trace)
    result = probe_context(insertion_host)
    assert result["verified"] and result["selection_restored"]
    assert view_state(song) == before and song.undo_steps == 0
    assert all(stage["context"]["errors"] for stage in result["context_trace"])


def test_cleanup_does_not_use_raising_track_inequality(insertion_host):
    class TrackWithBrokenInequality(LoadingTrack):
        def __ne__(self, _other):
            raise RuntimeError("inequality is unavailable")

    song, _browser, _adapter = insertion_host
    original = TrackWithBrokenInequality("RED", [Device("Before"), Device("After")])
    song.tracks[0] = original
    song.view._selected_track = original
    before = view_state(song)
    result = probe_context(insertion_host)
    assert result["verified"] and result["selection_restored"]
    assert view_state(song) == before and song.undo_steps == 0
