"""Entry point for the packaged build (PyInstaller runs this file as the program)."""

import sys

from lyricfloat.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
