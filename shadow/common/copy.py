import json
from pathlib import Path


def load_copy(directory=None):
    merged = {}
    for path in sorted(Path(directory or Path(__file__).parents[1] / 'copy').glob('*.json')):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f'duplicate copy key: {key}')
                result[key] = value
            return result
        values = json.loads(path.read_text(), object_pairs_hook=unique)
        for key, value in values.items():
            if key in merged:
                raise ValueError(f'duplicate copy key: {key}')
            if not isinstance(value, str):
                raise ValueError(f'copy must be text: {key}')
            merged[key] = value
    return merged


def copy(key, **fields):
    return load_copy()[key].format(**fields)
