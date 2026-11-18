"""File operations within the VPS directory mounted at /workspace."""

import hashlib
import os
import re
import stat
import uuid
from contextlib import contextmanager, suppress
from itertools import islice
from pathlib import PurePosixPath

from .config import MAX_FILE
from .files import TEXT_TYPES, read_text

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
REGULAR = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
PAGE = 10_000


class WorkspaceConflict(ValueError):
    pass


def valid_part(part):
    return (
        bool(part) and not part.startswith(".")
        and not any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF or char == "\\" for char in part)
        and len(part.encode()) <= 240
    )


def parts_for(path, root_allowed=False):
    if root_allowed and path in {"", "."}:
        return []
    parts = path.split("/")
    if len(path) > 800 or len(parts) > 12 or not all(map(valid_part, parts)):
        raise ValueError("Укажите относительный путь в рабочей папке. Скрытые файлы недоступны")
    return parts


class Workspace:
    def __init__(self, root):
        self.root = root

    @contextmanager
    def directory(self, parts, create=False):
        try:
            fd = os.open(self.root, DIRECTORY)
        except OSError as error:
            raise ValueError("Рабочая папка недоступна") from error
        try:
            for part in parts:
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o750, dir_fd=fd)
                next_fd = os.open(part, DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd
        except FileNotFoundError as error:
            raise ValueError("Путь не найден в рабочей папке") from error
        except OSError as error:
            raise ValueError("Нет доступа к файлу или путь содержит ссылку") from error
        finally:
            os.close(fd)

    @staticmethod
    @contextmanager
    def open_file(directory, name):
        fd = os.open(name, REGULAR, dir_fd=directory)
        with os.fdopen(fd, "rb") as opened:
            info = os.fstat(opened.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Поддерживаются обычные файлы без ссылок")
            if info.st_size > MAX_FILE:
                raise ValueError("Файл превышает лимит 8 МБ")
            yield opened

    @classmethod
    def read_at(cls, directory, name):
        with cls.open_file(directory, name) as opened:
            data = opened.read(MAX_FILE + 1)
            if len(data) > MAX_FILE:
                raise ValueError("Файл превышает лимит 8 МБ")
            return data

    def read(self, path):
        parts = parts_for(path)
        with self.directory(parts[:-1]) as directory:
            return self.read_at(directory, parts[-1])

    def check_file(self, path):
        parts = parts_for(path)
        with self.directory(parts[:-1]) as directory, self.open_file(directory, parts[-1]):
            pass

    def read_page(self, path, offset):
        if offset < 0:
            raise ValueError("Смещение должно быть неотрицательным")
        data = self.read(path)
        if PurePosixPath(path).suffix.lower() in TEXT_TYPES:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("cp1251", errors="replace")
        else:
            text = read_text(path, data)
        end = offset + PAGE
        return {
            "path": path, "sha256": hashlib.sha256(data).hexdigest(),
            "text": text[offset:end], "next_offset": end if end < len(text) else None,
        }

    def list(self, path):
        parts = parts_for(path, root_allowed=True)
        entries = []
        truncated = False
        with self.directory(parts) as directory, os.scandir(directory) as scanned:
            for index, entry in enumerate(islice(scanned, 401)):
                if index == 400 or len(entries) == 100:
                    truncated = True
                    break
                if not valid_part(entry.name):
                    continue
                entry_path = "/".join([*parts, entry.name])
                if len(parts) == 12 or len(entry_path) > 800:
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    entries.append({"path": entry_path, "type": "directory"})
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    entries.append({"path": entry_path, "type": "file", "bytes": info.st_size})
        entries.sort(key=lambda item: (item["type"] != "directory", item["path"].casefold()))
        return {"entries": entries, "truncated": truncated}

    def write(self, path, data, expected_sha256, *, create_dirs=True):
        parts = parts_for(path)
        if len(data) > MAX_FILE:
            raise ValueError("Файл превышает лимит 8 МБ")
        if expected_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("Укажите SHA-256 из чтения файла или пустую строку для нового файла")
        digest = hashlib.sha256(data).hexdigest()
        name = parts[-1]
        with self.directory(parts[:-1], create=create_dirs and not expected_sha256) as directory:
            try:
                current = self.read_at(directory, name)
            except FileNotFoundError:
                current = None
            if current == data:
                return {"path": path, "sha256": digest}
            if current is not None:
                if hashlib.sha256(current).hexdigest() != expected_sha256:
                    raise WorkspaceConflict("Файл с таким именем уже существует или изменился")
            elif expected_sha256:
                    raise WorkspaceConflict("Файл для обновления не найден")
            temporary = ".aster-" + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=directory)
            try:
                with os.fdopen(fd, "wb") as opened:
                    opened.write(data)
                    opened.flush()
                    os.fsync(opened.fileno())
                if expected_sha256:
                    if hashlib.sha256(self.read_at(directory, name)).hexdigest() != expected_sha256:
                        raise WorkspaceConflict("Файл изменился во время сохранения")
                    os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                else:
                    try:
                        os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    except FileExistsError as error:
                        raise WorkspaceConflict("Файл с таким именем уже существует") from error
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
            os.fsync(directory)
        return {"path": path, "sha256": digest}
