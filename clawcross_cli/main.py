#!/usr/bin/env python3
"""
ClawCross CLI entrypoint — model and provider management.

Invoked by ``scripts/clawcross`` bash wrapper (or directly via
``python3 -m clawcross_cli.main``).

Subcommands:
  model [list|show|use|add|remove|migrate|<name>]
  provider [<slug> [<base_url>]]
"""

from __future__ import annotations

import sys

from clawcross_cli.model_cmd import handle_model_command, handle_provider_command


def usage() -> None:
    print("Usage: clawcross <model|provider> [...]")
    print()
    print("  model                       interactive picker / list")
    print("  model list                  list configured profiles")
    print("  model show                  show active profile")
    print("  model use <name>            switch active profile")
    print("  model add [<name>]          add a profile (interactive)")
    print("  model remove <name>         delete a profile")
    print("  model migrate               import current .env into a profile")
    print("  provider                    show active provider")
    print("  provider <slug> [<url>]     set provider on active profile (or .env)")
    sys.exit(2)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        usage()

    cmd = args[0].lower().strip()
    rest = args[1:]

    if cmd == "model":
        out = handle_model_command(rest, interactive=True)
        if out:
            print(out)
    elif cmd == "provider":
        out = handle_provider_command(rest, interactive=True)
        if out:
            print(out)
    else:
        print(f"Unknown command: {cmd}")
        usage()


if __name__ == "__main__":
    main()
