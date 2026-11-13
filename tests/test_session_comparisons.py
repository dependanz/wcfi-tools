"""Complete-chain comparison copies preserve the mix and refuse ambiguous controls."""

import gzip
import xml.etree.ElementTree as ET

import pytest

from wcfi_tools.soundcheck.ableton import read_set, value
from wcfi_tools.soundcheck.session_comparisons import prepare_session_comparisons


@pytest.fixture
def source(tmp_path):
    root = ET.fromstring('''<Ableton Creator="Ableton Live 11.3.35"><LiveSet><Tracks>
    <AudioTrack Id="8"><Name><EffectiveName Value="RED"/></Name><DeviceChain>
    <Mixer><SoloSink Value="true"/><Volume><Manual Value="1"/>
    <AutomationTarget Id="90"/></Volume></Mixer>
    <MainSequencer><MonitoringEnum Value="1"/><AudioClip><SampleRef><FileRef>
    <RelativePath Value="Samples/RED.wav"/></FileRef></SampleRef></AudioClip></MainSequencer>
    <DeviceChain><Devices><PluginDevice><OpaqueState Value="preserve exactly"/>
    </PluginDevice></Devices></DeviceChain></DeviceChain></AudioTrack>
    <GroupTrack Id="16"><DeviceChain><Mixer><SoloSink Value="true"/></Mixer>
    <DeviceChain><Devices><GlueCompressor Threshold="-20"/></Devices></DeviceChain>
    </DeviceChain></GroupTrack>
    <ReturnTrack Id="36"><DeviceChain><DeviceChain><Devices><Reverb Amount=".3"/>
    </Devices></DeviceChain></DeviceChain></ReturnTrack></Tracks>
    <MasterTrack><DeviceChain><Mixer><Tempo><Manual Value="105"/></Tempo></Mixer>
    <AudioOutputRouting><Target Value="AudioOut/External/S11"/></AudioOutputRouting>
    <DeviceChain><Devices><Limiter Ceiling="-1"/></Devices></DeviceChain>
    </DeviceChain></MasterTrack></LiveSet></Ableton>''')
    path = tmp_path / 'Service.als'
    path.write_bytes(gzip.compress(ET.tostring(root)))
    return path


def prepare(source, **kwargs):
    return prepare_session_comparisons(source, source.parent / 'trial', lead_track_id=8,
                                       start_bar=97, bars=40, **kwargs)


def test_whole_chain_and_original_are_preserved_with_only_declared_changes(source):
    original = source.read_bytes()
    before = read_set(source)
    report = prepare(source)
    assert source.read_bytes() == original
    gains = []
    for alternative in report['alternatives']:
        after = read_set(source.parent / f"trial-{alternative['condition']}.als")
        for path in ('.//PluginDevice', './/GroupTrack/DeviceChain/DeviceChain',
                     './/ReturnTrack', './/MasterTrack', './/SampleRef'):
            assert ET.tostring(after.find(path)) == ET.tostring(before.find(path))
        assert all(e.get('Value') == 'false' for e in after.findall('.//SoloSink'))
        assert value(after, './/AudioTrack/DeviceChain/MainSequencer/MonitoringEnum') == '2'
        gains.append(float(value(after, './/AudioTrack/DeviceChain/Mixer/Volume/Manual')))
    assert gains == pytest.approx([1, 10**(-3/20), 10**(3/20)])


@pytest.mark.parametrize('problem', ['automation', 'external_effect', 'muted_fader',
                                     'fader_ceiling', 'missing_monitor'])
def test_unsafe_or_unsupported_trials_write_nothing(source, problem):
    root = read_set(source)
    if problem == 'automation':
        ET.SubElement(root.find('LiveSet'), 'PointeeId', Value='90')
    elif problem == 'external_effect':
        ET.SubElement(root.find('LiveSet'), 'ExternalAudioEffect')
    elif problem in {'muted_fader', 'fader_ceiling'}:
        root.find('.//AudioTrack/DeviceChain/Mixer/Volume/Manual').set(
            'Value', '0' if problem == 'muted_fader' else '1.99')
    else:
        node = root.find('.//AudioTrack/DeviceChain/MainSequencer')
        node.remove(node.find('MonitoringEnum'))
    source.write_bytes(gzip.compress(ET.tostring(root)))
    with pytest.raises(ValueError):
        prepare(source)
    assert not (source.parent / 'trial').exists()
    assert not list(source.parent.glob('trial-*.als'))


def test_existing_set_copy_is_never_overwritten(source):
    previous = source.parent / 'trial-lead-louder.als'
    previous.write_bytes(b'user work')
    with pytest.raises(ValueError, match='already exists'):
        prepare(source)
    assert previous.read_bytes() == b'user work'
    assert not (source.parent / 'trial-baseline.als').exists()
