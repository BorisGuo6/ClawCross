"""Curses-based interactive picker for ClawCross.

Adapted from Hermes' hermes_cli/curses_ui.py — provides a single-select
radio list with a numbered text fallback for non-curses terminals.
Color dependencies are stripped; plain text only.
"""

from __future__ import annotations

import sys
from typing import List


def flush_stdin() -> None:
    """Flush any stray bytes from the stdin input buffer.

    Must be called after ``curses.wrapper()`` returns, **before** the next
    ``input()`` / ``getpass.getpass()`` call.  ``curses.endwin()`` restores the
    terminal but does NOT drain the OS input buffer — leftover escape-sequence
    bytes (from arrow keys, terminal mode-switch responses, or rapid keypresses)
    remain buffered and silently get consumed by the next ``input()`` call,
    corrupting user data (e.g. writing ``^[^[`` into .env files).

    On non-TTY stdin (piped, redirected) or Windows, this is a no-op.
    """
    try:
        if not sys.stdin.isatty():
            return
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass


def curses_radiolist(
    title: str,
    items: List[str],
    selected: int = 0,
    *,
    cancel_returns: int | None = None,
    description: str | None = None,
) -> int:
    """Curses single-select radio list. Returns the selected index.

    Args:
        title: Header line displayed above the list.
        items: Display labels for each row.
        selected: Index that starts selected (pre-selected).
        cancel_returns: Returned on ESC/q. Defaults to the original *selected*.
        description: Optional multi-line text shown between the title and
            the item list.
    """
    if cancel_returns is None:
        cancel_returns = selected

    if not items:
        return cancel_returns

    if not sys.stdin.isatty():
        return cancel_returns

    desc_lines: list[str] = []
    if description:
        desc_lines = description.splitlines()

    try:
        import curses
        result_holder: list = [None]

        def _draw(stdscr):
            curses.curs_set(0)
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_GREEN, -1)
                curses.init_pair(2, curses.COLOR_YELLOW, -1)
            cursor = selected
            scroll_offset = 0

            while True:
                stdscr.clear()
                max_y, max_x = stdscr.getmaxyx()

                row = 0

                # Header
                try:
                    hattr = curses.A_BOLD
                    if curses.has_colors():
                        hattr |= curses.color_pair(2)
                    stdscr.addnstr(row, 0, title, max_x - 1, hattr)
                    row += 1

                    # Description lines
                    for dline in desc_lines:
                        if row >= max_y - 1:
                            break
                        stdscr.addnstr(row, 0, dline, max_x - 1, curses.A_NORMAL)
                        row += 1

                    stdscr.addnstr(
                        row, 0,
                        "  ↑↓ navigate  ENTER/SPACE select  ESC cancel",
                        max_x - 1, curses.A_DIM,
                    )
                    row += 1
                except curses.error:
                    pass

                # Scrollable item list
                items_start = row + 1
                visible_rows = max_y - items_start - 1
                if cursor < scroll_offset:
                    scroll_offset = cursor
                elif cursor >= scroll_offset + visible_rows:
                    scroll_offset = cursor - visible_rows + 1

                for draw_i, i in enumerate(
                    range(scroll_offset, min(len(items), scroll_offset + visible_rows))
                ):
                    y = draw_i + items_start
                    if y >= max_y - 1:
                        break
                    radio = "●" if i == selected else "○"
                    arrow = "→" if i == cursor else " "
                    line = f" {arrow} ({radio}) {items[i]}"
                    attr = curses.A_NORMAL
                    if i == cursor:
                        attr = curses.A_BOLD
                        if curses.has_colors():
                            attr |= curses.color_pair(1)
                    try:
                        stdscr.addnstr(y, 0, line, max_x - 1, attr)
                    except curses.error:
                        pass

                stdscr.refresh()
                key = stdscr.getch()

                if key in (curses.KEY_UP, ord("k")):
                    cursor = (cursor - 1) % len(items)
                elif key in (curses.KEY_DOWN, ord("j")):
                    cursor = (cursor + 1) % len(items)
                elif key in (ord(" "), curses.KEY_ENTER, 10, 13):
                    result_holder[0] = cursor
                    return
                elif key in (27, ord("q")):
                    result_holder[0] = cancel_returns
                    return

        curses.wrapper(_draw)
        flush_stdin()
        return result_holder[0] if result_holder[0] is not None else cancel_returns

    except KeyboardInterrupt:
        return cancel_returns
    except Exception:
        return _radio_numbered_fallback(title, items, selected, cancel_returns)


def _radio_numbered_fallback(
    title: str,
    items: List[str],
    selected: int,
    cancel_returns: int,
) -> int:
    """Text-based numbered fallback for radio selection."""
    print(f"\n  {title}")
    print("  Select by number, Enter to confirm.\n")

    for i, label in enumerate(items):
        marker = "(●)" if i == selected else "(○)"
        print(f"  {marker} {i + 1:>2}. {label}")
    print()
    try:
        val = input(f"  Choice [default {selected + 1}]: ").strip()
        if not val:
            return selected
        idx = int(val) - 1
        if 0 <= idx < len(items):
            return idx
        return selected
    except (ValueError, KeyboardInterrupt, EOFError):
        return cancel_returns


def prompt_text(label: str, *, default: str = "") -> str:
    """Print *label* and return the user's input.

    On non-tty stdin, returns *default* immediately so non-interactive
    invocations don't hang.
    """
    if not sys.stdin.isatty():
        return default
    try:
        val = input(label).strip()
    except (KeyboardInterrupt, EOFError):
        return default
    return val or default
