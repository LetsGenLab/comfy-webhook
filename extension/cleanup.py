"""Conservative, task-scoped output cleanup after webhook acknowledgement."""

import os
import stat
from contextlib import contextmanager

_HAS_DIR_FD = hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW") and os.stat in os.supports_dir_fd and os.unlink in os.supports_dir_fd

@contextmanager
def _open_folder(root, subfolder):
    """Walk only below root, refusing symlinks and unsafe components."""
    parts = subfolder.split("/") if subfolder else []
    if any(not part or part in (".", "..") or "\\" in part or ":" in part for part in parts):
        raise ValueError("Invalid output subfolder")
    if _HAS_DIR_FD:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, None
        finally:
            os.close(fd)
    else:
        # Windows does not expose POSIX directory descriptors. Reject every
        # symlink and reparse point while walking the configured output root.
        path = root
        for part in parts:
            path = os.path.join(path, part)
            info = os.lstat(path)
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                raise ValueError("Unsafe output subfolder")
        if os.path.commonpath([root, os.path.realpath(path)]) != root:
            raise ValueError("Output outside configured root")
        yield None, path


def _stat_file(fd, directory, name):
    return os.stat(name, dir_fd=fd, follow_symlinks=False) if fd is not None else os.stat(os.path.join(directory, name), follow_symlinks=False)


def _remove_file(fd, directory, name):
    if fd is not None:
        os.unlink(name, dir_fd=fd)
    else:
        os.unlink(os.path.join(directory, name))


def capture(output_root, outputs, cached_nodes):
    if not output_root or not os.path.isabs(output_root):
        raise ValueError("A configured output directory is required for auto clean")
    root = os.path.realpath(output_root)
    files, seen = [], set()
    for item in outputs:
        if not isinstance(item, dict) or item.get("type") != "output" or str(item.get("nodeId")) in cached_nodes:
            continue
        name, subfolder = item.get("filename"), item.get("subfolder", "")
        if (not isinstance(name, str) or not name or name in (".", "..") or os.path.basename(name) != name
                or "\\" in name or ":" in name or not isinstance(subfolder, str) or subfolder.startswith("/")):
            continue
        key = (subfolder, name)
        if key in seen:
            continue
        seen.add(key)
        try:
            with _open_folder(root, subfolder) as (fd, directory):
                info = _stat_file(fd, directory, name)
        except (OSError, ValueError):
            continue
        if stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            files.append({"name": name, "subfolder": subfolder, "dev": info.st_dev,
                          "ino": info.st_ino, "size": info.st_size, "mtime_ns": info.st_mtime_ns})
    return {"root": root, "files": files}


def remove_unchanged(output_root, recorded):
    if not output_root or os.path.realpath(output_root) != recorded.get("root"):
        raise ValueError("Output directory changed")
    for entry in recorded.get("files", []):
        with _open_folder(recorded["root"], entry["subfolder"]) as (fd, directory):
            try:
                info = _stat_file(fd, directory, entry["name"])
            except FileNotFoundError:
                continue
            if (stat.S_ISREG(info.st_mode) and info.st_dev == entry["dev"] and info.st_ino == entry["ino"]
                    and not getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                    and info.st_size == entry["size"] and info.st_mtime_ns == entry["mtime_ns"]):
                _remove_file(fd, directory, entry["name"])
            else:
                # A replacement is not ours to delete. Never remove it on retry.
                continue
