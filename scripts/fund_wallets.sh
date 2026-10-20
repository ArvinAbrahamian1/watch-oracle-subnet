#!/usr/bin/env bash
# Mint localnet test TAO to the subnet's coldkeys through the pow faucet.
# Each faucet call mints 1,000 TAO; owner gets 3 calls to cover the subnet lock cost.
#
# Usage: scripts/fund_wallets.sh [WALLET[:CALLS] ...]   default: owner:3 validator:1 miner:1
# Env:
#   BT_NETWORK      chain endpoint, default ws://127.0.0.1:9944 (what localnet.sh prints)
#   BT_WALLET_PATH  wallet directory, default ~/.bittensor/wallets
#   PYTHON          interpreter with bittensor 11.3.0 installed, default python3
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
targets=("$@")
((${#targets[@]})) || targets=(owner:3 validator:1 miner:1)

"$PYTHON" -c 'import bittensor' 2>/dev/null || {
  echo "error: $PYTHON cannot import bittensor; activate the project venv (pip install -e .)" >&2
  exit 1
}

exec "$PYTHON" "$here/faucet.py" "${targets[@]}"
