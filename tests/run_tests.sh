#!/bin/sh
# Builds the project and runs the whole Zappy test battery.
#   ./tests/run_tests.sh              everything (~2 min)
#   ZAPPY_SKIP_SLOW=1 ./tests/run_tests.sh   without the long end-to-end games
#   ./tests/run_tests.sh -k Broadcast        only the matching tests
cd "$(dirname "$0")/.." || exit 1
make -s all || exit 1
exec python3 tests/test_suite.py "$@"
