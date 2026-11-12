"""Capture the real CLI cards and owner confirmation flow using offline fixtures.

python3 desktop/dev/capture_cli.py <output-dir>
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from shadow.common.discovery import ModelCatalog
from shadow.common.models import Model
from shadow.common.storage import parse_trial_record
from shadow.switch import Engine, Proposal, Route, Store
from shadow.switch.plugin import SwitchPlugin
from shadow.trial.cards import render_verdict
from shadow.trial.presentation import verdict_presentation


def record_for(scenario):
    name = 'result' if scenario == 'holds-up' else f'result-{scenario}'
    record = parse_trial_record(json.loads((ROOT / 'desktop' / 'fixtures' / f'{name}.json').read_text())['tests'][0])
    for model in (record['current'], record['trial'], *record['judge']['models']):
        for key in list(model):
            if key not in {'provider', 'model', 'plan', 'reachable'}:
                del model[key]
    record['routes'] = {side: {'provider': record[side]['provider'], 'model': record[side]['model'],
                               'base_url': 'https://fixture.invalid/v1', 'api_mode': 'chat_completions'}
                        for side in ('current', 'trial')}
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('out', type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for scenario in ('worse', 'better', 'too-close'):
        record = record_for(scenario)
        (args.out / f'cli-{scenario}.txt').write_text(render_verdict(record, cli=True, stats=True) + '\n')
    record = record_for('worse')
    models = [Model(**record[side]) for side in ('current', 'trial')]
    routes = [Route(model.provider, model.model, 'https://fixture.invalid/v1', 'chat_completions') for model in models]
    engine = Engine(Store(sqlite3.connect(':memory:')), verified_pair=lambda *_: True, set_default=lambda _: None)
    proposal = Proposal(record['id'], *routes, record['totals']['word'], verdict_presentation(record)['headline'])
    service = SwitchPlugin(SimpleNamespace(), engine, config_reader=lambda: {}, proposal_lookup=lambda _: proposal,
                           catalog_reader=lambda: ModelCatalog.from_models(models[0], models))
    command = f'switch {record["id"]}'
    warning = service.command(command, terminal=True)
    assert engine.record is None
    (args.out / 'cli-switch-warning.txt').write_text(warning + '\n')
    confirmed = service.command(command + ' --anyway', terminal=True)
    assert engine.record.override['by'] == 'owner'
    (args.out / 'cli-switch-confirmed.txt').write_text(confirmed + '\n\n' + service.command('status', terminal=True) + '\n')


if __name__ == '__main__':
    main()
