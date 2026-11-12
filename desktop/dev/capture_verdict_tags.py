import sys
from pathlib import Path

from capture_cli import record_for
from shadow.trial.cards import render_verdict


CASES = [
    ('switch', 'better', 'safe-to-switch'),
    ('switch', 'holds-up', 'saves-money'),
    ('switch', 'holds-up', 'sub-percent-saves-money'),
    ('switch', 'holds-up', 'equal-safe'),
    ('switch', 'better', 'trial-ahead'),
    ('switch', 'holds-up', 'even'),
    ('switch', 'too-close', 'too-close'),
    ('switch', 'worse', 'stay'),
    ('compare', 'better', 'trial-ahead'),
    ('compare', 'worse', 'current-ahead'),
    ('compare', 'holds-up', 'trial-less'),
    ('compare', 'holds-up', 'sub-percent-trial-less'),
    ('compare', 'holds-up', 'current-less'),
    ('compare', 'holds-up', 'sub-percent-current-less'),
    ('compare', 'holds-up', 'even'),
    ('compare', 'too-close', 'too-close'),
    ('switch', 'not-enough', 'not-enough'),
    ('compare', 'not-enough', 'not-enough'),
    ('switch', 'better', 'current-plan-trial-ahead'),
    ('switch', 'holds-up', 'current-plan-even'),
    ('switch', 'better', 'modest-premium-trial-ahead'),
    ('switch', 'holds-up', 'modest-premium-even'),
    ('compare', 'holds-up', 'modest-premium-current-less'),
]


def main():
    output = []
    for intent, scenario, label in CASES:
        record = record_for(scenario)
        record['intent'] = intent
        receipt = record['totals']['receipt']
        if label.startswith('current-plan-'):
            receipt.update(plan_new=False, plan_yours=True, cost_mode='list_price',
                           cost_per_turn_new=.03, cost_per_turn_yours=.02, saved_pct=None, more_pct=50)
            record['current']['plan'] = True
        elif label.startswith('modest-premium-'):
            receipt.update(cost_per_turn_new=.021, cost_per_turn_yours=.02, saved_pct=None, more_pct=5)
        elif label in {'sub-percent-saves-money', 'sub-percent-trial-less'}:
            receipt.update(cost_per_turn_new=.01992, cost_per_turn_yours=.02, saved_pct=0, more_pct=None)
        elif label == 'sub-percent-current-less':
            receipt.update(cost_per_turn_new=.02008, cost_per_turn_yours=.02, saved_pct=None, more_pct=1)
        elif label == 'equal-safe':
            receipt.update(cost_per_turn_new=.02, cost_per_turn_yours=.02, saved_pct=0, more_pct=0)
        elif label == 'current-less' or intent == 'switch' and label in {'trial-ahead', 'even'}:
            receipt.update(cost_per_turn_new=.036, cost_per_turn_yours=.014, saved_pct=None, more_pct=158)
        elif label == 'even':
            receipt.update(cost_per_turn_new=.036, cost_per_turn_yours=.036, saved_pct=None, more_pct=0)
        output.extend([f'=== {intent} / {label} ===', render_verdict(record, cli=True), ''])
    destination = Path(sys.argv[1])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text('\n'.join(output))
    print(destination)


if __name__ == '__main__':
    main()
