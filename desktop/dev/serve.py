"""Serve the Shadow page outside Hermes: the real DesktopRpc over FixtureSource, plus the harness files.

    python3 desktop/dev/serve.py [port] [copy_dir ...]

GET  /harness.html?s=<scenario>      the page in a Hermes-like frame
POST /rpc/<scenario>                 {"method": "plugin.shadow-trial.<name>", "params": {...}} -> {"result"} | {"error"}
POST /reset                          fresh in-memory fixtures
"""
import importlib
import importlib.util
import json
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fixture_results import totals_for

ROOT = Path(__file__).resolve().parents[2]
INNER = ROOT / "shadow"


def _load_desktop_rpc():
    """Load the inner package from its directory.

    Hermes names the plugin ``hermes_plugins.<slug>``, so this script must not
    rely on a top-level module called ``shadow``. The inner directory name is
    the package the relative imports inside that directory use.
    """
    package = INNER.name
    if package not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            package, INNER / "__init__.py", submodule_search_locations=[str(INNER)])
        module = importlib.util.module_from_spec(spec)
        module.__package__ = package
        module.__path__ = [str(INNER)]
        sys.modules[package] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{package}.desktop_rpc")


d = _load_desktop_rpc()


def finalize_fixture(record):
    record['totals'] = totals_for(record)
    return record

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
COPY = d.fixture_copy(tuple(Path(p) for p in sys.argv[2:]))
FILES = {'/harness.html': 'desktop/dev/harness.html', '/react.js': 'desktop/dev/react-shim.js',
         '/sdk.js': 'desktop/dev/sdk-shim.js', '/plugin.js': 'desktop/plugin.js'}
servers: dict[str, d.DesktopRpc] = {}


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split('?')[0]
        if path not in FILES:
            return self.send_error(404)
        body = (ROOT / FILES[path]).read_bytes()
        kind = 'text/html' if path.endswith('.html') else 'text/javascript'
        self._send(200, body, kind)

    def do_POST(self):
        length = int(self.headers.get('content-length', 0))
        payload = json.loads(self.rfile.read(length) or b'{}')
        if self.path == '/reset':
            servers.clear()
            return self._json({'ok': True})
        scenario = self.path.removeprefix('/rpc/')
        rpc = servers.setdefault(scenario, d.DesktopRpc(d.FixtureSource(scenario, finalize_fixture=finalize_fixture), COPY))
        name = payload['method'].removeprefix('plugin.shadow-trial.')
        try:
            self._json({'result': rpc.methods()[name](payload.get('params') or {}, session_id=None)})
        except Exception as exc:
            self._json({'error': f'{type(exc).__name__}: {exc}'})

    def _json(self, value):
        self._send(200, json.dumps(value).encode(), 'application/json')

    def _send(self, code, body, kind):
        self.send_response(code)
        self.send_header('content-type', kind)
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    print(f'http://127.0.0.1:{PORT}/harness.html?s=result', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
