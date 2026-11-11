#!/usr/bin/env python3
"""Print the 12 spec section 11 acceptance rows. Exit non-zero on any FAIL."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from acceptance import mac, support
from acceptance.test_ac01_idle import check as check_01
from acceptance.test_ac02_blocking import check as check_02
from acceptance.test_ac03_privacy import check as check_03
from acceptance.test_ac04_judge import check as check_04
from acceptance.test_ac05_budget import check as check_05
from acceptance.test_ac06_real_test import check as check_06
from acceptance.test_ac07_verdict import check as check_07
from acceptance.test_ac08_gradual import check as check_08
from acceptance.test_ac09_rollback import check as check_09
from acceptance.test_ac10_all_at_once import check as check_10
from acceptance.test_ac11_desktop import check as check_11
from acceptance.test_ac12_doctor import check as check_12


CHECKS = (
    check_01, check_02, check_03, check_04, check_05, check_06,
    check_07, check_08, check_09, check_10, check_11, check_12,
)


def main(argv: list[str]) -> int:
    support.MAC = "--mac" in argv
    if support.MAC:
        support.MAC_REPORT = mac.drive()
    results = [check(support.MAC) for check in CHECKS]
    support.print_table(results)
    return 1 if any(result.status == "FAIL" for result in results) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
