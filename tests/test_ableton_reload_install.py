"""Versioned installation never activates partial or unverified bridge code."""
import hashlib
import importlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

installer = importlib.import_module("wcfi_tools.ableton.install")


@pytest.fixture
def project(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for name in installer.FILES:
        (source / name).write_text('"""Standalone ' + name + '."""\n', encoding="utf-8")
    library = tmp_path / "library"
    library.mkdir()
    state_dir = tmp_path / "state"
    monkeypatch.setattr(installer, "REMOTE_DIRECTORY", source)
    return source, library, state_dir


def deploy(project):
    _, library, state_dir = project
    return installer.install(user_library=library, state_dir=state_dir)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_bundle_layout_content_address_and_repeated_install(project):
    source, _, _ = project
    result = deploy(project)
    root = Path(result["installed"])
    bundle = root / "runtimes" / result["bundle_id"]
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
              for name in installer.RUNTIME_FILES}
    expected_id = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert result["bundle_id"] == expected_id
    assert read_json(bundle / "manifest.json") == {"schema": 1, "bundle_id": expected_id, "files": hashes}
    assert {entry.name for entry in bundle.iterdir()} == set(installer.RUNTIME_FILES) | {"manifest.json"}
    for name in installer.RUNTIME_FILES:
        assert (bundle / name).read_bytes() == (source / name).read_bytes()
        assert not (root / name).exists()
    for name in installer.BOOTSTRAP_FILES:
        assert (root / name).read_bytes() == (source / name).read_bytes()
    pointer = read_json(root / "runtime-current.json")
    assert pointer["bundle_id"] == expected_id
    assert pointer["bootstrap_hashes"] == {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                                             for name in installer.BOOTSTRAP_FILES}
    assert result["restart_required"]
    original_manifest = (bundle / "manifest.json").stat().st_mtime_ns
    repeated = deploy(project)
    assert repeated["bundle_id"] == expected_id
    assert not repeated["restart_required"]
    assert (bundle / "manifest.json").stat().st_mtime_ns == original_manifest


def test_runtime_update_preserves_old_bundle_and_needs_only_reload(project):
    source, _, _ = project
    first = deploy(project)
    root = Path(first["installed"])
    old = root / "runtimes" / first["bundle_id"]
    old_bytes = (old / "adapter.py").read_bytes()
    (source / "adapter.py").write_text("REVISION = 2\n")
    second = deploy(project)
    assert second["bundle_id"] != first["bundle_id"]
    assert not second["restart_required"]
    assert "wcfi audio ableton reload" in second["activation"]
    assert (old / "adapter.py").read_bytes() == old_bytes
    assert read_json(root / "runtime-current.json")["bundle_id"] == second["bundle_id"]


@pytest.mark.parametrize("name", installer.BOOTSTRAP_FILES)
def test_bootstrap_update_requires_one_restart(project, name):
    source, _, _ = project
    first = deploy(project)
    (source / name).write_text("BOOTSTRAP = 2\n")
    second = deploy(project)
    assert second["bundle_id"] == first["bundle_id"]
    assert second["restart_required"]
    assert "restart Live once" in second["activation"]


@pytest.mark.parametrize("name", ["surface.py", "bridge-home.json", "runtime-current.json"])
def test_modified_managed_file_blocks_any_overwrite(project, name):
    result = deploy(project)
    root = Path(result["installed"])
    (root / name).write_text("local user changes")
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    with pytest.raises(RuntimeError, match="modified or is missing"):
        deploy(project)
    assert {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("damage", ["modified", "extra", "manifest", "missing"])
def test_existing_bundle_must_be_exact_and_immutable(project, damage):
    result = deploy(project)
    root = Path(result["installed"])
    bundle = root / "runtimes" / result["bundle_id"]
    pointer_before = (root / "runtime-current.json").read_bytes()
    if damage == "modified":
        (bundle / "adapter.py").write_text("tampered")
    elif damage == "extra":
        (bundle / "untracked.py").write_text("unexpected")
    elif damage == "manifest":
        (bundle / "manifest.json").write_text("{}")
    else:
        (bundle / "server.py").unlink()
    with pytest.raises(RuntimeError, match="Runtime bundle|Existing runtime activation"):
        deploy(project)
    assert (root / "runtime-current.json").read_bytes() == pointer_before


def test_legacy_migration_verifies_and_preserves_flat_runtime_files(project):
    source, library, state_dir = project
    root = library / "Remote Scripts" / installer.PACKAGE_NAME
    root.mkdir(parents=True)
    legacy = {name: (source / name).read_bytes() for name in installer.FILES if name != "runtime.py"}
    legacy["bridge-home.json"] = json.dumps({"state_dir": str(state_dir)}).encode()
    for name, data in legacy.items():
        (root / name).write_bytes(data)
    (root / "wcfi-managed.json").write_text(json.dumps({
        "owner": "wcfi-tools", "protocol": 1,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in legacy.items()},
    }))
    result = deploy(project)
    assert result["restart_required"]
    for name in installer.RUNTIME_FILES:
        assert (root / name).read_bytes() == legacy[name]
    managed = read_json(root / "wcfi-managed.json")["files"]
    assert set(managed) == set(installer.BOOTSTRAP_FILES) | {"runtime-current.json", "bridge-home.json"}


def test_untracked_bootstrap_file_is_never_overwritten(project):
    _, library, _ = project
    root = library / "Remote Scripts" / installer.PACKAGE_NAME
    root.mkdir(parents=True)
    (root / "wcfi-managed.json").write_text('{"owner":"wcfi-tools","files":{}}')
    (root / "runtime.py").write_text("user file")
    with pytest.raises(RuntimeError, match="untracked"):
        deploy(project)
    assert (root / "runtime.py").read_text() == "user file"
    assert not (root / "runtimes").exists()


@pytest.mark.parametrize("name", ["../outside.py", "nested/file.py", "unknown.py"])
def test_managed_manifest_cannot_escape_allowlist(project, name):
    _, library, _ = project
    root = library / "Remote Scripts" / installer.PACKAGE_NAME
    root.mkdir(parents=True)
    (root / "wcfi-managed.json").write_text(json.dumps({
        "owner": "wcfi-tools", "files": {name: "0" * 64},
    }))
    with pytest.raises(RuntimeError, match="manifest is invalid"):
        deploy(project)


def test_invalid_runtime_syntax_does_not_publish_anything(project):
    source, library, _ = project
    (source / "adapter.py").write_text("match x:\n    case 1: pass\n")
    with pytest.raises(SyntaxError):
        deploy(project)
    assert not (library / "Remote Scripts").exists()


def test_pointer_publication_failure_restores_previous_root_files(project, monkeypatch):
    source, _, _ = project
    first = deploy(project)
    root = Path(first["installed"])
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    (source / "adapter.py").write_text("REVISION = 2\n")
    (source / "surface.py").write_text("BOOTSTRAP = 2\n")
    original_write = installer._write

    def fail_pointer(path, data):
        if path == root / "runtime-current.json":
            raise OSError("simulated pointer write failure")
        original_write(path, data)

    monkeypatch.setattr(installer, "_write", fail_pointer)
    with pytest.raises(OSError, match="simulated pointer"):
        deploy(project)
    assert {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()} == before
    assert not list((root / "runtimes").glob(".staging-*"))


def test_staging_failure_never_activates_incomplete_bundle(project, monkeypatch):
    source, _, _ = project
    first = deploy(project)
    root = Path(first["installed"])
    pointer_before = (root / "runtime-current.json").read_bytes()
    (source / "adapter.py").write_text("REVISION = 2\n")
    original_write = installer._write

    def fail_runtime(path, data):
        if path.name == "browser.py":
            raise OSError("simulated staging failure")
        original_write(path, data)

    monkeypatch.setattr(installer, "_write", fail_runtime)
    with pytest.raises(OSError, match="staging failure"):
        deploy(project)
    assert (root / "runtime-current.json").read_bytes() == pointer_before
    assert [entry.name for entry in (root / "runtimes").iterdir()] == [first["bundle_id"]]


def test_linked_remote_scripts_parent_is_rejected(project, tmp_path):
    _, library, _ = project
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    try:
        (library / "Remote Scripts").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or elevated privileges")
    with pytest.raises(RuntimeError, match="linked path"):
        deploy(project)
    assert list(elsewhere.iterdir()) == []


def test_bundle_file_link_is_rejected_without_following_it(project, tmp_path):
    result = deploy(project)
    root = Path(result["installed"])
    bundle = root / "runtimes" / result["bundle_id"]
    external = tmp_path / "external.py"
    external.write_bytes((bundle / "adapter.py").read_bytes())
    (bundle / "adapter.py").unlink()
    try:
        (bundle / "adapter.py").symlink_to(external)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or elevated privileges")
    with pytest.raises(RuntimeError, match="linked path"):
        deploy(project)


def test_windows_junction_attribute_is_rejected_before_directory_creation(project, monkeypatch):
    _, library, _ = project
    junction = library / "Remote Scripts"
    original_lstat = Path.lstat

    def fake_lstat(path):
        if path == junction:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(RuntimeError, match="linked path"):
        deploy(project)
    assert not junction.exists()


def test_new_bundle_does_not_hide_corrupted_previous_runtime(project):
    source, _, _ = project
    first = deploy(project)
    root = Path(first["installed"])
    previous_bundle = root / "runtimes" / first["bundle_id"]
    (previous_bundle / "adapter.py").write_text("unexpected modification")
    (source / "adapter.py").write_text("REVISION = 2\n")
    with pytest.raises(RuntimeError, match="Runtime bundle file was modified"):
        deploy(project)
    assert len(list((root / "runtimes").iterdir())) == 1


def test_concurrent_edit_during_staging_is_preserved(project, monkeypatch):
    source, _, _ = project
    first = deploy(project)
    root = Path(first["installed"])
    pointer_before = (root / "runtime-current.json").read_bytes()
    (source / "surface.py").write_text("BOOTSTRAP = 2\n")
    original_stage = installer._stage_bundle

    def concurrent_edit(destination, contents):
        bundle_id = original_stage(destination, contents)
        (destination / "surface.py").write_text("user changed this during install")
        return bundle_id

    monkeypatch.setattr(installer, "_stage_bundle", concurrent_edit)
    with pytest.raises(RuntimeError, match="changed during installation"):
        deploy(project)
    assert (root / "surface.py").read_text() == "user changed this during install"
    assert (root / "runtime-current.json").read_bytes() == pointer_before


def test_metadata_publication_failure_restores_previous_activation(project, monkeypatch):
    source, _, _ = project
    first = deploy(project)
    root = Path(first["installed"])
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    (source / "adapter.py").write_text("REVISION = 2\n")
    original_write = installer._write

    def fail_metadata(path, data):
        if path == root / "wcfi-managed.json":
            raise OSError("simulated metadata write failure")
        original_write(path, data)

    monkeypatch.setattr(installer, "_write", fail_metadata)
    with pytest.raises(OSError, match="metadata write failure"):
        deploy(project)
    assert {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()} == before


def test_atomic_write_does_not_remove_temporary_file_it_did_not_create(tmp_path, monkeypatch):
    destination = tmp_path / "runtime-current.json"
    destination.write_bytes(b"original activation")
    collision = tmp_path / "runtime-current.json.fixed.tmp"
    collision.write_bytes(b"owned by another installer")
    monkeypatch.setattr(installer.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    with pytest.raises(FileExistsError):
        installer._write(destination, b"new activation")
    assert destination.read_bytes() == b"original activation"
    assert collision.read_bytes() == b"owned by another installer"


def test_atomic_write_cleans_up_its_own_file_on_failure(tmp_path, monkeypatch):
    destination = tmp_path / "runtime-current.json"
    destination.write_bytes(b"original activation")
    monkeypatch.setattr(installer.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))

    def fail_sync(_descriptor):
        raise OSError("simulated sync failure")

    monkeypatch.setattr(installer.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="simulated sync failure"):
        installer._write(destination, b"new activation")
    assert destination.read_bytes() == b"original activation"
    assert not (tmp_path / "runtime-current.json.fixed.tmp").exists()
