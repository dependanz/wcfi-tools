"""Controlled reload clients use local descriptor fixtures, never a running host."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from wcfi_tools.ableton import client
from wcfi_tools.commands import ableton as commands

OLD = '1' * 32
NEW = '2' * 32
ACTIVE = 'a' * 64
INSTALLED = 'b' * 64


class Clock:
    def __init__(self):
        self.now = 0.

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Scenario:
    def __init__(self, folder, monkeypatch):
        self.folder = folder
        self.clock = Clock()
        self.calls = []
        self.requests = []
        self.states = {}
        self.on_reload = None
        monkeypatch.setattr(client.time, 'monotonic', self.clock.monotonic)
        monkeypatch.setattr(client.time, 'sleep', self.clock.sleep)
        self.old = self.descriptor(OLD)
        self.states[OLD] = self.status(OLD)
        self.connected = client.AbletonClient(self.old, discovery_folder=folder)
        scenario = self

        def call(connected, operation, arguments=None, **kwargs):
            scenario.calls.append((connected.descriptor['instance_id'], operation, arguments, kwargs))
            if operation == 'reload_bridge':
                scenario.requests.append((arguments, kwargs))
                if scenario.on_reload:
                    return scenario.on_reload(arguments, kwargs)
                return scenario.ack(arguments)
            assert operation == 'status'
            state = scenario.states[connected.descriptor['instance_id']]
            if isinstance(state, list):
                state = state.pop(0) if len(state) > 1 else state[0]
            if isinstance(state, Exception):
                raise state
            return state() if callable(state) else state

        monkeypatch.setattr(client.AbletonClient, 'call', call)

    def descriptor(self, session, *, pid=741, token=None, port=None):
        value = {'protocol': 1, 'pid': pid, 'instance_id': session,
                 'token': token or session * 2, 'port': port or (11001 if session == OLD else 11002)}
        (self.folder / f'bridge-{session}.json').write_text(json.dumps(value), encoding='utf-8')
        return value

    def status(self, session, receipt=None):
        return {'session_id': session, 'bridge_revision': 7, 'operations': ['reload_bridge'],
                'reload': {'supported': True, 'bootstrap_version': 1, 'bootstrap_current': True,
                           'active_bundle_id': ACTIVE if session == OLD else INSTALLED,
                           'installed_bundle_id': INSTALLED, 'last_reload': receipt}}

    def ack(self, arguments):
        return {'reload_id': arguments['reload_id'], 'old_session_id': arguments['session_id'],
                'bundle_id': arguments['expected_bundle_id'], 'state': 'accepted', 'dry_run': False}

    def receipt(self, arguments, **changes):
        return {**self.ack(arguments), 'state': 'complete', **changes}

    def complete(self, arguments, **receipt_changes):
        self.descriptor(NEW)
        self.states[NEW] = self.status(NEW, self.receipt(arguments, **receipt_changes))
        return self.ack(arguments)


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    return Scenario(tmp_path, monkeypatch)


def test_reload_sends_once_correlates_receipt_and_adopts_new_client(scenario):
    scenario.on_reload = lambda arguments, _: scenario.complete(arguments)
    result = client.reload_bridge(scenario.connected, timeout=.3)
    assert result['verified'] and result['changed'] and result['state'] == 'complete'
    assert result['acknowledgement_received'] and result['session_id'] == NEW
    assert scenario.connected.descriptor['instance_id'] == NEW
    assert scenario.connected.discovery_folder == scenario.folder
    assert len(scenario.requests) == 1
    args, options = scenario.requests[0]
    assert args == {'session_id': OLD, 'expected_bundle_id': INSTALLED,
                    'reload_id': result['reload_id'], 'dry_run': False, 'discard_receipts': False}
    assert options['request_id'] == args['reload_id'] and len(args['reload_id']) == 32
    assert 'token' not in result and 'port' not in result
    assert all(call[1] in ('status', 'reload_bridge') for call in scenario.calls)


@pytest.mark.parametrize('code', ['connection_failed', 'outcome_unknown', 'disconnected', 'protocol_error'])
def test_lost_acknowledgement_is_reconciled_without_resending(scenario, code):
    def lost(arguments, _):
        scenario.complete(arguments)
        raise client.AbletonError('reply lost', code=code)

    scenario.on_reload = lost
    result = client.reload_bridge(scenario.connected, timeout=.3)
    assert result['verified'] and not result['acknowledgement_received']
    assert result['acknowledgement_error']['code'] == code
    assert len(scenario.requests) == 1


def test_malformed_ack_is_not_success_but_exact_new_receipt_can_reconcile(scenario):
    def malformed(arguments, _):
        scenario.complete(arguments)
        return {'state': 'accepted', 'reload_id': 'wrong'}

    scenario.on_reload = malformed
    result = client.reload_bridge(scenario.connected, timeout=.3)
    assert result['verified'] and not result['acknowledgement_received']
    assert len(scenario.requests) == 1


def test_shutdown_and_initializing_status_are_tolerated(scenario):
    def transition(arguments, _):
        scenario.complete(arguments)
        finished = scenario.states[NEW]
        scenario.states[NEW] = [client.AbletonError('not ready', code='connection_failed'),
                                client.AbletonError('initializing', code='reloading'), finished]
        return scenario.ack(arguments)

    scenario.on_reload = transition
    result = client.reload_bridge(scenario.connected, timeout=.5)
    assert result['verified'] and len(scenario.requests) == 1
    assert scenario.clock.now >= .2


@pytest.mark.parametrize('change', [
    {'reload_id': '3' * 32}, {'old_session_id': '3' * 32},
    {'bundle_id': 'c' * 64}, {'state': 'accepted'},
])
def test_unrelated_receipt_never_counts_as_completion(scenario, change):
    scenario.on_reload = lambda arguments, _: scenario.complete(arguments, **change)
    with pytest.raises(client.AbletonError) as failure:
        client.reload_bridge(scenario.connected, timeout=.2)
    assert failure.value.code == 'reload_outcome_unknown'
    assert len(scenario.requests) == 1 and scenario.connected.descriptor['instance_id'] == OLD
    assert failure.value.details['request_id'] == scenario.requests[0][0]['reload_id']


@pytest.mark.parametrize('mismatch', ['pid', 'status_session', 'active_bundle', 'old_session',
                                      'bootstrap', 'operation'])
def test_descriptor_and_runtime_identity_must_be_coherent(scenario, mismatch):
    def wrong(arguments, _):
        scenario.complete(arguments)
        if mismatch == 'pid':
            scenario.descriptor(NEW, pid=999)
        elif mismatch == 'status_session':
            scenario.states[NEW]['session_id'] = '4' * 32
        elif mismatch == 'active_bundle':
            scenario.states[NEW]['reload']['active_bundle_id'] = ACTIVE
        elif mismatch == 'old_session':
            (scenario.folder / f'bridge-{NEW}.json').unlink()
            scenario.states[OLD] = scenario.status(OLD, scenario.receipt(arguments))
            scenario.states[OLD]['reload']['active_bundle_id'] = INSTALLED
        elif mismatch == 'bootstrap':
            scenario.states[NEW]['reload']['bootstrap_current'] = False
        elif mismatch == 'operation':
            scenario.states[NEW]['operations'] = []
        return scenario.ack(arguments)

    scenario.on_reload = wrong
    with pytest.raises(client.AbletonError) as failure:
        client.reload_bridge(scenario.connected, timeout=.2)
    assert failure.value.code == 'reload_outcome_unknown'
    if mismatch == 'pid':
        assert not any(call[0] == NEW for call in scenario.calls)
    assert len(scenario.requests) == 1


def test_failed_receipt_keeps_old_runtime_and_reports_failure(scenario):
    def failed(arguments, _):
        receipt = scenario.receipt(arguments, state='failed', error='replacement constructor failed')
        scenario.states[OLD] = scenario.status(OLD, receipt)
        return scenario.ack(arguments)

    scenario.on_reload = failed
    with pytest.raises(client.AbletonError) as failure:
        client.reload_bridge(scenario.connected, timeout=.3)
    assert failure.value.code == 'reload_failed'
    assert failure.value.details['failure'] == 'replacement constructor failed'
    assert scenario.connected.descriptor['instance_id'] == OLD
    assert len(scenario.requests) == 1


def test_definitive_rpc_rejection_is_not_retried(scenario):
    def conflict(_arguments, _options):
        raise client.AbletonError('installed bundle changed', code='bundle_conflict')

    scenario.on_reload = conflict
    with pytest.raises(client.AbletonError) as failure:
        client.reload_bridge(scenario.connected, expected_bundle_id='c' * 64, timeout=.3)
    assert failure.value.code == 'bundle_conflict' and len(scenario.requests) == 1
    assert scenario.requests[0][0]['expected_bundle_id'] == 'c' * 64


def test_dry_run_does_not_rediscover_or_replace_client(scenario):
    def dry(arguments, _):
        assert arguments['dry_run'] and arguments['discard_receipts']
        return {'verified': True, 'dry_run': True, 'changed': False, 'bundle_id': INSTALLED}

    scenario.on_reload = dry
    result = client.reload_bridge(scenario.connected, dry_run=True, discard_receipts=True)
    assert result['verified'] and not result['changed']
    assert len(scenario.calls) == 2 and scenario.connected.descriptor['instance_id'] == OLD


@pytest.mark.parametrize('result', [None, {}, {'verified': True, 'dry_run': True, 'changed': True}])
def test_bad_dry_run_reply_is_rejected(scenario, result):
    scenario.on_reload = lambda *_: result
    with pytest.raises(client.AbletonError) as failure:
        client.reload_bridge(scenario.connected, dry_run=True)
    assert failure.value.code == 'protocol_error' and len(scenario.requests) == 1


@pytest.mark.parametrize('missing', ['capability', 'operation', 'bootstrap', 'supported'])
def test_old_or_outdated_bootstrap_requires_one_activation_restart(scenario, missing):
    status = scenario.states[OLD]
    if missing == 'capability':
        status.pop('reload')
    elif missing == 'operation':
        status['operations'] = []
    else:
        status['reload']['bootstrap_current' if missing == 'bootstrap' else 'supported'] = False
    with pytest.raises(client.AbletonError) as failure:
        client.reload_bridge(scenario.connected)
    assert failure.value.code == 'bootstrap_update_required'
    assert 'restart Live once' in str(failure.value)
    assert not scenario.requests


def test_preflight_session_mismatch_never_sends_reload(scenario):
    scenario.states[OLD]['session_id'] = NEW
    with pytest.raises(client.AbletonError) as failure:
        client.reload_bridge(scenario.connected)
    assert failure.value.code == 'stale_session' and not scenario.requests


def test_malformed_preflight_status_never_sends_reload(scenario):
    scenario.states[OLD] = 'not a status object'
    with pytest.raises(client.AbletonError) as failure:
        client.reload_bridge(scenario.connected)
    assert failure.value.code == 'protocol_error' and not scenario.requests


def test_manual_client_uses_current_discovery_directory(scenario, monkeypatch):
    scenario.connected.discovery_folder = None
    monkeypatch.setenv('WCFI_ABLETON_HOME', str(scenario.folder))
    scenario.on_reload = lambda arguments, _: scenario.complete(arguments)
    assert client.reload_bridge(scenario.connected, timeout=.3)['verified']


@pytest.mark.parametrize('timeout', [0, -1, True, float('inf'), float('nan'), '20'])
def test_bad_timeout_does_not_contact_host(scenario, timeout):
    with pytest.raises(client.AbletonError) as failure:
        client.reload_bridge(scenario.connected, timeout=timeout)
    assert failure.value.code == 'invalid_argument' and not scenario.calls


@pytest.mark.parametrize('bundle', ['short', 'A' * 64, 123])
def test_invalid_expected_bundle_never_sends_reload(scenario, bundle):
    with pytest.raises(client.AbletonError) as failure:
        client.reload_bridge(scenario.connected, expected_bundle_id=bundle)
    assert failure.value.code == 'invalid_argument' and not scenario.requests


def test_timed_out_ack_does_not_hide_unknown_outcome_or_resend(scenario):
    def timeout(arguments, _):
        scenario.clock.now = 5
        raise client.AbletonError('no response', code='connection_failed')

    scenario.on_reload = timeout
    with pytest.raises(client.AbletonError) as failure:
        client.reload_bridge(scenario.connected, timeout=.2)
    assert failure.value.code == 'reload_outcome_unknown'
    assert failure.value.details['reload_id'] in str(failure.value)
    assert len(scenario.requests) == 1


def test_connect_retains_explicit_discovery_directory(scenario):
    connected = client.connect(folder=scenario.folder)
    assert connected.discovery_folder == scenario.folder
    assert connected.descriptor == scenario.old


def test_reload_cli_stages_once_and_uses_installed_identity(tmp_path, monkeypatch):
    from wcfi_tools.ableton import install as installer

    calls = []
    connected = object()
    monkeypatch.setattr(installer, 'install', lambda **kwargs: calls.append(('install', kwargs)) or
                        {'bundle_id': INSTALLED, 'restart_required': False})
    monkeypatch.setattr(client, 'connect', lambda **kwargs: calls.append(('connect', kwargs)) or connected)

    def reload(value, **kwargs):
        assert value is connected
        calls.append(('reload', kwargs))
        return {'verified': True, 'dry_run': False, 'state': 'complete'}

    monkeypatch.setattr(client, 'reload_bridge', reload)
    response = CliRunner().invoke(commands.app, ['reload', '--install', '--discard-receipts',
        '--state-dir', str(tmp_path), '--user-library', str(tmp_path), '--instance', OLD, '--timeout', '3'])
    assert response.exit_code == 0, response.output
    assert [name for name, _ in calls] == ['install', 'connect', 'reload']
    assert calls[2][1] == {'expected_bundle_id': INSTALLED, 'dry_run': False,
                           'discard_receipts': True, 'timeout': 3.}
    assert json.loads(response.stdout)['installation']['bundle_id'] == INSTALLED


def test_reload_cli_rejects_install_dry_run_before_any_action(monkeypatch):
    monkeypatch.setattr(client, 'connect', lambda **_: pytest.fail('Unexpected host access'))
    response = CliRunner().invoke(commands.app, ['reload', '--install', '--dry-run'])
    assert response.exit_code == 1
    assert json.loads(response.stdout)['code'] == 'invalid_argument'


def test_reload_cli_reports_bootstrap_install_before_connecting(monkeypatch):
    from wcfi_tools.ableton import install as installer

    monkeypatch.setattr(installer, 'install', lambda **_: {'bundle_id': INSTALLED,
                                                          'restart_required': True})
    monkeypatch.setattr(client, 'connect', lambda **_: pytest.fail('Bootstrap requires activation'))
    response = CliRunner().invoke(commands.app, ['reload', '--install'])
    assert response.exit_code == 1
    body = json.loads(response.stdout)
    assert body['code'] == 'bootstrap_update_required'
    assert body['details']['installation']['bundle_id'] == INSTALLED


def test_reload_cli_reports_unknown_request_identifier(monkeypatch):
    monkeypatch.setattr(client, 'connect', lambda **_: SimpleNamespace())

    def unknown(*_args, **_kwargs):
        raise client.AbletonError('inspect status before retry', code='reload_outcome_unknown',
                                  details={'reload_id': 'f' * 32})

    monkeypatch.setattr(client, 'reload_bridge', unknown)
    response = CliRunner().invoke(commands.app, ['reload'])
    assert response.exit_code == 1
    body = json.loads(response.stdout)
    assert body['code'] == 'reload_outcome_unknown' and body['details']['reload_id'] == 'f' * 32


def test_status_cli_still_reports_unresolved_information_without_mutation(monkeypatch):
    calls = []

    def status(operation):
        calls.append(operation)
        return {'session_id': OLD, 'unresolved_operation': {'reason': 'inspect first'},
                'reload': {'last_reload': {'state': 'failed'}}}

    monkeypatch.setattr(client, 'connect', lambda **_: SimpleNamespace(call=status))
    response = CliRunner().invoke(commands.app, ['status'])
    assert response.exit_code == 0 and calls == ['status']
    assert json.loads(response.stdout)['unresolved_operation']['reason'] == 'inspect first'
