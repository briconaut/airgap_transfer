#!/usr/bin/env python3
"""Baut aus einem tar.bz2-Stream (stdin) einen Commit im aktuellen
Git-Repository, ohne Archiv oder Dateien auf die Platte zu schreiben.

Der Baum des Commits entspricht exakt dem Archivinhalt (nach --strip). Ist
der aktuelle Branch noch leer, entsteht ein initialer Commit; hat er schon
Commits, wird der Import als neuer Commit darauf gesetzt (bisherige History
bleibt erhalten). Index und Arbeitsverzeichnis werden nicht angefasst - es
wird ein temporaerer Index benutzt; das Arbeitsverzeichnis laesst sich danach
z.B. per 'git checkout -f HEAD -- .' fuellen.

Exitcodes: 0 = Commit erstellt, 1 = Git-Fehler, 2 = ungueltige Kommandozeile.
"""
import argparse
import os
import subprocess
import sys
import tarfile
import tempfile


class GitError(Exception):
    """Ein git-Aufruf ist fehlgeschlagen (Text = Befehl + stderr von git)."""


def git(*args, input_bytes=None, env=None, check=True) -> bytes:
    proc = subprocess.run(["git", *args], input=input_bytes, capture_output=True, env=env)
    if check and proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip()
        raise GitError(f"'git {' '.join(args)}' fehlgeschlagen (Exitcode {proc.returncode}): {stderr}")
    return proc.stdout


def hash_blob(data: bytes) -> str:
    # Blob-Objekt direkt in .git/objects erzeugen, ohne Datei anzulegen
    return git("hash-object", "-w", "--stdin", input_bytes=data).decode().strip()


def strip_leading_dotslash(path: str) -> str:
    """Entfernt beliebig viele fuehrende './'-Segmente ('././a' -> 'a'), damit
    Archive mit und ohne './'-Praefix gleich behandelt werden."""
    while path.startswith("./"):
        path = path[2:]
    return path


def apply_strip(name: str, strip_dirs: list[str]) -> str | None:
    """Wendet die erste passende --strip-Regel an. Verglichen wird
    komponentenweise ('--strip proj' trifft 'proj/a', aber nicht 'project/a');
    nach dem ersten Treffer werden keine weiteren Regeln angewendet.
    None bedeutet: Eintrag ueberspringen, da der Zielpfad sonst leer wuerde
    (eine Datei, deren Pfad genau dem --strip-Verzeichnis entspricht)."""
    normalized = strip_leading_dotslash(name)
    components = normalized.split("/")

    for strip_dir in strip_dirs:
        prefix = strip_leading_dotslash(strip_dir).rstrip("/").split("/")
        if components[: len(prefix)] == prefix:
            remainder = components[len(prefix):]
            if not remainder:
                print(
                    f"WARNUNG: '{name}' wird durch --strip '{strip_dir}' leer, uebersprungen",
                    file=sys.stderr,
                )
                return None
            return "/".join(remainder)  # nur der erste Treffer zaehlt, kein Kaskadieren

    return normalized


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="tar.bz2-Stream (stdin) als Git-Commit importieren")
    parser.add_argument(
        "--strip",
        action="append",
        default=[],
        metavar="DIR",
        help="Fuehrendes Verzeichnis vom Pfad jedes Eintrags entfernen (mehrfach angebbar)",
    )
    parser.add_argument("-m", "--message", default="Import aus tar.bz2", help="Commit-Message")
    return parser.parse_args()


def import_archive(stream, strip_dirs: list[str], index_env: dict) -> int:
    """Liest das Archiv und traegt alle Eintraege in den (temporaeren) Index
    ein. Rueckgabe: Anzahl importierter Eintraege."""
    seen_paths: set[str] = set()
    # Archivname (normalisiert) -> (mode, blob), damit Hardlinks auf schon
    # gelesene Dateien aufgeloest werden koennen
    entries: dict[str, tuple[str, str]] = {}
    index_info = []

    with tarfile.open(fileobj=stream, mode="r|bz2") as tar:
        for member in tar:
            if member.isreg():
                data = tar.extractfile(member).read()  # nur im Speicher
                mode = "100755" if member.mode & 0o111 else "100644"
                blob = hash_blob(data)
            elif member.issym():
                blob = hash_blob(member.linkname.encode())
                mode = "120000"
            elif member.islnk():
                target = entries.get(strip_leading_dotslash(member.linkname))
                if target is None:
                    print(f"WARNUNG: Hardlink '{member.name}' -> '{member.linkname}': Ziel nicht "
                          f"im Archiv gefunden, uebersprungen", file=sys.stderr)
                    continue
                mode, blob = target
            else:
                continue  # Verzeichnisse, Devices, FIFOs etc.: in Git nicht abbildbar

            entries[strip_leading_dotslash(member.name)] = (mode, blob)
            path = apply_strip(member.name, strip_dirs)
            if path is None:
                continue

            if path in seen_paths:  # spaeterer Eintrag gewinnt (wie beim Entpacken), hier nur Warnung
                print(f"WARNUNG: Pfad '{path}' wird ueberschrieben (Kollision)", file=sys.stderr)
            seen_paths.add(path)
            index_info.append(f"{mode} {blob}\t{path}")

    # Alle Eintraege in einem Aufruf; bei Kollisionen gewinnt der letzte.
    git("update-index", "--add", "-z", "--index-info",
        input_bytes="".join(f"{line}\0" for line in index_info).encode(), env=index_env)
    return len(seen_paths)


def main() -> int:
    args = parse_args()
    try:
        git_dir = git("rev-parse", "--git-dir").decode().strip()
        branch_ref = git("symbolic-ref", "HEAD").decode().strip()
        # Bisheriger Stand des Branches (leer bei einem noch leeren Branch)
        old_commit = git("rev-parse", "--verify", "--quiet", f"{branch_ref}^{{commit}}", check=False)
        old_commit = old_commit.decode().strip()

        # Temporaerer Index: der echte Index (und damit evtl. gestagte
        # Aenderungen) bleibt unberuehrt und landet nicht im Import-Commit.
        with tempfile.TemporaryDirectory(dir=git_dir) as tmp:
            index_env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmp, "index")}
            count = import_archive(sys.stdin.buffer, args.strip, index_env)
            tree = git("write-tree", env=index_env).decode().strip()

        parent_args = ["-p", old_commit] if old_commit else []
        commit = git("commit-tree", tree, *parent_args, "-m", args.message).decode().strip()
        # Mit erwartetem Altwert: schlaegt fehl, falls der Branch zwischendurch
        # anderweitig bewegt wurde (leerer Altwert = Ref darf noch nicht existieren).
        git("update-ref", branch_ref, commit, old_commit)
    except (GitError, tarfile.TarError) as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        return 1

    note = f"auf {old_commit[:12]}" if old_commit else "initialer Commit"
    print(f"Commit: {commit} ({count} Eintraege, {note}, {branch_ref})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
