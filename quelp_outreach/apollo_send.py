"""
Deprecated — use list_send.py.

list_send.py accepts Apollo exports AND any other lead CSV, sends the current
pitch from pitch.py, and enforces the daily cap. This shim forwards to it so
old commands keep working:

  python apollo_send.py --in apollo-contacts-export.csv --dry-run
"""

from list_send import main

if __name__ == "__main__":
    print("apollo_send.py → forwarding to list_send.py\n")
    main()
