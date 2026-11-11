from collections import Counter
from types import SimpleNamespace

import pytest

from shadow.common.copy import copy
from shadow.trial.picking import FOLLOW_UP, export_sessions, pick_turns


def sessions(cwd, conversations=20, turns=5):
    return [{'id': f'chat{c}', 'cwd': str(cwd), 'source': 'cli', 'messages': [m for t in range(turns) for m in ({'id': f'{c}-{t}', 'role': 'user', 'content': f'Please investigate task number {c}-{t} carefully.'}, {'role': 'assistant', 'content': 'A useful answer.'})]} for c in range(conversations)]


def test_same_seed_same_30_and_conversation_limit(tmp_path):
    data = sessions(tmp_path)
    a = pick_turns(data, seed=42)
    b = pick_turns(data, seed=42)
    assert a.turns == b.turns
    assert len(a.turns) == 30
    assert max(Counter(t.conversation_id for t in a.turns).values()) <= 3
    assert a.turns != pick_turns(data, seed=43).turns


def test_filters_and_compacted_history(tmp_path):
    data = sessions(tmp_path, 1, 1)
    data[0]['messages'] = [
        {'role': 'user', 'content': 'Undo must not resurrect this long message.', 'active': False, 'compacted': False},
        {'role': 'assistant', 'content': 'undone', 'active': False, 'compacted': False},
        {'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'hello'},
        {'role': 'user', 'content': 'long text ' * 2000}, {'role': 'assistant', 'content': 'long'},
        {'role': 'user', 'content': 'An archived legitimate message to preserve.', 'active': False, 'compacted': True}, {'role': 'assistant', 'content': 'archived', 'active': False, 'compacted': True},
    ]
    result = pick_turns(data, seed=1)
    assert len(result.turns) == 1
    assert result.left_out == {'trivial': 1, 'too_long': 1}
    assert 'Undo must' not in result.turns[0].context
    assert result.turns[0].goal.startswith('An archived')
    data[0]['cwd'] = None
    assert pick_turns(data, seed=1).left_out['unknown_cwd'] == 3


def test_correct_export_command():
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(stdout='{"id":"fixture"}\n')
    assert export_sessions('demo', runner=run) == [{'id': 'fixture'}]
    assert calls == [['hermes', '-p', 'demo', 'sessions', 'export', '-', '--newer-than', '7d', '--redact']]


def test_failed_setup_session_is_never_selected_for_a_trial(tmp_path):
    data = sessions(tmp_path, conversations=1, turns=2)
    data[0]['messages'][0]['content'] = 'shadow-setup-check:failed-job:controller-token\nRun the setup check.'
    data[0]['messages'][1]['content'] = 'The setup check could not complete.'
    result = pick_turns(data, seed=1)
    assert result.turns == []
    assert result.left_out == {'shadow_session': 2}


@pytest.mark.parametrize('tools_off', [False, True])
def test_earlier_replay_goals_are_left_out_as_shadow_sessions(tmp_path, tools_off):
    data = sessions(tmp_path, conversations=2, turns=1)
    suffix = FOLLOW_UP + ('\n' + copy('trial.tools_unavailable') if tools_off else '') + '\n' + 'a' * 32
    data[0]['messages'][0]['content'] += suffix

    result = pick_turns(data, seed=1)

    assert [turn.id for turn in result.turns] == ['1-0']
    assert result.left_out == {'shadow_session': 1}


@pytest.mark.parametrize('suffix', [FOLLOW_UP, '\n' + 'a' * 32, FOLLOW_UP + '\n' + 'a' * 31,
                                    FOLLOW_UP + '\n' + 'g' * 32, FOLLOW_UP + '\n' + 'a' * 32 + ' user text'])
def test_user_text_without_a_complete_replay_suffix_stays_eligible(tmp_path, suffix):
    data = sessions(tmp_path, conversations=1, turns=1)
    data[0]['messages'][0]['content'] += suffix

    result = pick_turns(data, seed=1)

    assert [turn.id for turn in result.turns] == ['0-0']
    assert result.left_out == {}


def test_second_look_selects_from_all_candidates_with_combined_conversation_limit(tmp_path):
    data = sessions(tmp_path, conversations=40)
    first = pick_turns(data, seed=42)
    second = pick_turns(data, seed=43, previous=first.turns)
    assert len(second.turns) == 30
    assert not {t.id for t in first.turns} & {t.id for t in second.turns}
    assert not {t.goal for t in first.turns} & {t.goal for t in second.turns}
    assert max(Counter(t.conversation_id for t in first.turns + second.turns).values()) <= 3
    assert second.turns == pick_turns(data, seed=43, previous=first.turns).turns
