# Contribute to Shadow

Use Python 3.10 or later. CI checks Python 3.11 and 3.12.

## Set up a checkout

```bash
git clone https://github.com/akinduroifedayo/shadow.git
cd shadow
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

On Windows, activate with `.venv\Scripts\activate`.

Shadow has no runtime dependencies and ships no `pyproject.toml` on purpose: Hermes treats a plugin's `pyproject.toml` as Python dependencies to resolve, which adds work to every install and conflicts when the same plugin is installed in two profiles. Hermes supplies the host APIs when it loads the plugin. Use the README's `hermes plugins install` command for plugin installation. Run the tests from the repository root.

## Check a change

Run both checks from the repository root:

```bash
python -m pytest -q
python acceptance/run.py
```

The acceptance harness runs local checks without provider traffic. Some checks report `SKIP(needs-mac)` because they need a live Hermes profile on macOS. See [the acceptance guide](acceptance/README.md).

Tests that inspect Hermes source skip unless `SHADOW_HERMES_REF` points to a Hermes checkout. To include those checks:

```bash
SHADOW_HERMES_REF=/path/to/hermes-agent python -m pytest -q
```

Desktop interaction tests require Node.js. Live model checks use your configured provider tokens. Never commit credentials, private conversation text, or local machine paths.

## Edit copy

User-facing copy keys live in `shadow/copy/*.json`. Keep keys unique across files. Update the affected copy fixtures when text changes. The shared loader rejects duplicate keys and non-text values.

## Open a pull request

Describe the user-visible problem and the resulting behavior. Keep the change focused. Include the commands you ran and their outcomes. Explain skipped checks and any setup needed to reproduce a failure.

Add a regression test for a behavior fix. Include before-and-after screenshots for Desktop changes. Update docs when commands, permissions, judging, pricing, or data handling change. Keep the replay blocking layers and explicit Start consent intact.

Report suspected vulnerabilities privately through [GitHub security advisories](https://github.com/akinduroifedayo/shadow/security/advisories/new). Follow [the code of conduct](CODE_OF_CONDUCT.md).
