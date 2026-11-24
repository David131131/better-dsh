"""Fetch pinned upstream source fixtures without installing packages."""

from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
import json
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parent / "upstream-sources"
PYTHON = "https://raw.githubusercontent.com/python/cpython"
PACKAGING = "https://codeload.github.com/pypa/packaging/tar.gz/refs/tags"
FILES = {
    "3.12.0": ("Lib/difflib.py", "Lib/asyncio/queues.py", "LICENSE"),
    "3.13.0": ("Lib/graphlib.py", "Lib/configparser.py", "LICENSE"),
    "3.14.0": ("Lib/difflib.py", "Lib/asyncio/queues.py", "Lib/graphlib.py", "Lib/configparser.py", "LICENSE"),
}


def download(url):
    return subprocess.check_output(
        ["curl", "--fail", "--silent", "--show-error", "--location",
         "--connect-timeout", "10", "--max-time", "30", url]
    )


def main():
    hashes = {}
    for version, paths in FILES.items():
        for file in paths:
            url = f"{PYTHON}/v{version}/{file}"
            target = ROOT / "cpython" / version / file
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(download(url))
            hashes[str(target.relative_to(ROOT))] = {"url": url, "sha256": sha256(target.read_bytes()).hexdigest()}
    for version in ("25.0", "26.0", "26.2", "26.3"):
        url = f"{PACKAGING}/{version}"
        destination = ROOT / "packaging" / version
        if not (destination / "LICENSE").is_file() or not (destination / "packaging/__init__.py").is_file():
            with tarfile.open(fileobj=BytesIO(download(url)), mode="r:gz") as archive:
                for item in archive:
                    if not item.isfile():
                        continue
                    _, *parts = PurePosixPath(item.name).parts
                    if any(part in (".", "..") for part in parts):
                        raise ValueError(item.name)
                    if parts[:2] == ["src", "packaging"]:
                        file = destination.joinpath(*parts[1:])
                    elif len(parts) == 1 and parts[0].startswith("LICENSE"):
                        file = destination / parts[0]
                    else:
                        continue
                    file.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(item) as content:
                        file.write_bytes(content.read())
        for file in sorted(destination.rglob("*")):
            if file.is_file():
                hashes[str(file.relative_to(ROOT))] = {"url": url, "sha256": sha256(file.read_bytes()).hexdigest()}
    (ROOT / "manifest.json").write_text(json.dumps(hashes, indent=2) + "\n")
    print(f"已准备 {len(hashes)} 个上游源码和许可文件。")


if __name__ == "__main__":
    main()
