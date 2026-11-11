import json
import re
from collections import Counter
from functools import lru_cache


_CREDENTIAL_FIELDS = {
    'api_key': 'key', 'apikey': 'key', 'private_key': 'key', 'secret': 'key',
    'client_secret': 'key', 'password': 'key', 'pass': 'key', 'passwd': 'key',
    'pwd': 'key', 'secret_key': 'key', 'access_key': 'key',
    'access_key_id': 'key', 'secret_access_key': 'key',
    'token': 'token', 'access_token': 'token', 'refresh_token': 'token',
    'id_token': 'token', 'auth_token': 'token', 'api_token': 'token',
    'bearer_token': 'token', 'session_token': 'token', 'authorization': 'token',
}
_VALUE_PATTERN = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s"']+)'''
_VALUE = re.compile(_VALUE_PATTERN)
_PATTERNS = (
    ('key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S)),
    ('key', re.compile(r'\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{12,}|(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{15,}|github_pat_[A-Za-z0-9_]{15,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]+)\b')),
    ('token', re.compile(r'''(?i)(?P<quote>["'])(?:proxy-)?authorization[ \t]*:[ \t]*(?:\\.|(?!(?P=quote))[^\r\n\\])*(?P=quote)''')),
    ('token', re.compile(r'''(?i)\b(?:proxy-)?authorization["']?[ \t]*[=:][ \t]*[^\r\n]+''')),
    ('token', re.compile(r'(?i)\bBearer[ \t]+[^\s"\'<>]+')),
    ('token', re.compile(r'\b(?:Basic|Token|Digest)[ \t]+(?:(?=[A-Za-z0-9+/=._~-]*\d)[A-Za-z0-9+/=._~-]{8,}|[A-Za-z0-9+/=._~-]{7,}=)')),
    ('credential', re.compile(r'''(?<!\S)(?:-u|--user)[ \t]+(?:"[^"\s:]+:[^"]*"|'[^'\s:]+:[^']*'|[^\s:'"]+:\S+)''')),
    ('key', re.compile(r'(?i)(?<!\S)--(?:password|passwd|pass|token|secret)[ \t]+' + _VALUE_PATTERN)),
    ('field', re.compile(r'''(?<![A-Za-z0-9_-])([A-Za-z0-9_-]+)["']?[ \t]*[=:][ \t]*''')),
    ('credential', re.compile(r'(?i)[a-z][a-z0-9+.-]*://[^\s/@:]*:[^\s/@]+@')),
    ('token', re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b')),
    ('email', re.compile(r'\b[A-Za-z0-9.!#$%&*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')),
    ('phone', re.compile(r'(?<!\w)\+\d{1,3}(?:[ .-]?\d){7,14}(?!\w)')),
    ('phone', re.compile(r'(?<!\w)(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]?\d{3}[ .-]?\d{4}(?!\w)')),
    ('key', re.compile(r'\b(?![0-9a-fA-F]{40,}\b)(?=[A-Za-z0-9_+/=-]*[a-z])(?=[A-Za-z0-9_+/=-]*[A-Z])(?=[A-Za-z0-9_+/=-]*\d)[A-Za-z0-9_+/=-]{40,}\b')),
)


@lru_cache(maxsize=1024)
def credential_kind(key):
    name = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', str(key)).lower().replace('-', '_')
    if name == 'pwd':
        return None
    for field, kind in _CREDENTIAL_FIELDS.items():
        if re.search(r'(?:^|_)' + re.escape(field) + r'(?:_|$)', name):
            return kind
    if any(segment.endswith(('password', 'passwd', 'secret', 'apikey')) for segment in name.split('_')):
        return 'key'
    return None


class Scrubber:
    def __init__(self):
        self.counts = Counter()
        self.text_cache = {}

    def text(self, value):
        cached = self.text_cache.get(value)
        if cached is not None:
            clean, counts = cached
            self.counts.update(counts)
            return clean
        before = self.counts.copy()
        clean = self._text(value)
        if len(value) <= 1024 and len(self.text_cache) < 512:
            self.text_cache[value] = clean, self.counts - before
        return clean

    def _text(self, value):
        if value.lstrip().startswith(('{', '[')):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass
            else:
                return json.dumps(self.scrub(parsed), ensure_ascii=False)
        for kind, pattern in _PATTERNS:
            if kind == 'field':
                value = self._fields(value, pattern)
                continue
            value, count = pattern.subn(f'<{kind}>', value)
            self.counts[kind] += count
        return value

    def _fields(self, value, pattern):
        pieces, cursor = [], 0
        for match in pattern.finditer(value):
            if match.start() < cursor:
                continue
            kind = credential_kind(match[1])
            start = match.end()
            while start < len(value) and value[start] == '=':
                start += 1
            while start < len(value) and value[start] in ' \t':
                start += 1
            field_value = _VALUE.match(value, start) if kind else None
            if field_value is None:
                continue
            pieces.extend((value[cursor:match.start()], f'<{kind}>'))
            cursor = field_value.end()
            self.counts[kind] += 1
        pieces.append(value[cursor:])
        return ''.join(pieces)

    def scrub(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            clean = {}
            for key, item in value.items():
                kind = credential_kind(key)
                if kind and item not in (None, '', f'<{kind}>'):
                    self.counts[kind] += 1
                    clean[self.text(str(key))] = f'<{kind}>'
                else:
                    clean[self.text(str(key))] = self.scrub(item)
            return clean
        if isinstance(value, (list, tuple)):
            return [self.scrub(v) for v in value]
        if value is None or type(value) in (bool, int, float):
            return value
        raise TypeError('unsupported outbound value')


def outbound(payload, scrubber=None):
    allowed = {'goal', 'context', 'answers', 'question'}
    return (scrubber or Scrubber()).scrub({k: v for k, v in payload.items() if k in allowed})
