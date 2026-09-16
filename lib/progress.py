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

    Aktualisiert nur alle ~1% (mindestens alle 10 Schritte), um den
    flush()-Overhead bei sehr vielen Zeilen gering zu halten.
    """

    WIDTH = 40  # Breite des Balkenbereichs in Zeichen

    def __init__(self, total: int, enabled: bool = True, label: str = ""):
        self.total = max(total, 1)
        self.enabled = enabled
        self.label = label
        # Aktualisierungsintervall: alle 1%, aber mindestens alle 10 Schritte
        self.interval = max(10, self.total // 100)
        self._last_pct = -1
        if enabled:
            self._render(0)

    def update(self, current: int):
        if not self.enabled:
            return
        # Nur aktualisieren, wenn sich der Prozentwert geaendert hat
        # ODER das Intervall erreicht ist (verhindert Ausgabepause am Ende)
        pct = min(100, int(current * 100 / self.total))
        if pct == self._last_pct and current % self.interval != 0:
            return
        self._last_pct = pct
        self._render(current)

    def done(self):
        """Schliesst den Balken ab (100%) und gibt die Zeile frei."""
        if not self.enabled:
            return
        self._render(self.total)
        sys.stderr.write("\n")
        sys.stderr.flush()

    def _render(self, current: int):
        pct = min(100, int(current * 100 / self.total))
        filled = self.WIDTH * pct // 100
        bar = "=" * filled + "-" * (self.WIDTH - filled)
        prefix = f"{self.label} " if self.label else ""
        sys.stderr.write(f"\r{prefix}[{bar}] {pct:3d}%  {current}/{self.total}")
        sys.stderr.flush()
