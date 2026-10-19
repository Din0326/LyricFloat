"""Entry point: python -m lyricfloat [--debug] [--demo]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import qasync
from PyQt6.QtWidgets import QApplication

from lyricfloat import APP_NAME, __version__
from lyricfloat.app import run
from lyricfloat.logging_setup import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lyricfloat", description="Floating desktop lyrics (M1 prototype)")
    parser.add_argument("--debug", action="store_true", help="verbose logging, also to the console")
    parser.add_argument("--demo", action="store_true", help="play placeholder lyrics without a media player")
    args = parser.parse_args(argv)

    logs = setup_logging(args.debug)
    logging.getLogger(__name__).info("%s %s starting (logs: %s)", APP_NAME, __version__, logs)

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # tray app: closing the window is not quitting
    try:
        qasync.run(run(app, demo=args.demo))
    except asyncio.CancelledError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
