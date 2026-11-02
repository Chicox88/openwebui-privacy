"""Entry point used by PyInstaller to build EncompassFieldFinder.exe."""

import sys

from encompass_field_finder.app import main

if __name__ == "__main__":
    sys.exit(main())
