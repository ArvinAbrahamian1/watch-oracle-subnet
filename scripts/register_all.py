"""Register the validator and ten miner hotkeys on a localnet subnet, starting it first if needed.

Why not ``btcli tx burned-register``: in bittensor 11.3.0 the ``BurnedRegister``
intent sets ``mev_shield_required`` (to stop sandwiching of the collateral AMM
buy), so btcli and ``client.execute`` always wrap it in
``MevShield.submit_encrypted``. That path fails on the fast-blocks localnet
with BadProof. The runtime itself does not require shielding for
``burned_register``, so this submits the raw call in the clear with
``client.submit_call``. Front-running is not a concern on a private localnet.

Each coldkey is unlocked at most once (and only if it has work to do); every
extrinsic is signed with that already-unlocked keypair. Missing miner hotkeys
are created unencrypted, like ``btcli wallet new-hotkey`` would. Already
registered hotkeys are skipped.

Usage: python scripts/register_all.py [--network URL] [--netuid 2] [--wallet-path DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import os

import bittensor as bt
from bittensor.result import ChainError

ST = bt.storage.SubtensorModule
OWNER = "owner"
TARGETS = [("validator", "default")] + [("miner", h) for h in ["default", *(f"m{i}" for i in range(1, 10))]]
ALREADY_REGISTERED = "HotKeyAlreadyRegisteredInSubNet"


class Coldkeys:
    """Unlocks each wallet's coldkey on first use and reuses that keypair."""

    def __init__(self, wallet_path: str):
        self.wallet_path = wallet_path
        self._unlocked: dict[str, object] = {}

    def __call__(self, name: str):
        if name not in self._unlocked:
            print(f"unlocking coldkey {name!r}")
            self._unlocked[name] = bt.Wallet(name, path=self.wallet_path).coldkey
        return self._unlocked[name]


def hotkey_ss58(wallet_path: str, name: str, hotkey: str) -> str:
    wallet = bt.Wallet(name, hotkey, path=wallet_path)
    if not os.path.exists(os.path.join(os.path.expanduser(wallet_path), name, "hotkeys", hotkey)):
        if name != "miner":
            raise SystemExit(
                f"hotkey {name}/{hotkey} is missing; create it with: "
                f"btcli wallet new-hotkey -w {name} -H {hotkey}"
            )
        wallet.create_new_hotkey(suppress=True)
        print(f"created hotkey {name}/{hotkey}")
    try:
        return wallet.hotkeypub.ss58_address
    except FileNotFoundError:  # legacy wallets without hotkeypub.txt; hotkeys are unencrypted
        return wallet.hotkey.ss58_address


def failure(result, raised: ChainError | None) -> tuple[str | None, str]:
    error = raised or result.error
    return (error.name if error else None), (error.message if error else result.message)


async def ensure_started(client, coldkeys: Coldkeys, netuid: int) -> None:
    if await client.query(ST.FirstEmissionBlockNumber, [netuid]) is not None:
        print(f"netuid {netuid} already started")
        return
    owner_ss58 = bt.Wallet(OWNER, path=coldkeys.wallet_path).coldkeypub.ss58_address
    subnet_owner = await client.query(ST.SubnetOwner, [netuid])
    if subnet_owner != owner_ss58:
        raise SystemExit(f"netuid {netuid} is owned by {subnet_owner}, not wallet {OWNER!r} ({owner_ss58})")

    ready_at = int(await client.query(ST.NetworkRegisteredAt, [netuid]) or 0) + int(
        await client.query(ST.StartCallDelay) or 0
    )
    block_time = await client.block_time()
    while (now := await client.block()) < ready_at:
        print(f"start-call opens at block {ready_at} (now {now}), waiting")
        await asyncio.sleep(min(30.0, (ready_at - now) * block_time))

    result = await client.execute(bt.StartCall(netuid=netuid), coldkeys(OWNER), wait_for_finalization=False)
    if not result.success:
        raise SystemExit(f"start-call on netuid {netuid} failed: {result.message}")
    print(f"started netuid {netuid}")


async def register(client, coldkeys: Coldkeys, netuid: int, name: str, hotkey: str, ss58: str) -> None:
    label = f"{name}/{hotkey}"
    uid = await client.query(ST.Uids, [netuid, ss58])
    if uid is not None:
        print(f"  {label:<16} {ss58} already uid {uid}, skipped")
        return
    cost = bt.Balance.from_rao(int(await client.query(ST.Burn, [netuid]) or 0))
    call = bt.calls.SubtensorModule.burned_register(netuid=netuid, hotkey=ss58)
    result, raised = None, None
    try:
        result = await client.submit_call(call, coldkeys(name), wait_for_finalization=False)
    except ChainError as error:
        raised = error
    if result is not None and result.success:
        uid = await client.query(ST.Uids, [netuid, ss58])
        print(f"  {label:<16} {ss58} registered as uid {uid} (cost {cost})")
        return
    error_name, message = failure(result, raised)
    if error_name == ALREADY_REGISTERED:
        print(f"  {label:<16} {ss58} already registered, skipped")
        return
    raise SystemExit(f"burned_register {label} on netuid {netuid} failed: {message}")


async def main(args: argparse.Namespace) -> None:
    # Resolve every hotkey address first so a missing wallet fails before anything is signed.
    targets = [(name, hotkey, hotkey_ss58(args.wallet_path, name, hotkey)) for name, hotkey in TARGETS]
    coldkeys = Coldkeys(args.wallet_path)

    client = bt.Client(args.network, fallback_endpoints=[], archive_endpoints=[])
    await client.connect()
    try:
        if not await client.query(ST.NetworksAdded, [args.netuid]):
            raise SystemExit(f"netuid {args.netuid} does not exist on {args.network}")
        await ensure_started(client, coldkeys, args.netuid)
        print(f"registering on netuid {args.netuid}:")
        for name, hotkey, ss58 in targets:
            await register(client, coldkeys, args.netuid, name, hotkey, ss58)
        n = await client.query(ST.SubnetworkN, [args.netuid])
        print(f"netuid {args.netuid} now has {n} neurons")
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--network", default=os.environ.get("BT_NETWORK", "ws://127.0.0.1:9944"))
    parser.add_argument("--netuid", type=int, default=2)
    parser.add_argument(
        "--wallet-path",
        default=os.path.expanduser(os.environ.get("BT_WALLET_PATH", "~/.bittensor/wallets")),
    )
    asyncio.run(main(parser.parse_args()))
