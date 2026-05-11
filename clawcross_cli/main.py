#!/usr/bin/env python3
"""
ClawCross CLI entrypoint — model, provider, team, workflow, skill, cron.

Invoked by ``scripts/clawcross`` bash wrapper (or directly via
``python3 -m clawcross_cli.main``).

Subcommands:
  model [list|show|use|add|remove|migrate|<name>]
  provider [<slug> [<base_url>]]
  team [<name>]
  workflow [show <name> | run <name> team <T> question <Q>]
  skill [<agent>]
  cron [<team>]
"""

from __future__ import annotations

import sys

from clawcross_cli.model_cmd import handle_model_command, handle_provider_command


def usage() -> None:
    print("Usage: clawcross <model|provider|team|workflow|skill|cron> [...]")
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
    print("  team [<name>]               list teams or show one team's members + alarms")
    print("  workflow                    list workflows")
    print("  workflow show <name>        show workflow YAML/py content")
    print("  workflow run <name> team <T> question <Q>")
    print("                              launch a YAML workflow")
    print("  skill [<agent>]             list skills (optionally filtered by agent)")
    print("  cron [<team>]               list cron alarms (optionally for one team)")
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
    elif cmd == "team":
        from clawcross_cli.display_cmd import handle_team_command
        out = handle_team_command(rest, interactive=True)
        if out:
            print(out)
    elif cmd == "workflow":
        from clawcross_cli.display_cmd import handle_workflow_command
        out = handle_workflow_command(rest, interactive=True)
        if out:
            print(out)
    elif cmd == "skill":
        from clawcross_cli.display_cmd import handle_skill_command
        out = handle_skill_command(rest, interactive=True)
        if out:
            print(out)
    elif cmd == "cron":
        from clawcross_cli.display_cmd import handle_cron_command
        out = handle_cron_command(rest, interactive=True)
        if out:
            print(out)
    else:
        print(f"Unknown command: {cmd}")
        usage()


if __name__ == "__main__":
    main()
