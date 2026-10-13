#!/usr/bin/env python3
"""Build a source-only release archive from an explicit file allowlist."""

from __future__ import annotations

import argparse
import hashlib
import re
import zipfile
from pathlib import Path


ROOT_FILES = (
    "README.md", "THIRD_PARTY_NOTICES.md", ".gitignore", ".dockerignore",
    ".env.example", "requirements.txt", "requirements-validated.txt",
    "run_kimodo_sonic_mcp.py", "call_kimodo_sonic_mcp.py",
    "publish_g1_smpl_data.py", "publish_g1_joint_data.py",
)
DIRECTORY_PATTERNS = {
    "kimodo_sonic_service": "*.py",
    "tests": "*.py",
    "docs": "*.md",
    "scripts": "*.py",
}
EXTRA_FILES = (
    "docker/Dockerfile.mcp", "docker/compose.yaml", "runtime/README.md",
    "licenses/README.md", "licenses/Kimodo-LICENSE.txt",
    "licenses/SONIC-LICENSE.txt", "licenses/IOENV-LICENSE.txt",
)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
)


def release_files(root: Path) -> list[Path]:
    files = [root / name for name in ROOT_FILES + EXTRA_FILES]
    for folder, pattern in DIRECTORY_PATTERNS.items():
        files.extend(sorted((root / folder).glob(pattern)))
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing file or symlink in release allowlist: {path}")
        content = path.read_text(encoding="utf-8")
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            raise ValueError(f"Possible credential in release file: {path.name}")
    return sorted(set(files), key=lambda p: p.relative_to(root).as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/sonic-mcp.zip"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    files = release_files(root)
    if output in files or output.suffix != ".zip":
        raise ValueError("Output must be a .zip path outside the source allowlist")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            entry = zipfile.ZipInfo("sonic-mcp/" + path.relative_to(root).as_posix())
            entry.date_time = (1980, 1, 1, 0, 0, 0)
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, path.read_bytes())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_suffix(output.suffix + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(f"Archive: {output}")
    print(f"Files: {len(files)}; bytes: {output.stat().st_size}")
    print(f"SHA-256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
