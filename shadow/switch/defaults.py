from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from .domain import Route


class DefaultWriter:
    def __init__(self, profile_home: Path, *, runner=subprocess.run, config_reader=None):
        self.profile_home = Path(profile_home).resolve()
        self.profile = self.profile_home.name if self.profile_home.parent.name == "profiles" else "default"
        self.runner = runner
        self.config_reader = config_reader

    def __call__(self, route: Route):
        previous = dict(self.config_reader().get("model", {})) if self.config_reader else {}
        try:
            self._write(route.provider, route.model)
        except (RuntimeError, OSError):
            if previous.get("provider") and previous.get("default"):
                self._write(previous["provider"], previous["default"])
            raise

    def _write(self, provider, model):
        env = dict(os.environ, HERMES_HOME=str(self.profile_home))
        for setting, value in (("model.provider", provider), ("model.default", model)):
            try:
                result = self.runner([sys.executable, "-m", "hermes_cli.main", "-p", self.profile, "config", "set", setting, value],
                                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
            except subprocess.TimeoutExpired:
                raise RuntimeError("model defaults were not saved") from None
            if result.returncode:
                raise RuntimeError("model defaults were not saved")
