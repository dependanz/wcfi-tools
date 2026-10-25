"""App jobs, authenticated audition assets and explicit guarded gain application."""
from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from ..ableton.client import connect
from .review_apply import apply_gain, availability
from .takes import _manifest


class RehearsalReview:
    """One child; separate finite budgets for captures, KIT and reference studies."""

    def __init__(self, state_dir, library_root, *, client_factory=connect):
        self.root = (Path(state_dir) / "rehearsal-review").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.library_root = Path(library_root)
        self.client_factory = client_factory
        self.lock = threading.RLock()
        self.jobs = {}
        self.process = None
        self.thread = None
        self.closed = False
        # A restarted service must never silently retry an interrupted mutation.
        for folder in self._job_folders():
            path = folder / "job.json"
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if re.fullmatch(r"[0-9a-f]{32}", job["id"]) and path.parent == self.root / job["id"]:
                    if job["status"] == "working":
                        job.update(status="error", error="Previous analysis was interrupted; run it again")
                    self.jobs[job["id"]] = job
            except (ValueError, OSError, KeyError, TypeError):
                continue
        self._prune()

    def _save(self, job):
        path = self.root / job["id"] / "job.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(job, allow_nan=False), encoding="utf-8")
        tmp.replace(path)

    def snapshot(self, *, include_references=True, filter_reviewed=True, include_named_references=True,
                 include_balance=True, balance_schema=6, include_candidates=True, include_suites=True,
                 include_processing=True, include_diagnostics=True, include_presence=True, include_combined=True,
                 include_compression=True, include_overlap=True, include_red_lowmid=True, include_updated_reference=True,
                 include_full_band=True, include_full_mix_benchmark=True, include_balance_benchmark=True,
                 include_combined_benchmark=True):
        with self.lock:
            jobs = copy.deepcopy([j for j in reversed(self.jobs.values())
                                  if (include_diagnostics or j["source"] != "diagnostic")
                                  and (include_presence or j["source"] != "diagnostic"
                                       or j["reports"].get("KIT", {}).get("kind") != "wcfi_kit_presence_study")
                                  and (include_combined or j["source"] != "diagnostic"
                                       or j["reports"].get("KIT", {}).get("kind") != "wcfi_kit_combined_study")
                                  and (include_compression or j["source"] != "diagnostic"
                                       or j["reports"].get("KIT", {}).get("kind") != "wcfi_kit_compression_study")
                                  and (include_overlap or j["source"] != "diagnostic"
                                       or j["reports"].get("KIT", {}).get("kind") != "wcfi_kit_bass_study")
                                  and (include_red_lowmid or j["source"] != "diagnostic"
                                       or j["reports"].get("RED", {}).get("kind") != "wcfi_red_lowmid_study")
                                  and (include_updated_reference or j["source"] != "diagnostic"
                                       or j["reports"].get("MIX", {}).get("kind") != "wcfi_updated_reference_study")
                                  and (include_full_band or j["source"] != "diagnostic"
                                       or j["reports"].get("MIX", {}).get("kind") != "wcfi_full_band_study")
                                  and (include_full_mix_benchmark or j["source"] != "diagnostic"
                                       or j["reports"].get("MIX", {}).get("kind") != "wcfi_full_mix_benchmark")
                                  and (include_balance_benchmark or j["source"] != "diagnostic"
                                       or j["reports"].get("MIX", {}).get("kind") != "wcfi_source_balance_benchmark")
                                  and (include_combined_benchmark or j["source"] != "diagnostic"
                                       or j["reports"].get("MIX", {}).get("kind") != "wcfi_full_mix_combined_benchmark")
                                  and (include_suites or j["source"] != "suite")
                                  and (include_processing or j["source"] != "suite"
                                       or j["reports"]["MIX"]["kind"] != "wcfi_processing_study")
                                  and (include_references or j["source"] != "reference")
                                  and (include_named_references or j["source"] != "reference"
                                       or j["reports"]["MIX"].get("reference_key") != "reference")
                                  and (j["source"] != "balance" or include_balance
                                       and j.get("study_schema_version", 1) <= balance_schema)
                                  and (j["source"] != "balance" or include_candidates
                                       or j.get("performance_assessment", {}).get("include",
                                           j["reports"]["MIX"]["passage"].get("operator_approved", True)))])
            for job in jobs:
                if job["source"] == "reference":
                    report = job["reports"]["MIX"]
                    if "reference_assessments" in job or report.get("performance_confirmation_required"):
                        from .reference_study import reviewed_reference

                        reviewed = reviewed_reference(report, job.get("reference_assessments", {}))
                        if not filter_reviewed:
                            # An older already-open selector may point at a jam.
                            # Keep its options stable; audio eligibility is still enforced.
                            reviewed["pairs"] = report["pairs"]
                        job["reports"]["MIX"] = reviewed
                elif job["source"] == "study" and "performance_assessment" in job:
                    passage = job["reports"]["KIT"]["comparison"]["passage"]
                    passage["performance_review"] = job["performance_assessment"]
                    start, seconds = passage["start_seconds"], passage["duration_seconds"]
                    review = job["performance_assessment"]
                    classification = "Band performance" if review["include"] else "Unstructured jam" if review.get("classification") == "jam" else "Excluded passage"
                    passage["label"] = f"{classification} · {start:g}–{start+seconds:g}s"
                elif job["source"] == "balance" and "performance_assessment" in job:
                    passage = job["reports"]["MIX"]["passage"]
                    passage["operator_approved"] = job["performance_assessment"]["include"]
                    passage["performance_review"] = job["performance_assessment"]
                elif job["source"] == "suite":
                    if job["reports"]["MIX"]["kind"] == "wcfi_processing_study":
                        job["reports"]["MIX"]["processing_reviews"] = job.get("processing_assessments", {})
                    for passage in job["reports"]["MIX"]["passages"]:
                        passage["review"] = job.get("suite_assessments", {}).get(passage["id"], {
                            "include": passage["operator_approved"], "verdict": "unsure",
                            "reason": "Awaiting your judgment of the saved comparisons."})
            return {"jobs": jobs,
                    "busy": self.process is not None, "worker_budget": "One logical CPU, idle priority; no GPU or cloud"}

    def start(self, data):
        source = data.get("source")
        if source not in ("take", "nodes"):
            raise ValueError("Choose a recorded take or current Analysis Nodes")
        seconds, start = data.get("seconds", 10), data.get("start", 29)
        if (type(seconds) not in (int, float) or not math.isfinite(seconds) or not .5 <= seconds <= 20
                or type(start) not in (int, float) or not math.isfinite(start) or not 0 <= start <= 86400):
            raise ValueError("Use a 0.5–20 second window and a valid start time")
        request = {"source": source, "seconds": seconds, "start": start}
        if source == "take":
            folder, manifest = _manifest(self.library_root, data.get("take_id"))
            if start + seconds > manifest["frames"] / manifest["sample_rate"]:
                raise ValueError("Requested section extends beyond the take")
            request.update(take_path=str(folder), take_id=manifest["take_id"])
        with self.lock:
            if self.closed or self.process is not None:
                raise ValueError("An analysis is already running or the service is stopping")
            identifier = uuid.uuid4().hex
            folder = self.root / identifier
            folder.mkdir()
            (folder / "request.json").write_text(json.dumps(request), encoding="utf-8")
            job = {"id": identifier, "source": source, "status": "working", "started_at": time.time(),
                   "request": {k: v for k, v in request.items() if k != "take_path"}, "reports": {}, "applied": {}}
            self.jobs[identifier] = job
            self._save(job)
            env = dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                process = subprocess.Popen([sys.executable, "-m", "wcfi_tools.audio.review_worker", str(folder)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=env, creationflags=flags)
            except OSError as exc:
                job.update(status="error", error=str(exc))
                self._save(job)
                raise
            self.process = process
            self.thread = threading.Thread(target=self._finish, args=(job, process), daemon=True)
            self.thread.start()
            self._prune()
            return self.snapshot()

    def _finish(self, job, process):
        folder = self.root / job["id"]
        try:
            try:
                code = process.wait(timeout=100)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
                raise RuntimeError("Analysis worker exceeded its time budget") from None
            if code:
                error = folder / "error.json"
                raise RuntimeError(json.loads(error.read_text(encoding="utf-8"))["error"] if error.exists()
                                   else "Analysis was canceled or the worker stopped")
            reports = json.loads((folder / "result.json").read_text(encoding="utf-8"))
            for report in reports.values():
                report["apply"] = availability(report)
            with self.lock:
                job.update(status="ready", reports=reports, completed_at=time.time())
        except (ValueError, OSError, RuntimeError, KeyError) as exc:
            with self.lock:
                job.update(status="error", error=str(exc), completed_at=time.time())
        finally:
            with self.lock:
                self._save(job)
                self.process = None

    def _job_folders(self):
        return sorted((p for p in self.root.iterdir() if re.fullmatch(r"[0-9a-f]{32}", p.name)
                       and p.is_dir() and not p.is_symlink()), key=lambda p: p.stat().st_mtime)

    def _prune(self):
        # A new capture must not evict a prepared comparison. Both have finite budgets.
        paths = self._job_folders()
        def category(path):
            source = self.jobs.get(path.name, {}).get("source")
            return source if source in ("study", "reference", "balance", "suite", "diagnostic") else "capture"
        for kind in ("capture", "study", "reference", "balance", "suite", "diagnostic"):
            group = [p for p in paths if category(p) == kind]
            limit = 2 if kind == "suite" else 8 if kind == "balance" else 12 if kind == "diagnostic" else 3
            for folder in group[:-limit]:
                # Delete only feature-owned UUID directories within the resolved root.
                if folder.resolve().parent != self.root:
                    raise ValueError("Review storage escaped its root")
                self.jobs.pop(folder.name, None)
                shutil.rmtree(folder)

    def cancel(self):
        with self.lock:
            if self.process is not None:
                self.process.terminate()  # Only the child this manager created.
        return self.snapshot()

    def audio(self, identifier, track, variant):
        with self.lock:
            job = self.jobs.get(identifier)
            if not job or job["status"] != "ready" or track not in ("RED", "KIT", "MIX") or track not in job["reports"]:
                raise ValueError("Choose a completed analysis")
            report = job["reports"][track]
            if track == "MIX" and ("reference_assessments" in job or report.get("performance_confirmation_required")):
                from .reference_study import reference_source, reviewed_reference

                approved = reviewed_reference(report, job.get("reference_assessments", {}))["pairs"]
                allowed_keys = {f"reference_{p['id']}_{s}_matched" for p in approved for s in ("wcfi", reference_source(report))}
                if variant not in allowed_keys:
                    raise ValueError("This passage is not approved for musical reference comparison")
            file = report["audio"].get(variant)
            allowed = {f"{track}-{v}.wav" for v in ("before", "after", "candidate", "reference_matched", "candidate_matched", "gain")}
            if track == "KIT" and report.get("kind") == "wcfi_kit_context_study":
                from .kit_study import BYPASS_STUDY_AUDIO

                allowed |= {"KIT-"+v+".wav" for v in BYPASS_STUDY_AUDIO}
            if track == "KIT" and report.get("kind") == "wcfi_kit_diagnostic":
                from .kit_diagnostic import AUDIO

                allowed = set(AUDIO.values())
            if track == "KIT" and report.get("kind") in ("wcfi_kit_shelf_study", "wcfi_kit_presence_study", "wcfi_kit_combined_study"):
                from .kit_shelf import audio_keys

                allowed = {f"KIT-{v}.wav" for v in audio_keys(report["kind"])}
            if track == "KIT" and report.get("kind") == "wcfi_kit_compression_study":
                from .kit_compression import AUDIO_KEYS

                allowed = {f"KIT-{v}.wav" for v in AUDIO_KEYS}
            if track == "KIT" and report.get("kind") == "wcfi_kit_bass_study":
                from .kit_bass_study import AUDIO_KEYS as OVERLAP_AUDIO

                allowed = {f"KIT-{v}.wav" for v in OVERLAP_AUDIO}
            if track == "RED" and report.get("kind") == "wcfi_red_lowmid_study":
                from .red_lowmid import AUDIO_KEYS as RED_LOWMID_AUDIO

                allowed = {f"RED-{v}.wav" for v in RED_LOWMID_AUDIO}
            if track == "MIX" and report.get("kind") in ("wcfi_updated_reference_study", "wcfi_full_band_study"):
                from .updated_reference import AUDIO as UPDATED_REFERENCE_AUDIO

                allowed = set(UPDATED_REFERENCE_AUDIO.values())
            if track == "MIX" and report.get("kind") in ("wcfi_full_mix_benchmark", "wcfi_source_balance_benchmark", "wcfi_full_mix_combined_benchmark"):
                allowed = {f'MIX-fmb_{v["id"]}.wav' for s in report['sessions'] for v in s['ranked_candidates']+s['controls']}
                allowed |= {'MIX-fmb_bethel.wav', 'MIX-fmb_maverick.wav'}
            if track == "MIX" and report.get("kind") == "wcfi_reference_study":
                from .reference_study import reference_audio_keys

                allowed = {"MIX-"+v+".wav" for v in reference_audio_keys(report)}
            if track == "MIX" and report.get("kind") == "wcfi_mix_balance_study":
                from .mix_balance import balance_audio_keys

                allowed = {f"MIX-{v}.wav" for v in balance_audio_keys(job.get("study_schema_version", 1), report)}
            if track == "MIX" and report.get("kind") == "wcfi_rehearsal_suite":
                from .rehearsal_suite import suite_audio_keys

                allowed = {f"MIX-{v}.wav" for v in suite_audio_keys()}
            if track == "MIX" and report.get("kind") == "wcfi_processing_study":
                from .processing_study import processing_audio_keys

                allowed = {f"MIX-{v}.wav" for v in processing_audio_keys()}
            if file not in allowed:
                raise ValueError("Unknown audition variant")
            return (self.root / identifier / file).read_bytes()

    def import_diagnostic(self, data):
        """Publish a checksummed read-only report; never creates an apply proposal."""
        import hashlib

        from .kit_diagnostic import validate_package

        identifier = data.get("study_id", "")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("Choose a staged diagnostic identifier")
        staging = (self.root / "diagnostic-staging").resolve()
        folder = (staging / identifier).resolve()
        if folder.parent != staging or staging.parent != self.root:
            raise ValueError("Diagnostic staging escaped its root")
        package, digest = validate_package(folder)
        track, report = next(iter(package["reports"].items()))
        with self.lock:
            if self.closed or self.process is not None:
                raise ValueError("Wait until analysis finishes before importing diagnostics")
            if identifier in self.jobs:
                if self.jobs[identifier].get("study_digest") == digest:
                    return self.snapshot()
                raise ValueError("Diagnostic ID is already in use")
            target = self.root / identifier
            if target.exists():
                raise ValueError("Diagnostic output directory already exists")
            target.mkdir()
            try:
                for filename, expected in package["files"].items():
                    shutil.copyfile(folder / filename, target / filename)
                    if hashlib.sha256((target / filename).read_bytes()).hexdigest() != expected:
                        raise ValueError("Diagnostic asset changed during publication")
                job = {"id": identifier, "source": "diagnostic", "status": "ready", "started_at": time.time(),
                    "completed_at": time.time(), "request": {"start": report["context"]["start_seconds"],
                        "seconds": report["duration_seconds"] if "duration_seconds" in report
                            else report["measurements"]["raw"]["duration_seconds"]},
                    "reports": {track: report}, "applied": {}, "study_digest": digest}
                self._save(job)
                self.jobs[identifier] = job
                self._prune()
            except (OSError, ValueError, KeyError, TypeError):
                self.jobs.pop(identifier, None)
                raise
            return self.snapshot()

    def import_suite(self, data):
        """Publish a verified local evaluation; never applies any saved edit."""
        import hashlib

        from .processing_study import validate_processing_package
        from .rehearsal_suite import validate_suite_package

        identifier = data.get("study_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("Choose a locally prepared suite ID")
        staging = (self.root/"suite-staging").resolve()
        folder = (staging/identifier).resolve()
        if folder.parent != staging or folder.is_symlink():
            raise ValueError("Suite must stay inside its staging directory")
        validator = validate_processing_package if data.get("processing") is True else validate_suite_package
        report, files, digest = validator(folder)
        with self.lock:
            if self.closed or self.process is not None:
                raise ValueError("Wait until analysis finishes before importing")
            if identifier in self.jobs:
                if self.jobs[identifier].get("study_digest") == digest:
                    return self.snapshot()
                raise ValueError("Suite ID is already in use")
            destination = self.root/identifier
            if destination.exists():
                raise ValueError("Suite output already exists")
            destination.mkdir()
            try:
                for filename, checksum in files.items():
                    shutil.copyfile(folder/filename, destination/filename)
                    if hashlib.sha256((destination/filename).read_bytes()).hexdigest() != checksum:
                        raise ValueError("Suite audio changed during publication")
                job = {"id": identifier, "source": "suite", "status": "ready", "started_at": time.time(),
                    "completed_at": time.time(), "request": {"start": 0, "seconds": 0},
                    "reports": {"MIX": report}, "applied": {}, "study_digest": digest}
                self._save(job)
                self.jobs[identifier] = job
                self._prune()
            except (OSError, ValueError, KeyError, TypeError):
                self.jobs.pop(identifier, None)
                raise
            return self.snapshot()

    def assess_suite(self, data):
        """Operator performance/preference judgments, separate from technical evidence."""
        with self.lock:
            job = self.jobs.get(data.get("job_id"))
            if not job or job["source"] != "suite" or job["status"] != "ready":
                raise ValueError("Choose a completed rehearsal suite")
            passage = data.get("passage_id")
            if passage not in {p["id"] for p in job["reports"]["MIX"]["passages"]}:
                raise ValueError("Choose an existing suite passage")
            include, verdict, reason = data.get("include"), data.get("verdict"), data.get("reason")
            if (type(include) is not bool or verdict not in ("better", "same", "worse", "unsure")
                    or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500):
                raise ValueError("Use a performance decision, listening verdict and short reason")
            job.setdefault("suite_assessments", {})[passage] = {"include": include,
                "verdict": verdict, "reason": reason.strip(), "origin": "operator", "assessed_at": time.time()}
            self._save(job)
            return self.snapshot()

    def assess_processing(self, data):
        """Store an operator verdict for a specific role, component and context."""
        from .processing_study import SCOPES

        with self.lock:
            job = self.jobs.get(data.get("job_id"))
            if (not job or job["source"] != "suite" or job["status"] != "ready"
                    or job["reports"]["MIX"]["kind"] != "wcfi_processing_study"):
                raise ValueError("Choose a completed local processing study")
            passage, role, variant, context = (data.get(k) for k in ("passage_id", "role", "variant", "context"))
            permitted = SCOPES.get(role, ()) if role in ("red", "kit") else ("chain",) if role == "both" else ()
            if (passage not in {p["id"] for p in job["reports"]["MIX"]["passages"]}
                    or variant not in permitted or variant == "current" or context not in ("solo", "mix")
                    or role == "both" and context != "mix"):
                raise ValueError("Choose an existing role treatment and listening context")
            verdict, reason = data.get("verdict"), data.get("reason")
            if verdict not in ("better", "same", "worse", "unsure") or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
                raise ValueError("Use a listening verdict and short reason")
            key = f"{passage}:{role}:{variant}:{context}"
            job.setdefault("processing_assessments", {})[key] = {"verdict": verdict, "reason": reason.strip(),
                "origin": "operator", "assessed_at": time.time()}
            self._save(job)
            return self.snapshot()

    def import_study(self, data):
        """Publish a locally prepared, bounded study; no arbitrary paths or writes to Live."""
        import hashlib

        import numpy as np
        import soundfile as sf

        from .kit_study import study_audio_keys

        identifier = data.get("study_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("Choose a locally prepared KIT study ID")
        staging_root = self.root / "study-staging"
        folder = (staging_root / identifier).resolve()
        if folder.parent != staging_root.resolve() or folder.is_symlink():
            raise ValueError("Study must stay inside its staging directory")
        manifest = folder / "study.json"
        if manifest.stat().st_size > 2*1024*1024:
            raise ValueError("Study manifest is too large")
        raw = manifest.read_bytes()
        package = json.loads(raw)
        if package.get("kind") != "wcfi_kit_study_package":
            raise ValueError("Not a supported KIT study")
        audio_keys = study_audio_keys(package.get("schema_version"))
        report = package["reports"]["KIT"]
        if report.get("kind") != "wcfi_kit_context_study" or set(report["audio"]) != audio_keys:
            raise ValueError("Study must contain all KIT and mix comparison variants")
        duration = report["comparison"]["duration_seconds"]
        if type(duration) not in (int, float) or not .5 <= duration <= 20:
            raise ValueError("Study must contain 0.5–20 seconds")
        passage = report["comparison"].get("passage")
        if package["schema_version"] == 2 and (
                not isinstance(passage, dict) or passage.get("id") not in ("quiet", "loud", "fills")
                or not isinstance(passage.get("label"), str) or not 1 <= len(passage["label"]) <= 100
                or type(passage.get("start_seconds")) not in (int, float)
                or not 0 <= passage["start_seconds"] <= 86400 or passage.get("duration_seconds") != duration):
            raise ValueError("Bypass study requires a bounded named passage")
        expected_files = {"KIT-"+v+".wav" for v in audio_keys}
        if set(package["files"]) != expected_files:
            raise ValueError("Study asset list does not match its allowed variants")
        for variant, filename in report["audio"].items():
            if filename != "KIT-"+variant+".wav":
                raise ValueError("Invalid study asset filename")
        digest = hashlib.sha256(raw).hexdigest()
        with self.lock:
            if self.closed or self.process is not None:
                raise ValueError("Wait until analysis finishes before importing a study")
            if identifier in self.jobs:
                if self.jobs[identifier].get("study_digest") == digest:
                    return self.snapshot()
                raise ValueError("Study ID is already in use")
            target = self.root / identifier
            if target.exists():
                raise ValueError("Study output directory already exists")
            for filename in expected_files:
                path = (folder / filename).resolve()
                if path.parent != folder or path.stat().st_size > 16*1024*1024:
                    raise ValueError("Invalid study asset location or size")
                info = sf.info(path)
                if info.samplerate != 48000 or info.channels != 2 or info.frames != round(duration*48000):
                    raise ValueError("Study asset format does not match its manifest")
                samples, _ = sf.read(path, dtype="float32", always_2d=True)
                if not np.isfinite(samples).all() or hashlib.sha256(path.read_bytes()).hexdigest() != package["files"][filename]:
                    raise ValueError("Study asset is invalid or its checksum changed")
            target.mkdir()
            try:
                for filename in expected_files:
                    shutil.copyfile(folder / filename, target / filename)
                    if hashlib.sha256((target / filename).read_bytes()).hexdigest() != package["files"][filename]:
                        raise ValueError("Study asset changed during publication")
                job = {"id": identifier, "source": "study", "status": "ready", "started_at": time.time(),
                    "completed_at": time.time(), "request": {"start": passage["start_seconds"] if passage else 29, "seconds": duration},
                    "reports": {"KIT": report}, "applied": {}, "study_digest": digest}
                self._save(job)
                self.jobs[identifier] = job
                self._prune()
            except (OSError, ValueError, KeyError, TypeError):
                # Preserve failed publication files for inspection; no job was authorized.
                self.jobs.pop(identifier, None)
                raise
            return self.snapshot()

    def import_reference(self, data):
        """Import bounded checksummed local pairs, never URLs or arbitrary paths."""
        import hashlib

        import numpy as np
        import soundfile as sf

        from .reference_study import PASSAGES, reference_audio_keys, reference_source

        identifier = data.get("study_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("Choose a locally prepared reference ID")
        staging = (self.root / "reference-staging").resolve()
        folder = (staging / identifier).resolve()
        if folder.parent != staging or folder.is_symlink():
            raise ValueError("Reference must stay inside its staging directory")
        manifest = folder / "study.json"
        if manifest.stat().st_size > 2*1024*1024:
            raise ValueError("Reference manifest is too large")
        raw = manifest.read_bytes()
        package = json.loads(raw)
        if (package.get("kind") != "wcfi_reference_package" or type(package.get("schema_version")) is not int
                or package["schema_version"] not in (1, 2)):
            raise ValueError("Not a supported reference package")
        report = package["reports"]["MIX"]
        if report.get("kind") != "wcfi_reference_study":
            raise ValueError("Not a reference study")
        pairs = report["pairs"]
        ids = {p["id"] for p in pairs}
        version = package["schema_version"]
        if (not 1 <= len(pairs) <= 3 or len(ids) != len(pairs) or not ids <= set(PASSAGES)
                or version == 1 and (ids != set(PASSAGES) or reference_source(report) != "bethel")):
            raise ValueError("Reference needs distinct bounded passage pairs")
        if version == 2 and (report.get("reference_key") != "reference"
                or not isinstance(report.get("reference_name"), str) or not 1 <= len(report["reference_name"]) <= 80
                or report.get("reference_kind") not in ("service_broadcast", "released_live_performance")
                or report.get("performance_confirmation_required") is not True):
            raise ValueError("Reference needs a name, performance type and passage confirmation")
        audio_keys = reference_audio_keys(report)
        if set(report["audio"]) != audio_keys:
            raise ValueError("Reference requires exactly the declared comparison excerpts")
        lengths = {}
        target = report["target_lufs"]
        if type(target) not in (int, float) or not math.isfinite(target) or not -60 <= target <= -18:
            raise ValueError("Invalid reference comparison loudness")
        for pair in pairs:
            duration = pair["duration_seconds"]
            if type(duration) not in (int, float) or not .5 <= duration <= 20:
                raise ValueError("Use finite 0.5–20 second reference excerpts")
            for source in ("wcfi", reference_source(report)):
                key = f"reference_{pair['id']}_{source}_matched"
                lengths[key] = round(duration*48000)
                matched = pair[source]["matched"]
                if (not math.isfinite(matched["integrated_lufs"]) or abs(matched["integrated_lufs"]-target) > .15
                        or not math.isfinite(matched["true_peak_dbtp"]) or matched["true_peak_dbtp"] > -2.85):
                    raise ValueError("Reference matching has not been verified")
                if source != "wcfi" and not re.fullmatch(
                        r"https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}&t=\d+s", pair[source]["url"]):
                    raise ValueError("Reference attribution must be a public YouTube video link")
        expected = {f"MIX-{v}.wav" for v in audio_keys}
        if set(package["files"]) != expected or any(report["audio"][v] != f"MIX-{v}.wav" for v in audio_keys):
            raise ValueError("Invalid reference asset names")
        digest = hashlib.sha256(raw).hexdigest()
        with self.lock:
            if self.closed or self.process is not None:
                raise ValueError("Wait until analysis finishes before importing a reference")
            if identifier in self.jobs:
                if self.jobs[identifier].get("study_digest") == digest:
                    return self.snapshot()
                raise ValueError("Reference ID is already in use")
            target_folder = self.root / identifier
            if target_folder.exists():
                raise ValueError("Reference output directory already exists")
            for key, filename in report["audio"].items():
                path = (folder / filename).resolve()
                if path.parent != folder or path.stat().st_size > 16*1024*1024:
                    raise ValueError("Invalid reference asset location or size")
                info = sf.info(path)
                if info.samplerate != 48000 or info.channels != 2 or info.frames != lengths[key]:
                    raise ValueError("Reference asset format does not match")
                samples, _ = sf.read(path, dtype="float32", always_2d=True)
                if (not np.isfinite(samples).all() or np.max(np.abs(samples)) > .999
                        or hashlib.sha256(path.read_bytes()).hexdigest() != package["files"][filename]):
                    raise ValueError("Reference audio is invalid or its checksum changed")
            target_folder.mkdir()
            try:
                for filename in expected:
                    shutil.copyfile(folder / filename, target_folder / filename)
                    if hashlib.sha256((target_folder / filename).read_bytes()).hexdigest() != package["files"][filename]:
                        raise ValueError("Reference asset changed during publication")
                job = {"id": identifier, "source": "reference", "status": "ready", "started_at": time.time(),
                    "completed_at": time.time(), "request": {"start": 0, "seconds": pairs[0]["duration_seconds"]},
                    "reports": {"MIX": report}, "applied": {}, "study_digest": digest}
                self._save(job)
                self.jobs[identifier] = job
                self._prune()
            except (OSError, ValueError, KeyError, TypeError):
                self.jobs.pop(identifier, None)
                raise
            return self.snapshot()

    def import_balance(self, data):
        """Publish a finite full-mix experiment without any DAW control."""
        import hashlib

        import numpy as np
        import soundfile as sf

        from .mix_balance import balance_audio_keys, balance_variants

        identifier = data.get("study_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("Choose a locally prepared balance study ID")
        staging = (self.root/"balance-staging").resolve()
        folder = (staging/identifier).resolve()
        if folder.parent != staging or folder.is_symlink():
            raise ValueError("Balance study must stay inside staging")
        manifest = folder/"study.json"
        if manifest.stat().st_size > 2*1024*1024:
            raise ValueError("Balance manifest is too large")
        raw = manifest.read_bytes()
        package = json.loads(raw)
        if package.get("kind") != "wcfi_mix_balance_package":
            raise ValueError("Not a supported balance package")
        version = package.get("schema_version")
        report = package["reports"]["MIX"]
        variants = balance_variants(version, report)
        audio_keys = balance_audio_keys(version, report)
        duration, target = report["duration_seconds"], report["target_lufs"]
        if (report.get("kind") != "wcfi_mix_balance_study" or set(report["audio"]) != audio_keys
                or type(duration) not in (int, float) or not .5 <= duration <= 20
                or type(target) not in (int, float) or not -60 <= target <= -18
                or report.get("restored") is not True or report.get("preference_established") is not False):
            raise ValueError("Balance study needs bounded matched captures and confirmed restoration")
        if version in (2, 3):
            descriptions = report.get("variants", [])
            if (not isinstance(descriptions, list) or len(descriptions) != 3
                    or any(not isinstance(v, dict) for v in descriptions)
                    or [v.get("id") for v in descriptions] != list(variants)
                    or [v.get("origin") for v in descriptions] != ["native_capture", "native_capture", "local_eq_preview"]
                    or any(not isinstance(v.get("label"), str) or not 1 <= len(v["label"]) <= 80
                           or not isinstance(v.get("description"), str) or not 1 <= len(v["description"]) <= 500
                           for v in descriptions)):
                raise ValueError("EQ comparison needs explicit captured and local-preview provenance")
        if version == 3:
            for key, maximum in (("target_track", 100), ("comparison_summary", 1500),
                                 ("comparison_listen_note", 1000)):
                if not isinstance(report.get(key), str) or not 1 <= len(report[key].strip()) <= maximum:
                    raise ValueError("Targeted comparison needs its source and listening description")
            findings = report.get("source_findings")
            if (not isinstance(findings, list) or not 1 <= len(findings) <= 12
                    or any(not isinstance(v, str) or not 1 <= len(v) <= 1000 for v in findings)):
                raise ValueError("Targeted comparison needs bounded source findings")
        if version in (4, 5):
            descriptions = report.get("variants", [])
            if (not isinstance(descriptions, list) or len(descriptions) != len(variants)
                    or any(not isinstance(v, dict) for v in descriptions)
                    or [v.get("id") for v in descriptions] != list(variants)
                    or any(v.get("origin") != "native_capture"
                           or not isinstance(v.get("label"), str) or not 1 <= len(v["label"]) <= 80
                           or not isinstance(v.get("description"), str) or not 1 <= len(v["description"]) <= 500
                           for v in descriptions)):
                raise ValueError("Balance comparison needs explicit native capture provenance")
            evaluator = report.get("evaluator", {})
            if not isinstance(evaluator, dict):
                raise ValueError("Balance comparison needs a CPU evaluator report")
            rows = evaluator.get("rows", [])
            if (evaluator.get("device") != "cpu" or evaluator.get("gpu_initialized") is not False
                    or evaluator.get("cpu_threads") != 1 or evaluator.get("logical_cpus_allowed") != 1
                    or evaluator.get("audio_uploads") != 0 or evaluator.get("human_preference_validated") is not False
                    or not isinstance(rows, list) or len(rows) != len(variants)
                    or any(not isinstance(r, dict) for r in rows)
                    or [r.get("variant") for r in rows] != list(variants)):
                raise ValueError("Balance comparison needs a completed CPU-only evaluator report")
            median = evaluator.get("median_inference_seconds")
            if (not isinstance(evaluator.get("note"), str) or not 1 <= len(evaluator["note"]) <= 1500
                    or type(median) not in (int, float) or not math.isfinite(median) or median <= 0
                    or type(evaluator.get("identical_control_passed")) is not bool
                    or type(evaluator.get("repeat_controls_passed")) is not bool):
                raise ValueError("Evaluator timing and control results are required")
            for row in rows:
                scores = row.get("scores", {})
                delta = row.get("pq_delta_from_current")
                if (not isinstance(scores, dict) or set(scores) != {"PQ", "CE", "CU", "PC"}
                        or any(type(v) not in (int, float) or not math.isfinite(v) for v in scores.values())
                        or type(delta) not in (int, float) or not math.isfinite(delta)):
                    raise ValueError("Invalid Audiobox scores")
                filename = f"MIX-balance_{row['variant']}_matched.wav"
                if row.get("matched_audio_sha256") != package["files"].get(filename):
                    raise ValueError("Evaluator scores must refer to these exact matched mixes")
            baseline = rows[0]["scores"]["PQ"]
            if any(abs(r["pq_delta_from_current"]-(r["scores"]["PQ"]-baseline)) > 1e-6 for r in rows):
                raise ValueError("Audiobox score deltas do not match the baseline")
        if version == 5:
            grid = report.get("balance_grid")
            expected_points = [{"variant": f"ac{abs(ac)}_kit{kit}", "ac_db": ac, "kit_db": kit}
                               for ac in (-24, -18, -12) for kit in (0, 3, 6)]
            if (not isinstance(grid, dict) or grid.get("points") != expected_points
                    or grid.get("default_variant") not in variants[3:]):
                raise ValueError("Level sweep needs nine distinct acoustic/KIT combinations and a valid default")
            highest = max(report["evaluator"]["rows"][3:], key=lambda r: r["scores"]["PQ"])["variant"]
            if grid["default_variant"] != highest:
                raise ValueError("Initial sweep selection must match the stated model ranking")
        if version == 6:
            descriptions = report["variants"]
            if any(v.get("origin") != "native_capture"
                   or not isinstance(v.get("label"), str) or not 1 <= len(v["label"]) <= 80
                   or not isinstance(v.get("description"), str) or not 1 <= len(v["description"]) <= 500
                   for v in descriptions):
                raise ValueError("Foundation comparisons need explicit native capture provenance")
            foundation = report.get("foundation")
            if (not isinstance(foundation, dict) or foundation.get("starting_variant") not in variants[1:]
                    or foundation.get("applied_variant") != foundation.get("starting_variant")
                    or foundation.get("selection_method") != "engineering_judgment"
                    or foundation.get("original_controls_saved") is not True
                    or foundation.get("audio_models_run") != 0 or report.get("evaluator") is not None):
                raise ValueError("Foundation comparisons need an explicit starting decision and saved controls, without model scores")
            for name, maximum in (("anchor_track", 100), ("added_track", 100), ("explanation", 1500)):
                if not isinstance(foundation.get(name), str) or not 1 <= len(foundation[name].strip()) <= maximum:
                    raise ValueError("Foundation decisions need named sources and a bounded explanation")
        for variant in variants:
            matched = report["matched"][variant]
            if (not math.isfinite(matched["integrated_lufs"]) or abs(matched["integrated_lufs"]-target) > .15
                    or not math.isfinite(matched["true_peak_dbtp"]) or matched["true_peak_dbtp"] > -2.85):
                raise ValueError("Balance loudness matching is unverified")
        expected = {f"MIX-{v}.wav" for v in audio_keys}
        if set(package["files"]) != expected or any(report["audio"][v] != f"MIX-{v}.wav" for v in audio_keys):
            raise ValueError("Invalid balance asset names")
        digest = hashlib.sha256(raw).hexdigest()
        with self.lock:
            if self.closed or self.process is not None:
                raise ValueError("Wait until analysis finishes before importing")
            if identifier in self.jobs:
                if self.jobs[identifier].get("study_digest") == digest:
                    return self.snapshot()
                raise ValueError("Balance study ID is already in use")
            destination = self.root/identifier
            if destination.exists():
                raise ValueError("Balance output already exists")
            for filename in expected:
                path = (folder/filename).resolve()
                if path.parent != folder or path.stat().st_size > 16*1024*1024:
                    raise ValueError("Invalid balance asset location or size")
                info = sf.info(path)
                if info.samplerate != 48000 or info.channels != 2 or info.frames != round(duration*48000):
                    raise ValueError("Balance audio format does not match")
                samples, _ = sf.read(path, dtype="float32", always_2d=True)
                if (not np.isfinite(samples).all() or np.max(np.abs(samples)) > .999
                        or hashlib.sha256(path.read_bytes()).hexdigest() != package["files"][filename]):
                    raise ValueError("Invalid balance audio or checksum")
            destination.mkdir()
            try:
                for filename in expected:
                    shutil.copyfile(folder/filename, destination/filename)
                    if hashlib.sha256((destination/filename).read_bytes()).hexdigest() != package["files"][filename]:
                        raise ValueError("Balance asset changed during publication")
                job = {"id": identifier, "source": "balance", "status": "ready", "started_at": time.time(),
                    "study_schema_version": version,
                    "completed_at": time.time(), "request": {"start": report["passage"]["start_seconds"], "seconds": duration},
                    "reports": {"MIX": report}, "applied": {}, "study_digest": digest}
                self._save(job)
                self.jobs[identifier] = job
                self._prune()
            except (OSError, ValueError, KeyError, TypeError):
                self.jobs.pop(identifier, None)
                raise
            return self.snapshot()

    def assess_balance(self, data):
        """Confirm a candidate performance independently of signal-energy selection."""
        with self.lock:
            job = self.jobs.get(data.get("job_id"))
            if not job or job["source"] != "balance" or job["status"] != "ready":
                raise ValueError("Choose a completed full-mix experiment")
            include, reason = data.get("include"), data.get("reason")
            if type(include) is not bool or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
                raise ValueError("Use a true/false performance decision and a short reason")
            job["performance_assessment"] = {"include": include, "reason": reason.strip(),
                "origin": "operator", "assessed_at": time.time()}
            self._save(job)
            return self.snapshot()

    def assess_reference(self, data):
        """Persist operator judgments; never infer performance validity from energy."""
        with self.lock:
            job = self.jobs.get(data.get("job_id"))
            if not job or job["source"] != "reference" or job["status"] != "ready":
                raise ValueError("Choose a completed full-mix reference comparison")
            report = job["reports"]["MIX"]
            pairs = {p["id"]: p for p in report["pairs"]}
            supplied = data.get("assessments")
            if not isinstance(supplied, dict) or not supplied or not set(supplied) <= set(pairs):
                raise ValueError("Choose existing reference passage IDs")
            updates = {}
            for identity, assessment in supplied.items():
                if (not isinstance(assessment, dict) or type(assessment.get("include")) is not bool
                        or not isinstance(assessment.get("reason"), str) or not 1 <= len(assessment["reason"].strip()) <= 500):
                    raise ValueError("Use a true/false eligibility decision and a short reason")
                classification = assessment.get("classification", "performance" if assessment["include"] else "unsuitable")
                if classification not in ({"performance"} if assessment["include"] else {"jam", "unsuitable", "unverified"}):
                    raise ValueError("Use a classification consistent with performance eligibility")
                updates[identity] = {"include": assessment["include"], "reason": assessment["reason"].strip(),
                    "classification": classification, "origin": "operator", "assessed_at": time.time()}
            job.setdefault("reference_assessments", {}).update(updates)
            self._save(job)
            # These KIT studies are the exact source captures of this reference.
            # Link by restored snapshot timestamp and exact window, not track name.
            captured_at = report.get("session_context", {}).get("captured_session_inspected_at")
            if captured_at is not None:
                for study in self.jobs.values():
                    if study["source"] != "study":
                        continue
                    comparison = study["reports"].get("KIT", {}).get("comparison", {})
                    passage = comparison.get("passage", {})
                    identity = passage.get("id")
                    pair = pairs.get(identity, {})
                    if (identity in updates and comparison.get("restore", {}).get("inspected_at") == captured_at
                            and passage.get("start_seconds") == pair.get("wcfi", {}).get("start_seconds")
                            and passage.get("duration_seconds") == pair.get("duration_seconds")):
                        study["performance_assessment"] = updates[identity] | {"reference_job_id": job["id"]}
                        self._save(study)
            return self.snapshot()

    def apply(self, data):
        with self.lock:
            if self.process is not None:
                raise ValueError("Wait until analysis finishes before applying")
            job = self.jobs.get(data.get("job_id"))
            name = data.get("track")
            if not job or job["status"] != "ready" or name not in ("RED", "KIT"):
                raise ValueError("Choose a completed RED/KIT analysis")
            if data.get("kind") != "software_gain":
                raise ValueError("Experimental HF processing is preview-only; no matching live processor is installed")
            prior = job["applied"].get(name)
            if prior:
                if prior["status"] == "applied":
                    return copy.deepcopy(prior)
                raise ValueError("A previous apply attempt requires inspection. Do not retry; capture nodes again.")
            if job["source"] != "nodes" or not 0 <= time.time() - job["completed_at"] <= 600:
                raise ValueError("Capture current Ableton nodes first; gain proposals expire after 10 minutes")
            report = job["reports"][name]
            gain_target_check = availability(report)
            if not gain_target_check["supported"]:
                raise ValueError(gain_target_check["reason"])
            client = self.client_factory()
            # Persist intent before any possible write; a lost response must not duplicate it.
            job["applied"][name] = {"status": "inspect_required", "at": time.time()}
            self._save(job)
            try:
                result = apply_gain(client, report, request_id=uuid.uuid4().hex)
                job["applied"][name] = result
            except (ValueError, OSError, RuntimeError):
                self._save(job)
                raise
            self._save(job)
            return copy.deepcopy(result)

    def close(self):
        with self.lock:
            self.closed = True
            if self.process is not None:
                self.process.terminate()
        if self.thread:
            self.thread.join(timeout=8)
