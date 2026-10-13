"""
Einfacher ASCII-Fortschrittsbalken fuer stderr.

Nutzung:
    bar = ProgressBar(total=total_lines, enabled=args.progress)
    for i, ...:
        bar.update(i + 1)
    bar.done()
"""
import sys


class ProgressBar:
    """Schreibt einen ASCII-Fortschrittsbalken auf stderr.

    Zeichnet nur neu, wenn sich der ganzzahlige Prozentwert aendert
    (hoechstens 101 Ausgaben, unabhaengig von `total`), um den
    flush()-Overhead bei sehr vielen Zeilen gering zu halten.
    """

    WIDTH = 40  # Breite des Balkenbereichs in Zeichen

    def __init__(self, total: int, enabled: bool = True, label: str = ""):
        self.total = max(total, 1)
        self.enabled = enabled
        self.label = label
        self._last_pct = -1
        if enabled:
            self._render(0)

    def update(self, current: int):
        if not self.enabled:
            return
        pct = self._percent(current)
        if pct == self._last_pct:
            return
        self._render(current)

    def done(self):
        """Schliesst den Balken ab (100%) und gibt die Zeile frei."""
        if not self.enabled:
            return
        self._render(self.total)
        sys.stderr.write("\n")
        sys.stderr.flush()

    def _percent(self, current: int) -> int:
        return min(100, current * 100 // self.total)

    def _render(self, current: int):
        pct = self._last_pct = self._percent(current)
        filled = self.WIDTH * pct // 100
        bar = "=" * filled + "-" * (self.WIDTH - filled)
        prefix = f"{self.label} " if self.label else ""
        sys.stderr.write(f"\r{prefix}[{bar}] {pct:3d}%  {current}/{self.total}")
        sys.stderr.flush()
