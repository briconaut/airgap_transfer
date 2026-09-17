#!/usr/bin/env python3
"""Baut aus einem tar.bz2-Stream (stdin) einen initialen Commit,
ohne Archiv oder Dateien auf die Platte zu schreiben."""
import argparse
import subprocess
import sys
import tarfile


def git(*args, input_bytes=None) -> bytes:
    return subprocess.run(
        ["git", *args], input=input_bytes, capture_output=True, check=True
    ).stdout


def hash_blob(data: bytes) -> str:
    # Blob-Objekt direkt im .git/objects erzeugen, ohne Datei anzulegen
    return git("hash-object", "-w", "--stdin", input_bytes=data).decode().strip()


def strip_leading_dotslash(path: str) -> str:
    """Entfernt beliebig viele führende './'-Segmente (Q2)."""
    while path.startswith("./"):
        path = path[2:]
    return path


def apply_strip(name: str, strip_dirs: list[str]) -> str | None:
    """Wendet die erste passende --strip-Regel an, komponentenweise (Q1, Q3).
    None bedeutet: Eintrag überspringen, da Zielpfad sonst leer würde (Q5)."""
    normalized = strip_leading_dotslash(name)
    components = normalized.split("/")

    for strip_dir in strip_dirs:
        prefix = strip_leading_dotslash(strip_dir).rstrip("/").split("/")
        if components[: len(prefix)] == prefix:
            remainder = components[len(prefix):]
            if not remainder:
                print(
                    f"WARNUNG: '{name}' wird durch --strip '{strip_dir}' leer, übersprungen",
                    file=sys.stderr,
                )
                return None
            return "/".join(remainder)  # nur der erste Treffer zählt, kein Kaskadieren

    return normalized


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="tar.bz2-Stream als Git-Commit importieren")
    parser.add_argument(
        "--strip",
        action="append",
        default=[],
        metavar="DIR",
        help="Führendes Verzeichnis vom Pfad jedes Eintrags entfernen (mehrfach angebbar)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    seen_paths: set[str] = set()

    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|bz2") as tar:
        for member in tar:
            if member.isreg():
                data = tar.extractfile(member).read()  # nur im Speicher
                mode = "100755" if member.mode & 0o111 else "100644"
                blob = hash_blob(data)
            elif member.issym():
                blob = hash_blob(member.linkname.encode())
                mode = "120000"
            else:
                continue  # Verzeichnisse, Hardlinks, Devices etc.: übersprungen

            path = apply_strip(member.name, args.strip)
            if path is None:
                continue

            if path in seen_paths:  # Q4: git überschreibt selbst, hier nur Sichtbarkeit
                print(f"WARNUNG: Pfad '{path}' wird überschrieben (Kollision)", file=sys.stderr)
            seen_paths.add(path)

            git("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}")

    tree = git("write-tree").decode().strip()
    commit = git("commit-tree", tree, "-m", "Import aus tar.bz2").decode().strip()

    branch_ref = git("symbolic-ref", "HEAD").decode().strip()
    git("update-ref", branch_ref, commit)
    print(f"Commit: {commit}")


if __name__ == "__main__":
    main()
