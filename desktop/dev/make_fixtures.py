"""Write desktop/fixtures/<scenario>.json: whole Shadow states in the trial and switch record shapes.

Counts and receipts are the design's golden rows (design 3.6, _work/gen_verdict.py); totals use the
spec 5.7 numbers so every number the page shows can be checked against its record.

    python3 desktop/dev/make_fixtures.py
"""
import json
from copy import deepcopy
from fixture_results import receipt, stats
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / 'fixtures'
NOUS = {'provider': 'nous', 'provider_name': 'Nous Portal'}
NEW = {'model': 'nousresearch/hermes-4-70b-fp8-2026-08-14', 'name': 'Hermes 4 70B', 'maker': 'Nous Research', **NOUS}
OLD = {'model': 'nousresearch/hermes-4-405b', 'name': 'Hermes 4 405B', 'maker': 'Nous Research', **NOUS}
QWEN = {'model': 'qwen/qwen-3.5-72b', 'name': 'Qwen 3.5 72B', 'maker': 'Alibaba', 'provider': 'openrouter', 'provider_name': 'OpenRouter'}
DEEPSEEK = {'model': 'deepseek/deepseek-v3.2', 'name': 'DeepSeek V3.2', 'maker': 'DeepSeek', **NOUS}
SIGNALS = ['failed', 'stopped', 'retried', 'malformed']

# first line, ISO day, source
TURNS = [
    ('Clean up the old backup folders on the NAS, keep the last 3', '2026-11-03T10:12:00', 'desktop'),
    ('Refactor the token refresh in auth.ts so it retries once', '2026-11-03T15:40:00', 'desktop'),
    ('Summarize the three PDFs in ~/Reading/agents and compare', '2026-11-04T09:05:00', 'desktop'),
    ('Why is the nightly cron for the price scraper not firing?', '2026-11-05T08:30:00', 'cli'),
    ('Draft a Slack message to Sam about moving the launch', '2026-11-06T11:20:00', 'telegram'),
    ('Find every place we still call the v1 billing API', '2026-11-02T14:02:00', 'desktop'),
    ('Rename the photos in ~/Desktop/trip by date taken', '2026-11-05T19:45:00', 'desktop'),
    ('Write a Python script that dedupes the contacts CSV', '2026-11-02T16:10:00', 'desktop'),
    ('Explain why this regex misses multi-line comments', '2026-11-07T10:00:00', 'desktop'),
    ('Plan 3 days in Lisbon with one museum a day', '2026-11-08T12:30:00', 'telegram'),
    ('Bump the plugin hooks from 0.9 to 0.10 and fix the breakage', '2026-11-09T09:15:00', 'desktop'),
    ('What changed in the auth service between v2.3 and v2.4?', '2026-11-03T13:00:00', 'desktop'),
    ('Add a retry with backoff to the webhook sender', '2026-11-04T17:20:00', 'desktop'),
    ('Turn these meeting notes into a short action list', '2026-11-04T11:00:00', 'desktop'),
    ('Check which of my repos still pin Node 18', '2026-11-05T10:40:00', 'cli'),
    ('Write unit tests for the date parser in utils/dates.ts', '2026-11-06T09:30:00', 'desktop'),
    ('Find the biggest files in ~/Downloads and list them', '2026-11-06T18:05:00', 'desktop'),
    ('Translate the README intro into Spanish', '2026-11-07T15:10:00', 'desktop'),
    ('Why does the build fail only on CI?', '2026-11-09T10:50:00', 'desktop'),
    ('Make the settings page form validate emails', '2026-11-02T10:25:00', 'desktop'),
    ('Sort out the merge conflict in package.json', '2026-11-03T09:00:00', 'cli'),
    ('Draft a reply to the landlord about the broken heater', '2026-11-08T20:00:00', 'telegram'),
    ('List the open PRs that touch the billing module', '2026-11-04T14:30:00', 'desktop'),
    ('Convert the CSV export to JSON with the same keys', '2026-11-05T13:15:00', 'desktop'),
    ('Compare the two pricing pages and list differences', '2026-11-06T13:40:00', 'desktop'),
    ("Write a bash alias that opens today's notes file", '2026-11-07T08:20:00', 'cli'),
    ('Explain this stack trace from the worker logs', '2026-11-09T16:00:00', 'desktop'),
    ('Rename the exported function getUser to fetchUser everywhere', '2026-11-02T11:45:00', 'desktop'),
    ("Summarize this week's commits for the changelog", '2026-11-09T17:30:00', 'desktop'),
    ('Set up a cron that backs up ~/notes every night', '2026-11-08T09:10:00', 'desktop'),
]
LOST_REASONS = [
    ('weaker_action', 'it keeps the 3 newest as asked; Answer 1 would delete all of them.'),
    ('missed_request', 'no retry on the second call.'),
    ('wrong_facts', "mixed up two papers' setups."),
    ('weaker_action', 'restarted the job without reading the log.'),
    ('harder_to_use', 'about the same message; the shorter one reads better.'),
]
NAS_RUNS = {
    'new': {'tool_calls': [{'name': 'search_files', 'args': {'path': '/Volumes/nas/backups'}, 'status': 'ok', 'note': '9 folders'},
                           {'name': 'terminal', 'args': {'command': 'rmdir /Volumes/nas/backups/2026-*'}, 'status': 'not_run'}],
            'summary': "I'll remove the dated backup folders under /Volumes/nas/backups once each one is empty."},
    'yours': {'tool_calls': [{'name': 'search_files', 'args': {'path': '/Volumes/nas/backups'}, 'status': 'ok', 'note': '9 folders'},
                             {'name': 'read_file', 'args': {'path': 'backups/README.md'}, 'status': 'ok'},
                             {'name': 'terminal', 'args': {'command': 'ls -1td /Volumes/nas/backups/*/ | tail -n +4 | xargs rm -rf'}, 'status': 'not_run'}],
              'summary': 'This deletes every backup folder except the 3 newest by date. The README says nothing else depends on them.'},
}
NAS_ORIGINAL = ('I listed /Volumes/nas/backups and found 9 dated folders. Keeping the 3 newest (2026-11-01, 2026-10-25, '
                '2026-10-18), the command to remove the other 6 is ready; say go and I will run it.')


def generic_run(i, side):
    paths = ['src/auth.ts', 'notes/2026-11.md', 'README.md', 'package.json', 'scripts/backup.sh', 'docs/pricing.md']
    path = paths[(i + (side == 'new')) % len(paths)]
    calls = [{'name': 'read_file', 'args': {'path': path}, 'status': 'ok'}]
    if i % 3 == 0:
        calls.append({'name': 'search_files', 'args': {'query': TURNS[i][0].split()[1]}, 'status': 'ok'})
    lead = 'Here is a short plan' if side == 'new' else 'Here is what I would do'
    return {'tool_calls': calls, 'summary': f'{lead}: I read {path} first, then answered the request in three steps.'}


def turn(i, result=None, state='done', reason=None):
    first, at, source = TURNS[i]
    runs = NAS_RUNS if i == 0 else {side: generic_run(i, side) for side in ('new', 'yours')}
    t = {'id': f't{i + 1:02d}', 'first_line': first, 'goal': first + ('.' if not first.endswith('?') else ''), 'at': at,
         'source': source, 'state': state, 'usable': state == 'done',
         'runs': {side: [runs[side]] if state in {'done', 'judging'} else [] for side in ('new', 'yours')}}
    if state == 'done':
        kind, text = reason or ('missed_request', 'the other answer missed part of the request.')
        t['judgment'] = {'result': result, 'reason_kind': kind, 'reason': text, 'agree': True, 'disagreement': False}
    if i == 0:
        t['original_answer'] = NAS_ORIGINAL
    return t


def finished_turns(won, same, lost, n=30):
    order = ['lost'] * min(lost, 5) + ['won'] * won + ['lost'] * max(0, lost - 5) + ['same'] * same
    turns = []
    for i, result in enumerate(order[:n]):
        reason = LOST_REASONS[i] if result == 'lost' and i < 5 else None
        turns.append(turn(i, result, reason=reason))
    return turns


def test(tid, created, w, s, l, *, agree, rec, picked=30, judge_mode='third_maker', state='finished', **extra):
    stop = extra.pop('stop_reason', None)
    totals = stats(w, s, l, agree, stop_reason=stop)
    totals['receipt'] = rec
    turns = finished_turns(w, s, l)
    for i in range(len(turns), picked):
        turns.append({**turn(i, state='done'), 'state': 'unusable', 'usable': False, 'unusable_reason': 'both_failed'})
    base = {'id': tid, 'created_at': created, 'origin': {'session_id': 'local-demo', 'source': 'desktop'},
            'current': OLD, 'trial': NEW, 'judge': {'mode': judge_mode, 'models': [DEEPSEEK] if judge_mode == 'third_maker' else [NEW, OLD]},
            'judge_mode': judge_mode, 'judge_source': 'auto', 'judge_disagreements': 4 if judge_mode == 'backup_both' else 0,
            'seed': 7, 'days': 7, 'total': 212, 'picked': picked, 'cap': 5.0, 'spend': 1.84, 'web_lookups': True,
            'web_lookup_turns': 7, 'state': state, 'stop_reason': stop, 'turns': turns, 'totals': totals}
    base.update(extra)
    base['routes'] = {side: {**{key: base[side][key] for key in ('provider', 'model')},
                              'base_url': 'https://fixture.invalid/v1', 'api_mode': 'chat_completions'}
                      for side in ('current', 'trial')}
    return base


def holds_up(tid='b71c', created='2026-11-10T16:12:00', **extra):
    rec = receipt(30, cost=(0.014, 0.036), speed=(8.8, 13.7), steps=(3.4, 3.6), broken=(1, 1), fixes=(4, 4),
                  where_won=('missed_request', 3), where_lost=('weaker_action', 2))
    return test(tid, created, 6, 19, 5, agree=26, rec=rec,
                blind={'judge3': ['t01', 't07', 't04'], 'sides': {'t01': 'new', 't07': 'yours', 't04': 'new'}}, **extra)


VARIANTS = {
    'better': lambda: test('a3f2', '2026-11-10T16:12:00', 15, 11, 4, agree=27, intent='switch', rec=receipt(
        30, cost=(0.014, 0.036), speed=(8.6, 13.9), steps=(3.2, 3.8), broken=(0, 1), fixes=(3, 6),
        where_won=('missed_request', 6), where_lost=('tool_details', 3))),
    'worse': lambda: test('c09e', '2026-11-10T16:12:00', 3, 9, 18, agree=28, spend=1.79, rec=receipt(
        30, cost=(0.014, 0.036), speed=(7.9, 13.8), steps=(4.6, 3.7), broken=(7, 1), fixes=(14, 5),
        where_won=('stopped_early', 2), where_lost=('tool_details', 11))),
    'too-close': lambda: test('d4a8', '2026-11-10T16:12:00', 4, 20, 6, agree=25, more_estimate=1.90, rec=receipt(
        30, cost=(0.014, 0.036), speed=(8.9, 13.6), steps=(3.5, 3.4), broken=(1, 0), fixes=(5, 4),
        where_won=('wrong_facts', 2), where_lost=('missed_request', 3))),
    'not-enough': lambda: test('e5f1', '2026-11-10T16:12:00', 7, 8, 4, agree=16, spend=5.0, more_estimate=7.90,
                               web_lookup_turns=4, stop_reason='cap', rec=receipt(
        19, cost=(0.068, 0.172), speed=(10.2, 15.4), steps=(3.1, 3.3), broken=(1, 1), fixes=(3, 2),
        where_won=('missed_request', 3), where_lost=('weaker_action', 2), readable='so_far')),
    'backup': lambda: holds_up('b8e0', judge_mode='backup_both'),
}

START = {
    'try': NEW['model'], 'current': OLD,
    'models': [NEW, DEEPSEEK, QWEN,
               {'model': 'moonshot/kimi-k2', 'name': 'Kimi K2', 'maker': 'Moonshot', **NOUS},
               {'model': 'zhipu/glm-4.6', 'name': 'GLM 4.6', 'maker': 'Zhipu', 'provider': 'openrouter', 'provider_name': 'OpenRouter'}],
    'facts': {'total': 212, 'days': 7, 'picked': 30, 'left_out': {'too_long': 61, 'too_short': 48, 'email': 12},
              'scrub': {'keys': 2, 'emails': 5},
              'backup_judge_costs': {NEW['model']: .30, OLD['model']: .45, DEEPSEEK['model']: .40,
                                     QWEN['model']: .35, 'moonshot/kimi-k2': .55, 'zhipu/glm-4.6': .45},
              'replay_estimates': {DEEPSEEK['model']: 1.20, 'moonshot/kimi-k2': 1.65,
                                   QWEN['model']: 1.35, 'zhipu/glm-4.6': 1.40},
              'replay_estimate': 1.50, 'cap': 5.0, 'minutes': 25, 'hidden_maker': 'Hermes',
              'judges': [{**DEEPSEEK, 'cost': 0.40, 'auto': True},
                         {'model': 'moonshot/kimi-k2', 'name': 'Kimi K2', 'maker': 'Moonshot', **NOUS, 'cost': 0.55},
                         {**QWEN, 'cost': 0.35},
                         {'model': 'zhipu/glm-4.6', 'name': 'GLM 4.6', 'maker': 'Zhipu', 'provider': 'openrouter',
                          'provider_name': 'OpenRouter', 'cost': 0.45}]},
}
NOTICES = {'keep': 'Kept Hermes 4 405B. Nothing changed.',
           'doctor': 'Setup check: all three plugin permissions are on, 4 other models to try, and a judge from a third maker.'}


def world(tests=(), switch=None, start=True, **extra):
    return {'settings': {'web_lookups': True}, 'tests': list(tests), 'switch': switch,
            'start': START if start else None, 'notices': NOTICES, **extra}


def progress_test():
    t = holds_up('a3f2', '2026-11-10T15:47:00', state='running', spend=0.71)
    t.pop('blind', None)
    order = [0, 5, 1, 6, 7] + list(range(10, 23)) + [3, 8, 9, 4] + list(range(23, 30)) + [2]
    states = ['done'] * 18 + ['judging', 'running', 'running'] + ['waiting'] * 9
    t['turns'] = deepcopy([turn(i, 'same', state=s) if s != 'done' else turn(i, 'same') for i, s in zip(order, states)])
    t['totals'] = None
    for item in t['turns']:
        for side in ('new', 'yours'):
            for run in item['runs'][side]:
                run.update(cost=.014 if side == 'new' else .036, duration_s=8.8 if side == 'new' else 13.7)
    t['live'] = {'minutes_left': 10, 'own_session': True,
                 'running': {'count': 2, 'provider': 'Nous Portal', 'side': 'yours', 'max': 4}}
    return t


def gradual(phase='running'):
    s = {'id': 's1', 'test_id': 'b71c', 'old': OLD, 'new': NEW, 'mode': 'gradual', 'phase': phase,
         'step': 2, 'step_turns': 20, 'step_progress': 11, 'started_at': '2026-11-10T16:20:00',
         'counts': {'new': {'turns': 31, 'trouble': 1}, 'old': {'turns': 211, 'trouble': 3}},
         'failure_streak': 0, 'last_answered': NEW, 'signals': SIGNALS, 'trips': [],
         'history': [{'at': '2026-11-10T16:20:00', 'kind': 'start', 'test': 'b71c', 'word': 'holds_up', 'won': 6, 'same': 19, 'lost': 5},
                     {'at': '2026-11-12T09:02:00', 'kind': 'step', 'share': 2, 'turns': 20},
                     {'at': '2026-11-12T14:14:00', 'kind': 'trouble', 'model': 'Hermes 4 70B', 'what': 'a request failed, the retry finished the turn'}]}
    if phase == 'paused':
        s.update(step=1, step_progress=14, counts={'new': {'turns': 14, 'trouble': 5}, 'old': {'turns': 120, 'trouble': 4}}, last_answered=OLD,
                 trips=[{'kind': 'rate', 'text': 'On Hermes 4 70B, 5 of 14 turns hit trouble (3 failed requests, 2 you retried). '
                                                 'On Hermes 4 405B over the same days, 4 of 120. This can be bad luck. Nothing else changed.'}])
        s['history'] = s['history'][:1] + [{'at': '2026-11-11T18:40:00', 'kind': 'rollback', 'old': 'Hermes 4 405B',
                                             'what': 'trouble on 5 of 14 turns on Hermes 4 70B, 4 of 120 on Hermes 4 405B'}]
    return s


def watch(phase='watch'):
    s = {'id': 's2', 'test_id': 'f20d', 'test_word': 'better', 'old': OLD, 'new': QWEN, 'mode': 'all_at_once', 'phase': phase,
         'step': 1, 'step_turns': 20, 'step_progress': 9, 'started_at': '2026-11-10T16:40:00',
         'counts': {'new': {'turns': 9, 'trouble': 0}}, 'failure_streak': 0, 'last_answered': QWEN, 'signals': SIGNALS, 'trips': [],
         'history': [{'at': '2026-11-10T16:40:00', 'kind': 'watch_start', 'new': 'Qwen 3.5 72B', 'provider': 'OpenRouter', 'test': 'f20d',
                      'word': 'better', 'won': 15, 'same': 11, 'lost': 4, 'old': 'Hermes 4 405B', 'old_provider': 'Nous Portal'}]}
    if phase == 'paused':
        s.update(counts={'new': {'turns': 9, 'trouble': 3}}, failure_streak=3,
                 trips=[{'kind': 'streak', 'text': 'In 9 turns on Qwen 3.5 72B, 3 requests in a row failed. This can be a provider hiccup. Nothing else changed.'}])
    return s


SCENARIOS = {
    'empty': world(),
    'progress': world([progress_test()]),
    'result': world([holds_up(), VARIANTS['worse']() | {'id': 'c09e', 'created_at': '2026-11-08T11:02:00'}]),
    **{f'result-{name}': world([make()]) for name, make in VARIANTS.items()},
    'result-compare': world([deepcopy(VARIANTS['better']()) | {'intent': 'compare'}]),
    'switch': world([holds_up()], gradual()),
    'switch-paused': world([holds_up()], gradual('paused')),
    'watch': world([], watch()),
    'watch-tripped': world([], watch('paused')),
}

if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    for name, state in SCENARIOS.items():
        (OUT / f'{name}.json').write_text(json.dumps(state, indent=1, ensure_ascii=False) + '\n')
        print(OUT / f'{name}.json')
