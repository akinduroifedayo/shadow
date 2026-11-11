import pytest

from shadow.common.copy import load_copy


@pytest.mark.parametrize('second', [False, True])
def test_duplicate_copy_keys_fail_within_or_across_line_files(tmp_path, second):
    (tmp_path / 'trial.json').write_text('{"label": "First"}' if second else '{"label": "First", "label": "Second"}')
    if second:
        (tmp_path / 'switch.json').write_text('{"label": "Second"}')
    with pytest.raises(ValueError, match='duplicate copy key: label'):
        load_copy(tmp_path)


def test_line_copy_files_form_one_table(tmp_path):
    for name in ('common', 'trial', 'switch'):
        (tmp_path / f'{name}.json').write_text('{"' + name + '": "' + name + ' copy"}')
    assert load_copy(tmp_path) == {name: name + ' copy' for name in ('common', 'trial', 'switch')}
