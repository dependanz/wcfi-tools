"""CPU-only, file-based full-mix candidate and selector benchmark."""
from pathlib import Path
from typing import Annotated

import typer

from .role_analysis import execute

app = typer.Typer(help='Blinded full-mix reference/ambience benchmark; no DAW access.', no_args_is_help=True)


@app.command('prepare-combined')
def prepare_combined(previous: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
                     balance_directory: Annotated[Path, typer.Option(exists=True, file_okay=False)],
                     output: Annotated[Path, typer.Option()],
                     ffmpeg: Annotated[Path, typer.Option(exists=True, dir_okay=False)], seed: int = 2026102203):
    """Retain every previous control and add fresh hum tests and selection checks."""
    def work():
        from ..audio.full_mix_combined import prepare as build
        return build(previous, balance_directory, output, ffmpeg=ffmpeg, seed=seed,
                     progress=lambda v: typer.echo(v, err=True))
    execute(work)


@app.command('prepare-balance')
def prepare_balance(previous: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
                    output: Annotated[Path, typer.Option()],
                    ffmpeg: Annotated[Path, typer.Option(exists=True, dir_okay=False)], seed: int = 2026102202):
    """Freeze the source-balance veto and generate fresh controls from saved inputs."""
    def work():
        from ..audio.balance_benchmark import prepare as build
        return build(previous, output, ffmpeg=ffmpeg, seed=seed, progress=lambda v: typer.echo(v, err=True))
    execute(work)


@app.command('prepare')
def prepare(profile: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
            output: Annotated[Path, typer.Option()],
            ffmpeg: Annotated[Path, typer.Option(exists=True, dir_okay=False)], seed: int = 20261022):
    """Freeze policy, create candidates/controls and conceal their labels."""
    def work():
        from ..audio.full_mix_benchmark import prepare as build
        return build(profile, output, ffmpeg=ffmpeg, seed=seed, progress=lambda v: typer.echo(v, err=True))
    execute(work)


@app.command('run')
def run(directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
        output: Annotated[Path, typer.Option()]):
    """Score opaque audio on one CPU without reading the answer key."""
    def work():
        from ..audio.full_mix_benchmark import load
        kind = load(directory/'blind-inputs.json').get('kind')
        if kind == 'wcfi_combined_blind_inputs':
            from ..audio.full_mix_combined import run as score
        elif kind == 'wcfi_balance_blind_inputs':
            from ..audio.balance_benchmark import run as score
        else:
            from ..audio.full_mix_benchmark import run as score
        return score(directory, output, progress=lambda v: typer.echo(v, err=True))
    execute(work)


@app.command('report')
def report(directory: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
           run_directory: Annotated[Path, typer.Option(exists=True, file_okay=False)],
           package: Annotated[Path | None, typer.Option(help='Optionally export a new app package.')] = None):
    """Unblind results, expose failed gates and optionally package app auditions."""
    def work():
        from ..audio.full_mix_benchmark import load
        kind = load(directory/'blind-inputs.json').get('kind')
        if kind == 'wcfi_combined_blind_inputs':
            from ..audio.full_mix_combined import export_package, summarize
        elif kind == 'wcfi_balance_blind_inputs':
            from ..audio.balance_benchmark import export_package, summarize
        else:
            from ..audio.full_mix_benchmark import export_package, summarize
        result = (export_package(directory, run_directory, package)['reports']['MIX'] if package
                  else summarize(directory, run_directory))
        return {'control_gate_passed': result['control_gate_passed'],
                'professional_quality_judgment_validated': False,
                'checks_passed': sum(c['passed'] for c in result['checks']), 'checks': len(result['checks']),
                'sessions': [{k: s[k] for k in ('id', 'best_candidate', 'automatic_recommendation', 'improvement_over_current')}
                             for s in result['sessions']], 'runtime': result['runtime']}
    execute(work)
