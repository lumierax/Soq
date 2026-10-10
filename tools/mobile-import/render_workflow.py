"""Render the standalone workflow; only the external release copy gets a ZIP hash."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


DIRECTORY = Path(__file__).resolve().parent


def render() -> str:
    template = (DIRECTORY / "workflow.template.yml").read_text()
    importer = (DIRECTORY / "import_archive.py").read_text().rstrip()
    placeholder = "          # __EMBED_IMPORTER__"
    if template.count(placeholder) != 1:
        raise ValueError("Workflow importer placeholder must occur exactly once")
    return template.replace(placeholder, "\n".join("          " + line for line in importer.splitlines()))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, help="Final release ZIP: replace its single hash placeholder")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    content = render()
    if content.count("__ARCHIVE_SHA256__") != 1:
        raise ValueError("Archive hash placeholder must occur exactly once")
    if arguments.archive:
        digest = hashlib.sha256(arguments.archive.read_bytes()).hexdigest()
        content = content.replace("__ARCHIVE_SHA256__", digest)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(content)
    print(f"Standalone workflow written: {arguments.output}")


if __name__ == "__main__":
    main()
