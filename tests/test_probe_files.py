import pytest

from shadow.trial.probe import memory_files, memory_snippet, profile_snapshot


@pytest.mark.parametrize('location', ['MEMORY.md', 'memories/note.md', 'memories'])
def test_memory_scan_never_reads_symlink_targets(tmp_path, monkeypatch, location):
    home = tmp_path / 'home'
    home.mkdir()
    target = tmp_path / 'private-fixture'
    target.write_text('This synthetic private text must never be opened as memory.')
    path = home / location
    path.parent.mkdir(parents=True, exist_ok=True)
    if location == 'memories':
        folder = tmp_path / 'external'
        folder.mkdir()
        (folder / 'note.md').write_text(target.read_text())
        target = folder
    path.symlink_to(target)
    assert memory_files(home) == []
    assert memory_snippet(home) is None
    assert profile_snapshot(home)['memory'] == {}


def test_memory_replaced_by_symlink_between_scan_and_open_is_rejected(tmp_path, monkeypatch):
    import shadow.trial.probe as probe
    note = tmp_path / 'MEMORY.md'
    note.write_text('Original memory text that is sufficiently long.')
    private = tmp_path / 'private-fixture'
    private.write_text('Synthetic private target that must never be read.')

    def raced_scan(_home):
        note.unlink(missing_ok=True)
        note.symlink_to(private)
        return [note]

    monkeypatch.setattr(probe, 'memory_files', raced_scan)
    assert memory_snippet(tmp_path) is None
    with pytest.raises(OSError):
        profile_snapshot(tmp_path)


def test_result_readers_only_see_complete_publications(tmp_path, monkeypatch):
    import io
    import json
    from shadow.trial import probe
    path = probe.result_path(tmp_path, 'atomic')
    path.write_text('{"passed": false}')
    open_file = io.open
    observed = []

    class InterruptedWriter:
        def __init__(self, handle):
            self.handle = handle
        def __getattr__(self, name):
            return getattr(self.handle, name)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.handle.__exit__(*args)
        def write(self, text):
            self.handle.write(text[:1])
            self.handle.flush()
            with open_file(path) as reader:
                observed.append(json.load(reader))
            return 1 + self.handle.write(text[1:])

    def interrupted_open(file, mode='r', *args, **kwargs):
        handle = open_file(file, mode, *args, **kwargs)
        return InterruptedWriter(handle) if 'w' in mode else handle

    monkeypatch.setattr(io, 'open', interrupted_open)
    probe.write_result(tmp_path, 'atomic', {'passed': True}, sessions=['controller', 'child'])
    assert observed == [{'passed': False}]
    assert json.loads(path.read_text())['passed'] is True
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


def test_result_poll_retries_partial_json_until_publication(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from shadow.trial import probe
    job = {'id': 'poll'}
    path = probe.result_path(tmp_path, job['id'])
    path.write_text('{')
    clock = [100.0]
    pauses = []
    monkeypatch.setattr(probe.time, 'time', lambda: clock[0])

    def publish(delay):
        pauses.append(delay)
        clock[0] += delay
        path.write_text(json.dumps({'passed': True}))

    result = probe.wait_for_result(SimpleNamespace(directory=tmp_path), job, timeout=1, pause=publish)
    assert result == {'passed': True}
    assert pauses == [0.2]


def test_result_poll_stops_at_deadline_for_invalid_json(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from shadow.trial import probe
    job = {'id': 'invalid'}
    probe.result_path(tmp_path, job['id']).write_text('{')
    clock = [100.0]
    monkeypatch.setattr(probe.time, 'time', lambda: clock[0])
    pauses = []

    def tick(delay):
        pauses.append(delay)
        clock[0] += delay

    assert probe.wait_for_result(SimpleNamespace(directory=tmp_path), job, timeout=0.5, pause=tick) is None
    assert len(pauses) == 3


@pytest.mark.parametrize('kind', ['job', 'result'])
def test_setup_artifacts_are_private_from_creation(tmp_path, monkeypatch, kind):
    import io
    import os
    import stat
    from shadow.trial import probe
    open_file = io.open
    modes = []

    def inspect_creation(file, mode='r', *args, **kwargs):
        handle = open_file(file, mode, *args, **kwargs)
        if 'w' in mode:
            modes.append(stat.S_IMODE(os.fstat(handle.fileno()).st_mode))
        return handle

    monkeypatch.setattr(io, 'open', inspect_creation)
    previous_umask = os.umask(0)
    try:
        if kind == 'job':
            probe.write_job(tmp_path, {'id': 'private', 'controller': 'fixture-token'})
        else:
            probe.write_result(tmp_path, 'private', {'passed': False}, sessions=[])
    finally:
        os.umask(previous_umask)
    assert modes == [0o600]


@pytest.mark.parametrize('symlink', [False, True])
def test_job_creation_refuses_existing_paths_without_changing_them(tmp_path, symlink):
    from shadow.trial import probe
    path = probe.job_path(tmp_path, 'existing')
    target = tmp_path / 'keep.txt' if symlink else path
    target.write_text('Keep this existing file.')
    if symlink:
        path.symlink_to(target)
    with pytest.raises(FileExistsError):
        probe.write_job(tmp_path, {'id': 'existing', 'controller': 'fixture-token'})
    assert target.read_text() == 'Keep this existing file.'
    assert path.is_symlink() is symlink


def test_unreadable_memory_is_not_reported_as_empty(tmp_path):
    note = tmp_path / 'MEMORY.md'
    note.write_text('Saved private memory.')
    note.chmod(0)
    try:
        assert memory_snippet(tmp_path) is None
    finally:
        note.chmod(0o600)


def test_oversized_memory_is_not_reported_as_empty(tmp_path):
    folder = tmp_path / 'memories'
    folder.mkdir()
    (folder / 'MEMORY.md').write_bytes(b'x' * 1_000_001)
    assert memory_snippet(tmp_path) is None


@pytest.mark.parametrize('location', ['memories', 'memories/nested'])
def test_unreadable_memory_directory_is_not_reported_as_empty(tmp_path, location):
    folder = tmp_path / location
    folder.mkdir(parents=True)
    (folder / 'MEMORY.md').write_text('Saved private memory.')
    folder.chmod(0)
    try:
        assert memory_snippet(tmp_path) is None
    finally:
        folder.chmod(0o700)
