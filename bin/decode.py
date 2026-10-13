#!/usr/bin/env python3
"""Startskript fuer airgap.decode (Doku und Optionen siehe dort bzw. --help).
Gleichwertig: 'python3 -m airgap.decode' aus dem Projektverzeichnis."""
import os
import sys

# Projektverzeichnis (enthaelt das Paket 'airgap') importierbar machen,
# unabhaengig vom Aufrufverzeichnis
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from airgap.decode import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
