"""Offline experimental preference and artifact ranker training."""
from pathlib import Path
from typing import Annotated

import typer

from .master_benchmark import _execute

app = typer.Typer(help="Train and test small MED preference / ODAQ artifact rankers, CPU only.",
                  no_args_is_help=True)


@app.command('test-balance-activity')
def test_balance_activity_command(
    manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option()],
    protocol: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
):
    """Test frozen source-balance detectors with shared activity, saved files only."""
    def execute():
        from ..soundcheck.balance_activity_validation import run

        return run(manifest, output, protocol, progress=lambda s: typer.echo(s, err=True))
    _execute(execute)


@app.command('test-balance-calibration')
def test_balance_calibration_command(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option()],
    protocol: Annotated[Path | None, typer.Option(exists=True,dir_okay=False)] = None,
):
    """Compare frozen balance detectors on fresh saved sources, without playback."""
    def execute():
        from ..soundcheck.balance_validation import run

        return run(directory,output,protocol_file=protocol,progress=lambda s:typer.echo(s,err=True))
    _execute(execute)


@app.command('prepare-source-aware')
def prepare_source_aware_command(
    metadata: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    previous: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    assets: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option()],
):
    """Freeze source-label joins, artist-disjoint source tests and CPU policies."""
    def execute():
        from ..soundcheck.source_aware import prepare

        return prepare(metadata, previous, assets, output)
    _execute(execute)


@app.command('test-source-aware')
def test_source_aware_command(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option()],
    ffmpeg: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
):
    """Test source-aware preference and measured balance on saved songs, CPU only."""
    def execute():
        from ..soundcheck.source_aware import run

        return run(directory, output, ffmpeg, progress=lambda s: typer.echo(s, err=True))
    _execute(execute)


@app.command('inspect-sources')
def inspect_sources_command(
    manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    model: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    output: Annotated[Path | None, typer.Option()] = None,
):
    """Measure synchronized raw and post-mixer saved stems; never opens audio devices."""
    def execute():
        from ..soundcheck.master_benchmark_cpu import configure_cpu, offline_connections

        configure_cpu()
        from ..soundcheck.mix_preference import resource_checks
        from ..soundcheck.profiles import write_json
        from ..soundcheck.source_manifest import inspect

        with offline_connections():
            report = inspect(manifest, model=model) | {'resources': resource_checks()}
            if output:
                write_json(output, report)
            return report
    _execute(execute)


@app.command('prepare')
def prepare_command(
    assets_root: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    expanded_root: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option()],
):
    """Freeze saved sources, paired labels, whole-content folds and prospective policy."""
    def execute():
        from ..soundcheck.mix_preference import prepare

        return prepare(assets_root, expanded_root, output)
    _execute(execute)


@app.command('prepare-v2')
def prepare_v2_command(
    previous: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    assets: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    acquired: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option()],
):
    """Freeze selection-aware training and three separately blinded final songs."""
    def execute():
        from ..soundcheck.preference_v2 import prepare

        return prepare(previous, assets, acquired, output)
    _execute(execute)


@app.command('train-v2')
def train_v2_command(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    previous: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option()],
):
    """Test selection changes and reference-free artifact judgments on one CPU."""
    def execute():
        from ..soundcheck.preference_v2 import run

        return run(directory, previous, output, progress=lambda s: typer.echo(s, err=True))
    _execute(execute)


@app.command('train')
def train_command(
    directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option()],
):
    """Run nested song-held-out training and report; saved audio only, one CPU."""
    def execute():
        from ..soundcheck.mix_preference import run

        return run(directory, output, progress=lambda s: typer.echo(s, err=True))
    _execute(execute)


@app.command('inspect-signal')
def inspect_signal_command(
    audio: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    seconds: Annotated[int, typer.Option(min=8, max=120)] = 10,
):
    """Inspect a saved excerpt for stationary mains/flat peaks, no reference or playback."""
    def execute():
        from ..soundcheck.master_benchmark_cpu import configure_cpu, offline_connections

        configure_cpu()
        import soundfile as sf

        from ..soundcheck.artifact_health import analyze
        from ..soundcheck.mix_preference import resource_checks

        with offline_connections():
            with sf.SoundFile(audio) as stream:
                rate = stream.samplerate
                samples = stream.read(rate*seconds, dtype='float32', always_2d=True)
            return analyze(samples, rate) | {'source_file': str(audio.resolve()), 'resources': resource_checks()}
    _execute(execute)
