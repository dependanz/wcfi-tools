"""Read-only V19-to-shared-store compatibility inspection.

No authentication, network, DB writes, or import operation. Accept only synthetic
fixtures during development; run locally on private backups later if desired.
Never include lyrics, notes, or other user content in output diagnostics.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from shared_store import InvalidRecord, validate_lineup, validate_song

FORMAT = 'wcfi-music-local-backup'
KEYS = frozenset({'wcfi-music-hub-v2', 'wcfi-song-workshop-v1',
                  'wcfi-music-preview-v1', 'wcfi-song-editor-last'})
DRAFT_PREFIX = 'wcfi-song-editor-draft-v1:'
MAX_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 600
KNOWN_V19_DEMO_ID = "demo-sunday"


def is_known_v19_demo_lineup(value: Any) -> bool:
    """Only the bundled, recognizable V19 example may be excluded from staging."""
    return (
        isinstance(value, dict)
        and value.get("demo") is True
        and value.get("id") == KNOWN_V19_DEMO_ID
        and isinstance(value.get("title"), str)
        and isinstance(value.get("songs"), list)
        and isinstance(value.get("date"), str)
        and isinstance(value.get("notes"), str)
    )



@dataclass(frozen=True)
class MigrationPlan:
    source_format: str
    source_version: int
    record_count: int
    song_candidates: int
    song_ready: int
    song_blocked: int
    lineup_candidates: int
    lineup_ready: int
    lineup_blocked: int
    unfinished_editor_drafts: int
    arrangement_entries: int
    undated_draft_lineups: int
    demo_lineups_excluded: int
    publication_holds: tuple[str, ...]
    blockers: tuple[str, ...]
    can_auto_import: bool

    def summary(self) -> dict[str, Any]:
        return asdict(self)


def _storage_key(k: Any) -> bool:
    return isinstance(k, str) and (k in KEYS or (
        k.startswith(DRAFT_PREFIX) and 1 <= len(k[len(DRAFT_PREFIX):]) <= 128 and
        k[len(DRAFT_PREFIX)].isascii() and k[len(DRAFT_PREFIX)].isalnum() and
        all(c.isascii() and (c.isalnum() or c in '-_') for c in k[len(DRAFT_PREFIX):])))


def _json_object(raw: str) -> dict:
    def duplicate_check(pairs: list[tuple[str, Any]]) -> dict:
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError('Duplicate JSON field')
            obj[key] = value
        return obj
    result = json.loads(raw, object_pairs_hook=duplicate_check)
    if not isinstance(result, dict):
        raise TypeError('Expected object')
    return result


_LINEUP_VALIDATION_REASONS = {
    'Expected stable lowercase identifier': 'invalid_identifier',
    'Lineup title required': 'invalid_title',
    'Lineup date required': 'invalid_date',
    'Invalid calendar date': 'invalid_date',
    'Lineup needs songs': 'invalid_song_list',
    'Invalid lineup song': 'invalid_song_entry',
    'Invalid performance key': 'invalid_performance_key',
    'Invalid arrangement sequence': 'invalid_arrangement',
    'Invalid lineup notes': 'invalid_notes',
}


def _lineup_blockers(lineup: Any, available_song_ids: set[str]) -> set[str]:
    """Only categorical diagnostics: never echo titles, lyrics, IDs or private notes."""
    if not isinstance(lineup, dict):
        return {'lineups:invalid_record', 'lineups:incompatible_lineup'}
    issues: set[str] = set()
    try:
        validate_lineup(lineup)
    except (InvalidRecord, TypeError, KeyError) as exc:
        reason = _LINEUP_VALIDATION_REASONS.get(str(exc), 'incompatible_lineup')
        issues.update({'lineups:incompatible_lineup', 'lineups:' + reason})
    # Check references even if the validator stopped earlier at a missing date.
    items = lineup.get('songs')
    if isinstance(items, list) and any(
        isinstance(entry, dict) and isinstance(entry.get('id'), str) and
        entry['id'] not in available_song_ids for entry in items
    ):
        issues.add('lineups:unresolved_song_reference')
    return issues


def inspect_v19_backup(envelope: Any) -> MigrationPlan:
    """Inspect-only. Invalid backup envelopes raise; incompatible content is reported."""
    if not isinstance(envelope, dict) or envelope.get('format') != FORMAT or type(envelope.get('version')) is not int or envelope['version'] != 1 or envelope.get('scope') != 'browser-local-only':
        raise ValueError('Unsupported full local backup')
    if not isinstance(envelope.get('exportedAt'), str):
        raise TypeError('Missing backup date')
    try:
        datetime.fromisoformat(envelope['exportedAt'])
    except ValueError as exc:
        raise ValueError('Invalid backup date') from exc
    rows = envelope.get('records')
    if not isinstance(rows, list) or len(rows) > MAX_RECORDS or len(json.dumps(envelope).encode('utf-8')) > MAX_BYTES:
        raise ValueError('Invalid or oversized backup')
    raw = {}
    for row in rows:
        if not isinstance(row, dict) or not _storage_key(row.get('key')) or not isinstance(row.get('value'), str) or row['key'] in raw:
            raise ValueError('Invalid or duplicate browser storage record')
        raw[row['key']] = row['value']

    blockers: list[str] = []
    parsed: dict[str, dict] = {}
    for name in ('wcfi-song-workshop-v1', 'wcfi-music-hub-v2'):
        if name in raw:
            try:
                parsed[name] = _json_object(raw[name])
            except (ValueError, TypeError):
                blockers.append(name + ':invalid_json')
        else:
            blockers.append(name + ':missing')
    workshop = parsed.get('wcfi-song-workshop-v1', {})
    hub = parsed.get('wcfi-music-hub-v2', {})
    songs = workshop.get('songs', [])
    if not isinstance(songs, list):
        blockers.append('workshop:songs_not_list')
        songs = []
    song_ids: set[str] = set()
    ready_songs = 0
    for song in songs:
        try:
            cleaned = validate_song(song)
            if cleaned['id'] in song_ids:
                raise InvalidRecord('Duplicate song')
            song_ids.add(cleaned['id'])
            ready_songs += 1
        except (InvalidRecord, TypeError, KeyError):
            blockers.append('workshop:incompatible_song')
    arrangements = workshop.get('arrangements', {})
    arrangement_count = 0
    if not isinstance(arrangements, dict):
        blockers.append('workshop:invalid_arrangements')
    else:
        for entry in arrangements.values():
            if not isinstance(entry, dict):
                blockers.append('workshop:invalid_arrangements')
            else:
                arrangement_count += len(entry)
        if arrangement_count:
            blockers.append('arrangements:shared_schema_not_yet_supported')

    lineup_items = hub.get('lineups', [])
    if not isinstance(lineup_items, list):
        blockers.append('hub:lineups_not_list')
        lineup_items = []
    ready_lineups = 0
    undated_lineups = 0
    demo_excluded = 0
    for lineup in lineup_items:
        if isinstance(lineup, dict) and lineup.get("demo") is True:
            if is_known_v19_demo_lineup(lineup):
                # Exclude only the bundled demo fixture; the source backup is retained.
                demo_excluded += 1
            else:
                blockers.append("lineups:unrecognized_demo")
            continue
        issues = _lineup_blockers(lineup, song_ids)
        if issues:
            blockers.extend(sorted(issues))
        else:
            ready_lineups += 1
            if not lineup.get('date'):
                undated_lineups += 1
    real_lineups = len(lineup_items) - demo_excluded
    draft = hub.get('draft')
    if isinstance(draft, dict) and any(draft.get(k) for k in ('title', 'notes', 'date', 'songs')):
        blockers.append('draft:local_lineup_requires_separate_migration')
    elif draft is not None and not isinstance(draft, dict):
        blockers.append('draft:invalid_lineup_draft')
    draft_count = sum(k.startswith(DRAFT_PREFIX) for k in raw)
    if draft_count:
        blockers.append('drafts:unfinished_editor_state_requires_separate_migration')
    if 'wcfi-music-preview-v1' in raw:
        blockers.append('legacy:state_requires_review')
    blockers.append('migration:approval_mapping_not_implemented')
    blockers.append('migration:atomic_import_not_implemented')
    publication_holds = ('lineups:publication_date_required',) if undated_lineups else ()
    return MigrationPlan(FORMAT, 1, len(rows), len(songs), ready_songs,
                         len(songs)-ready_songs, real_lineups, ready_lineups,
                         real_lineups-ready_lineups, draft_count, arrangement_count,
                         undated_lineups, demo_excluded, publication_holds,
                         tuple(sorted(set(blockers))), False)


def inspect_backup_file(path: str | Path) -> MigrationPlan:
    file = Path(path)
    if file.stat().st_size > MAX_BYTES:
        raise ValueError('Backup exceeds size limit')
    return inspect_v19_backup(json.loads(file.read_text(encoding='utf-8-sig')))