#!/usr/bin/env python3
"""Pinned C_E refusal probe; invoke only under reviewed P_E and admission.

No pytest, vectors, secret writer/inventory, child, HTTP/SQL or exporter.
The failure report is not process cleanup or conformance success.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from alrust_conformance.refusal_entrypoint import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
