#!/usr/bin/env python3
"""Reset command for the fixture app: write the seed data again. Takes no arguments.

Env, for tests only: FIXTURE_RESET_FAIL=1 exits with 3. FIXTURE_RESET_SLEEP=<seconds> waits first (a timeout test).
"""
import os
import sys
import time

from fixture_app import SEED, save

if os.environ.get("FIXTURE_RESET_SLEEP"):
    time.sleep(float(os.environ["FIXTURE_RESET_SLEEP"]))
if os.environ.get("FIXTURE_RESET_FAIL") == "1":
    sys.exit(3)
save(SEED)
