#!/usr/bin/env python3
"""Entry point: python run.py [--config config.yaml]"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="Discord options-alert copy-trader")
    ap.add_argument("--config", default="config.yaml", help="path to config file")
    args = ap.parse_args()

    if not Path(args.config).exists():
        sys.exit(f"{args.config} not found — copy config.example.yaml to config.yaml "
                 "and .env.example to .env, then edit them.")

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # fine if env vars are set another way

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("bot.log")],
    )
    logging.getLogger("discord").setLevel(logging.WARNING)

    from bot.main import run_bot
    try:
        asyncio.run(run_bot(args.config))
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
