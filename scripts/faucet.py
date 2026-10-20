"""Mint test TAO on a pow-faucet localnet via ``SubtensorModule.faucet``.

btcli 11.3.0 has no faucet command, and ``btcli call`` only resolves calls in
the SDK's generated (mainnet) catalog, which excludes the faucet. So this
builds the call by name, lets the SDK encode it against the live runtime
metadata, and mines the proof of work itself.

The faucet seal (pallets/subtensor/src/subnets/registration.rs) is
    keccak256(sha256(nonce_le8 || keccak256(block_hash || coldkey_pubkey)))
read as a little-endian U256 that must not overflow when multiplied by
1_000_000, and the block must be fewer than 3 blocks behind the block that
includes the extrinsic. On a fast-blocks chain that window is ~0.5s, so stale
work is expected and simply re-mined. The call is ``Pays::No``, so an empty
coldkey can call it. Each call mints 1,000 TAO.

Usage: python scripts/faucet.py [--network URL] [--wallet-path DIR] owner:3 validator:1 miner:1
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import multiprocessing as mp
import os
import secrets
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import bittensor as bt
from bittensor.result import ChainError
from Crypto.Hash import keccak  # pycryptodome, pulled in by bittensor via eth-account

FAUCET_DIFFICULTY = 1_000_000
FAUCET_TAO_PER_CALL = 1_000
TARGET = ((1 << 256) - 1) // FAUCET_DIFFICULTY
# Chain errors meaning the work went stale or was mined on a reorged block.
STALE_WORK_ERRORS = {"InvalidWorkBlock", "InvalidSeal"}
CHECK_EVERY = 4096

_stop = None


def _keccak(data: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=data).digest()


def _init_worker(stop) -> None:
    global _stop
    _stop = stop


def _mine(block_and_key: bytes, start: int, deadline: float) -> tuple[int, bytes] | None:
    """Search nonces from ``start`` until a seal meets the faucet difficulty or time runs out."""
    sha256 = hashlib.sha256
    while not _stop.is_set() and time.monotonic() < deadline:
        for nonce in range(start, start + CHECK_EVERY):
            work = _keccak(sha256(nonce.to_bytes(8, "little") + block_and_key).digest())
            if int.from_bytes(work, "little") <= TARGET:
                _stop.set()
                return nonce, work
        start += CHECK_EVERY
    return None


async def _solve(pool, stop, workers: int, block_and_key: bytes, budget: float):
    stop.clear()
    deadline = time.monotonic() + budget
    futures = [
        asyncio.wrap_future(pool.submit(_mine, block_and_key, secrets.randbits(62), deadline))
        for _ in range(workers)
    ]
    solved = None
    for future in asyncio.as_completed(futures):
        solved = await future
        if solved:
            break
    stop.set()
    await asyncio.gather(*futures)
    return solved


async def drip(client, pool, stop, workers: int, wallet, budget: float, max_attempts: int) -> int:
    """Land one faucet call for ``wallet``; return how many attempts it took."""
    pubkey = wallet.coldkey.public_key
    for attempt in range(1, max_attempts + 1):
        head = await client.block_info()
        block_and_key = _keccak(bytes.fromhex(head.hash.removeprefix("0x")) + pubkey)
        solved = await _solve(pool, stop, workers, block_and_key, budget)
        if solved is None:
            continue
        nonce, work = solved
        call = bt.calls.Call(
            "SubtensorModule",
            "faucet",
            {"block_number": head.number, "nonce": nonce, "work": "0x" + work.hex()},
        )
        try:
            result = await client.submit_call(call, wallet, wait_for_finalization=False)
            error = result.error
        except ChainError as raised:
            result, error = None, raised
        if result is not None and result.success:
            return attempt
        if error is None or error.name not in STALE_WORK_ERRORS:
            message = error.message if error else result.message
            raise SystemExit(f"faucet call failed for {wallet.name}: {message}")
    raise SystemExit(f"no faucet call landed for {wallet.name} after {max_attempts} attempts")


def parse_target(spec: str) -> tuple[str, int]:
    name, _, calls = spec.partition(":")
    if not name or (calls and not calls.isdigit()) or calls == "0":
        raise argparse.ArgumentTypeError(f"expected WALLET or WALLET:CALLS, got {spec!r}")
    return name, int(calls or 1)


async def main(args: argparse.Namespace) -> None:
    wallets = []
    for name, calls in args.targets:
        wallet = bt.Wallet(name, path=args.wallet_path)
        try:
            _ = wallet.coldkey  # unlock (password prompt) before any mining starts
        except Exception as error:
            raise SystemExit(
                f"cannot load coldkey for wallet {name!r} under {args.wallet_path}: {error}\n"
                f"create it with: btcli wallet create -w {name} -H default"
            ) from None
        wallets.append((wallet, calls))

    client = bt.Client(args.network, fallback_endpoints=[], archive_endpoints=[])
    await client.connect()
    try:
        probe = {"block_number": 0, "nonce": 0, "work": "0x" + "00" * 32}
        try:
            await client.compose(bt.calls.Call("SubtensorModule", "faucet", probe))
        except Exception as error:
            raise SystemExit(
                f"{args.network} has no usable SubtensorModule.faucet ({error}); the runtime "
                "was built without the pow-faucet feature (only localnet builds have it)"
            ) from None
        block_time = await client.block_time()
        # Work must land within 3 blocks of the one it was mined on; leave room to submit.
        budget = block_time * 1.5
        ctx = mp.get_context("spawn")
        stop = ctx.Event()
        with ProcessPoolExecutor(
            args.workers, mp_context=ctx, initializer=_init_worker, initargs=(stop,)
        ) as pool:
            print(f"{args.network}: {block_time}s blocks, {args.workers} mining workers")
            for wallet, calls in wallets:
                ss58 = wallet.coldkeypub.ss58_address
                for n in range(1, calls + 1):
                    attempts = await drip(
                        client, pool, stop, args.workers, wallet, budget, args.max_attempts
                    )
                    print(f"  {wallet.name}: +{FAUCET_TAO_PER_CALL} TAO ({n}/{calls}, {attempts} attempt(s))")
                print(f"  {wallet.name} {ss58} balance {await client.balances.get(ss58)}")
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--network", default=os.environ.get("BT_NETWORK", "ws://127.0.0.1:9944"))
    parser.add_argument(
        "--wallet-path",
        default=os.path.expanduser(os.environ.get("BT_WALLET_PATH", "~/.bittensor/wallets")),
    )
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--max-attempts", type=int, default=200)
    parser.add_argument("targets", nargs="+", type=parse_target, metavar="WALLET[:CALLS]")
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        sys.exit(130)
