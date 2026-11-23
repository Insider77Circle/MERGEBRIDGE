#!/bin/sh
# Build (if needed) and run the MERGEBRIDGE proof suite.
set -e
cd "$(dirname "$0")"
export PATH="$HOME/.cargo/bin:$PATH"
if [ ! -x target/release/replica ]; then
  cargo build --release
fi
exec python3 tests/run_tests.py
