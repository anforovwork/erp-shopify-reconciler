"""Command-line entry point. Commands are added in later tasks."""

import argparse

from reconciler import __version__


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="reconcile",
        description="Read-only reconciliation between Shopify and ERPNext.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args()
    parser.print_help()


if __name__ == "__main__":
    main()
