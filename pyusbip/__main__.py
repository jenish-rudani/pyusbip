"""`python -m pyusbip` entry point.

Mirrors the console_scripts entry (`pyusbip`) so both forms work
identically. Useful when pyusbip is imported as a library but the
operator also wants to run it without a venv activation.
"""

from . import main

if __name__ == "__main__":
    main()
