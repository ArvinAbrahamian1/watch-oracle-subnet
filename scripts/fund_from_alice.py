"""Fund the subnet's coldkeys on a localnet by transferring from the dev account Alice.

Every subtensor localnet pre-funds Alice (``//Alice`` on the public Substrate
dev mnemonic). Her key is derived in memory, so nothing is unlocked: the
destinations only need their public ``coldkeypub.txt`` addresses. Never point
this at a real network; the dev mnemonic is public.

Usage: python scripts/fund_from_alice.py [--network URL] [--wallet-path DIR] [WALLET:TAO ...]
       default targets: owner:3000 validator:1000 miner:1000
"""

from __future__ import annotations

import argparse
import asyncio
import os

import bittensor as bt
from bittensor.sp_core import CRYPTO_SR25519, Keypair

DEV_MNEMONIC = "bottom drive obey lake curtain smoke basket hold race lonely fit walk"
ALICE_SS58 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
DEFAULT_TARGETS = ["owner:3000", "validator:1000", "miner:1000"]


def alice() -> Keypair:
    keypair = Keypair.create_from_uri(f"{DEV_MNEMONIC}//Alice", crypto_type=CRYPTO_SR25519)
    if keypair.ss58_address != ALICE_SS58:
        raise SystemExit(f"derived Alice {keypair.ss58_address}, expected {ALICE_SS58}")
    return keypair


def parse_target(spec: str) -> tuple[str, int]:
    name, _, tao = spec.partition(":")
    if not name or not tao.isdigit() or tao == "0":
        raise argparse.ArgumentTypeError(f"expected WALLET:TAO, got {spec!r}")
    return name, int(tao)


async def main(args: argparse.Namespace) -> None:
    sender = alice()
    destinations = []
    for name, tao in args.targets:
        try:
            ss58 = bt.Wallet(name, path=args.wallet_path).coldkeypub.ss58_address
        except Exception as error:
            raise SystemExit(
                f"cannot read coldkeypub for wallet {name!r} under {args.wallet_path}: {error}\n"
                f"create it with: btcli wallet create -w {name} -H default"
            ) from None
        destinations.append((name, ss58, tao))

    client = bt.Client(args.network, fallback_endpoints=[], archive_endpoints=[])
    await client.connect()
    try:
        available = await client.balances.get(ALICE_SS58)
        needed = sum(tao for _, _, tao in destinations)
        print(f"{args.network}: Alice {ALICE_SS58} has {available}")
        if available.tao < needed:
            raise SystemExit(f"Alice holds less than the {needed} TAO requested; is this a localnet?")

        for name, ss58, tao in destinations:
            result = await client.execute(
                bt.Transfer(dest_ss58=ss58, amount_tao=tao), sender, wait_for_finalization=False
            )
            if not result.success:
                raise SystemExit(f"transfer to {name} ({ss58}) failed: {result.message}")
            print(f"  sent {tao} TAO to {name} ({ss58})")

        print("balances:")
        for name, ss58, _ in destinations:
            print(f"  {name:<10} {ss58} {await client.balances.get(ss58)}")
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
    parser.add_argument(
        "targets", nargs="*", type=parse_target, default=None, metavar="WALLET:TAO"
    )
    args = parser.parse_args()
    args.targets = args.targets or [parse_target(t) for t in DEFAULT_TARGETS]
    asyncio.run(main(args))
