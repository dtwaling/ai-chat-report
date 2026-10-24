"""Minimal PEP 517 build backend using only the Python 3.10+ standard library."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
from pathlib import Path
import tarfile
import zipfile

from chat_report import __version__


_NAME = "chat-report"
_DIST_NAME = "chat_report"
_SUMMARY = "Deterministic AI chat session extraction, heuristic diagnostics, and v2.0 reporting."
_REQUIRES_PYTHON = ">=3.10"
_ENTRYPOINT = "chat-report = chat_report.cli:main"


def _wheel_filename(version: str) -> str:
    return f"{_DIST_NAME}-{version}-py3-none-any.whl"


def _sdist_filename(version: str) -> str:
    return f"{_DIST_NAME}-{version}.tar.gz"


def _dist_info_dir(version: str) -> str:
    return f"{_DIST_NAME}-{version}.dist-info"


def _metadata_text(version: str) -> str:
    readme_path = Path(__file__).resolve().parent / "README.md"
    readme = readme_path.read_text(encoding="utf-8") if readme_path.is_file() else ""
    return (
        "Metadata-Version: 2.1\n"
        f"Name: {_NAME}\n"
        f"Version: {version}\n"
        f"Summary: {_SUMMARY}\n"
        f"Requires-Python: {_REQUIRES_PYTHON}\n"
        "Description-Content-Type: text/markdown\n\n"
        f"{readme}"
    )


def _wheel_text() -> str:
    return (
        "Wheel-Version: 1.0\n"
        "Generator: _build_backend\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )


def _entry_points_text() -> str:
    return f"[console_scripts]\n{_ENTRYPOINT}\n"


def _hash_bytes(data: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(data).digest()
    b64 = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"sha256={b64}", str(len(data))


def get_requires_for_build_wheel(config_settings: object = None) -> list[str]:
    return []


def get_requires_for_build_sdist(config_settings: object = None) -> list[str]:
    return []


def get_requires_for_build_editable(config_settings: object = None) -> list[str]:
    return []


def prepare_metadata_for_build_wheel(
    metadata_directory: str,
    config_settings: object = None,
) -> str:
    dist_info = _dist_info_dir(__version__)
    target = Path(metadata_directory) / dist_info
    target.mkdir(parents=True, exist_ok=True)
    (target / "METADATA").write_text(_metadata_text(__version__), encoding="utf-8")
    (target / "WHEEL").write_text(_wheel_text(), encoding="utf-8")
    (target / "entry_points.txt").write_text(_entry_points_text(), encoding="utf-8")
    return dist_info


prepare_metadata_for_build_editable = prepare_metadata_for_build_wheel


def build_wheel(
    wheel_directory: str,
    config_settings: object = None,
    metadata_directory: str | None = None,
) -> str:
    version = __version__
    wheel_name = _wheel_filename(version)
    wheel_path = Path(wheel_directory) / wheel_name
    root = Path(__file__).resolve().parent
    package_root = root / "chat_report"
    dist_info = _dist_info_dir(version)
    records: list[tuple[str, str, str]] = []

    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(package_root.rglob("*")):
            if not file_path.is_file():
                continue
            if "__pycache__" in file_path.parts or file_path.suffix == ".pyc":
                continue
            arcname = file_path.relative_to(root).as_posix()
            data = file_path.read_bytes()
            archive.writestr(arcname, data)
            digest, size = _hash_bytes(data)
            records.append((arcname, digest, size))

        for name, text in (
            (f"{dist_info}/METADATA", _metadata_text(version)),
            (f"{dist_info}/WHEEL", _wheel_text()),
            (f"{dist_info}/entry_points.txt", _entry_points_text()),
        ):
            data = text.encode("utf-8")
            archive.writestr(name, data)
            digest, size = _hash_bytes(data)
            records.append((name, digest, size))

        record_arcname = f"{dist_info}/RECORD"
        records.append((record_arcname, "", ""))
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        for row in records:
            writer.writerow(row)
        archive.writestr(record_arcname, buf.getvalue().encode("utf-8"))

    return wheel_name


def build_sdist(
    sdist_directory: str,
    config_settings: object = None,
) -> str:
    version = __version__
    sdist_name = _sdist_filename(version)
    sdist_path = Path(sdist_directory) / sdist_name
    root = Path(__file__).resolve().parent
    prefix = f"{_DIST_NAME}-{version}"

    with tarfile.open(sdist_path, "w:gz") as archive:
        for rel in ("pyproject.toml", "_build_backend.py", "README.md"):
            src = root / rel
            if src.is_file():
                archive.add(src, arcname=f"{prefix}/{rel}")
        for folder in ("chat_report", "skills"):
            folder_path = root / folder
            if not folder_path.is_dir():
                continue
            for file_path in sorted(folder_path.rglob("*")):
                if not file_path.is_file():
                    continue
                if "__pycache__" in file_path.parts or file_path.suffix == ".pyc":
                    continue
                rel_path = file_path.relative_to(root).as_posix()
                archive.add(file_path, arcname=f"{prefix}/{rel_path}")
        pkg_info = _metadata_text(version).encode("utf-8")
        info = tarfile.TarInfo(name=f"{prefix}/PKG-INFO")
        info.size = len(pkg_info)
        archive.addfile(info, io.BytesIO(pkg_info))

    return sdist_name


def build_editable(
    wheel_directory: str,
    config_settings: object = None,
    metadata_directory: str | None = None,
) -> str:
    version = __version__
    wheel_name = _wheel_filename(version)
    wheel_path = Path(wheel_directory) / wheel_name
    root = Path(__file__).resolve().parent
    dist_info = _dist_info_dir(version)
    records: list[tuple[str, str, str]] = []

    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        pth_name = f"{_DIST_NAME}.pth"
        pth_data = f"{root}\n".encode("utf-8")
        archive.writestr(pth_name, pth_data)
        digest, size = _hash_bytes(pth_data)
        records.append((pth_name, digest, size))

        for name, text in (
            (f"{dist_info}/METADATA", _metadata_text(version)),
            (f"{dist_info}/WHEEL", _wheel_text()),
            (f"{dist_info}/entry_points.txt", _entry_points_text()),
        ):
            data = text.encode("utf-8")
            archive.writestr(name, data)
            digest, size = _hash_bytes(data)
            records.append((name, digest, size))

        record_arcname = f"{dist_info}/RECORD"
        records.append((record_arcname, "", ""))
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        for row in records:
            writer.writerow(row)
        archive.writestr(record_arcname, buf.getvalue().encode("utf-8"))

    return wheel_name
