#!/usr/bin/env python3
"""Startskript fuer airgap.encode (Doku und Optionen siehe dort bzw. --help).
Gleichwertig: 'python3 -m airgap.encode' aus dem Projektverzeichnis."""
import os
import sys

# Projektverzeichnis (enthaelt das Paket 'airgap') importierbar machen,
# unabhaengig vom Aufrufverzeichnis
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from airgap.encode import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
