# watch-oracle-subnet

A Bittensor subnet where miners predict the wholesale floor price of luxury watch references for an upcoming window, and validators score those predictions against a private ground truth oracle once the window has closed.

Structure follows the [opentensor bittensor-subnet-template](https://github.com/opentensor/bittensor-subnet-template). Built against bittensor SDK 11.3.0, which no longer ships Axon/Dendrite/Synapse: miners and validators talk over plain HTTP, authenticated with hotkey-signed requests (`bittensor.http_auth`, btauth/1).

## Mechanism

**Windows**

A scoring window is `--neuron.window_days` long (1 to 7, default 7) and starts at 00:00 UTC. All window lengths are anchored on Monday 1970-01-05:

```
window_start = 1970-01-05 + floor((day - 1970-01-05) / window_days) * window_days
```

So 7-day windows start on Mondays and 1-day windows start at every UTC midnight. A window is closed once `now >= window_start + window_days`.

**Query round** (every `--neuron.round_interval` seconds)

1. The validator fetches the reference universe from the oracle (cached for `--neuron.universe_ttl`, default one day) and samples `--neuron.batch_size` refs.
2. It POSTs a signed `FloorPrediction` (`refs`, `horizon_days`, `window_days`) to `/floor_prediction` on every queryable miner's published axon.
3. Miners verify the signature and reply with `predictions` filled as `{ref: floor_usd}` for the `window_days` window containing `now + horizon_days`.
4. Every (miner, ref) pair is stored in SQLite with `target_window_start`, the start of the window containing `now + horizon_days`.

**Scoring** (every cycle)

Once a target window has closed, the validator fetches its floor for each (ref, window) once, caches it, and scores every prediction:

```
error = |predicted - floor_usd| / floor_usd
score = max(0, 1 - error / error_band)        # error_band defaults to 0.15
```

- A miner's **round score** is the mean of its scores across the refs scored in that round.
- Refs where the oracle returned 404 (`insufficient_data`) are **excluded**, not zeroed.
- A miner that did not respond, or returned a malformed response (bad JSON, non-object predictions, or any non-finite or non-positive value), scores 0 on every ref in that round.
- A requested ref that the miner left out of `predictions` also scores 0 when ground truth exists. Otherwise a miner could answer only the refs it is confident on.

**Weights**

Weights are proportional to each miner's mean round score over the trailing `--neuron.rolling_window_days` (default 28). Miners with no scored rounds in that period get 0. History is keyed by (uid, hotkey), so a re-registered uid starts from zero. Until at least one miner has a positive score, the validator does not set weights. `bt.SetWeights` clips, normalizes and quantizes the weights and switches to commit-reveal automatically when the subnet has it enabled. It fails if fewer miners have non-zero weight than the subnet's `min_allowed_weights`.

## Setup

Requires Python 3.10+.

```bash
git clone <this repo> watch-oracle-subnet
cd watch-oracle-subnet
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env    # then fill in ORACLE_API_KEY
```

Secrets are read from the environment only. Load `.env` into your shell before running a neuron:

```bash
set -a; source .env; set +a
```

`ORACLE_BASE_URL` is optional and defaults to `https://www.watchcomp.ai`.

Run `python tests/smoke.py` for an offline end to end check (mock oracle, no chain, no key needed).

## Running on testnet

bittensor 11 ships `btcli`. Create wallets and get testnet TAO (see the Bittensor docs for the testnet faucet):

```bash
btcli wallet create -w owner
btcli wallet create -w validator
btcli wallet create -w miner
```

Create the subnet (or use an existing netuid), register both hotkeys, and stake to the validator so it gets a validator permit. Run `btcli tx --help` for the exact intent names and flags on your installed version:

```bash
btcli -n test tx register-subnet -w owner
btcli -n test tx burned-register --netuid <NETUID> -w validator -H default
btcli -n test tx burned-register --netuid <NETUID> -w miner -H default
btcli -n test tx add-stake --netuid <NETUID> --hotkey validator/default --amount-tao 10 -w validator
```

Testnet uses 1-day windows and a 1-day horizon so a scoring cycle completes every day. The window length is set by the validator and sent in every request, so miners need no matching flag.

**Validator:**

```bash
python neurons/validator.py \
  --netuid <NETUID> \
  --subtensor.network test \
  --wallet.name validator --wallet.hotkey default \
  --neuron.window_days 1 \
  --neuron.horizon_days 1
```

**Miner:**

```bash
python neurons/miner.py \
  --netuid <NETUID> \
  --subtensor.network test \
  --wallet.name miner --wallet.hotkey default \
  --axon.port 8091 \
  --axon.external_ip <PUBLIC_IP>
```

On first start the miner publishes `<PUBLIC_IP>:8091` on chain, then serves `POST /floor_prediction`. The port must be reachable from the validator.

With 1-day windows and a 1-day horizon, a prediction made today targets tomorrow's window, which closes at 00:00 UTC the day after. The first scores land within two days, then a new window closes every day.

To see a spread of scores, run several miners (each with its own hotkey and port) at different noise levels, e.g. `--miner.noise 0`, `--miner.noise 0.05`, `--miner.noise 0.2`.

The baseline miner reads the oracle directly so the subnet can run end to end. Real miners will not have ground truth access and must bring their own model.

## Config flags

### Shared

| Flag | Default | Description |
| --- | --- | --- |
| `--netuid` | `1` | Subnet netuid |
| `--wallet.name` | `default` | Coldkey wallet name |
| `--wallet.hotkey` | `default` | Hotkey name |
| `--wallet.path` | `~/.bittensor/wallets` | Wallet directory |
| `--subtensor.network` | `finney` | `finney`, `test`, `local`, or a `ws://` endpoint |
| `--logging.level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `--neuron.state_dir` | `~/.bittensor/miners` | Root directory for neuron state |
| `--neuron.epoch_length` | `100` | Blocks between metagraph syncs (and weight sets, for validators) |
| `--neuron.name` | `validator` / `miner` | Used in the state path |

### Validator

| Flag | Default | Description |
| --- | --- | --- |
| `--neuron.window_days` | `7` | Scoring window length in days, 1 to 7, sent to miners in every request. Use `1` on testnet |
| `--neuron.horizon_days` | `7` | Days ahead to predict. Use `1` on testnet |
| `--neuron.batch_size` | `50` | Refs sampled per query round |
| `--neuron.error_band` | `0.15` | Relative error at which a prediction scores 0 |
| `--neuron.rolling_window_days` | `28` | Trailing days of round scores averaged into weights. Must be >= `window_days` |
| `--neuron.round_interval` | `14400` | Seconds between query rounds |
| `--neuron.cycle_interval` | `60` | Seconds between cycles (scoring pass and weight check) |
| `--neuron.universe_ttl` | `86400` | Seconds to cache the oracle universe |
| `--neuron.timeout` | `60` | Per-miner HTTP timeout in seconds |
| `--neuron.vpermit_tao_limit` | `4096` | Permit holders with more stake than this are not queried as miners |
| `--neuron.db_path` | `<state path>/watch_oracle.db` | SQLite database path |
| `--neuron.disable_set_weights` | off | Score but never set weights |

### Miner

| Flag | Default | Description |
| --- | --- | --- |
| `--axon.ip` | `0.0.0.0` | Interface the miner server binds to |
| `--axon.port` | `8091` | Port the miner server binds to |
| `--axon.external_ip` | required | Public IPv4 address published on chain |
| `--axon.external_port` | `--axon.port` | Public port published on chain |
| `--miner.noise` | `0` | Multiply each prediction by a random factor in `[1 - noise, 1 + noise]` |
| `--blacklist.force_validator_permit` | off | Only accept requests from validator permit holders |
| `--blacklist.allow_non_registered` | off | Accept requests from unregistered hotkeys (unsafe) |

### Environment

| Variable | Required | Description |
| --- | --- | --- |
| `ORACLE_API_KEY` | yes | Sent as `x-oracle-key`. Never logged |
| `ORACLE_BASE_URL` | no | Defaults to `https://www.watchcomp.ai` |

## Layout

```
neurons/
  validator.py            Validator entrypoint
  miner.py                Baseline miner entrypoint
watch_oracle/
  protocol.py             FloorPrediction request/response (JSON)
  transport.py            Signed HTTP client (validator) and server (miner)
  oracle_client.py        Ground truth API client (retries, error types)
  base/                   BaseNeuron, BaseMinerNeuron, BaseValidatorNeuron
  utils/config.py         argparse config and validation
  utils/uids.py           Queryable miner selection
  utils/windows.py        Window boundary helpers (UTC, anchored on 1970-01-05)
  validator/forward.py    Query round and scoring pass
  validator/reward.py     Per-prediction scoring and weight vector
  validator/storage.py    SQLite schema and queries
```
