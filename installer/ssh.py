import base64
import hashlib
import hmac
import io
import ipaddress
import re
import secrets
import shlex
import socket
import tarfile
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import paramiko

HERE = Path(__file__).resolve().parent
NODE = HERE.parent / "node"
STAGES = {
    "preflight": "Проверяю права на VPS",
    "docker": "Проверяю и при необходимости устанавливаю Docker",
    "files": "Готовлю файлы Aster",
    "build": "Собираю и запускаю панель и Chromium",
    "rollback": "Возвращаю предыдущую конфигурацию",
    "ready": "Контейнеры запущены",
}


class OperationError(Exception):
    pass


@dataclass(frozen=True)
class Target:
    address: str
    port: int

    @classmethod
    def parse(cls, raw):
        values = raw.strip().split()
        if len(values) not in {1, 2}:
            raise ValueError("Укажите публичный IP и, при необходимости, порт через пробел")
        ip = ipaddress.ip_address(values[0])
        if not ip.is_global or ip.is_multicast or (ip.version == 6 and (ip.ipv4_mapped or "%" in values[0])):
            raise ValueError("Укажите публичный IP вашего VPS")
        port = int(values[1]) if len(values) == 2 else 22
        if not 1 <= port <= 65535:
            raise ValueError("Порт должен быть от 1 до 65535")
        return cls(str(ip), port)

    @property
    def authority(self):
        return f"[{self.address}]" if ":" in self.address else self.address

    @property
    def url(self):
        return "https://" + self.authority


def fingerprint(key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


@contextmanager
def connection(target, login, password, expected):
    transport = None
    sock = None
    try:
        sock = socket.create_connection((target.address, target.port), timeout=12)
        transport = paramiko.Transport(sock)
        transport.get_security_options().key_types = ("ssh-ed25519",)
        transport.banner_timeout = 12
        transport.auth_timeout = 15
        transport.start_client(timeout=15)
        if not hmac.compare_digest(fingerprint(transport.get_remote_server_key()), expected):
            raise OperationError("Отпечаток SSH отличается от введённого. Сверьте его в консоли хостинга.")
        transport.auth_password(login, password, fallback=False)
        if not transport.is_authenticated():
            raise OperationError("Не удалось войти по паролю SSH.")
        transport.set_keepalive(15)
        yield transport
    except paramiko.AuthenticationException:
        raise OperationError("SSH отклонил логин или пароль.") from None
    except (OSError, EOFError, paramiko.SSHException):
        raise OperationError("SSH недоступен. Проверьте IP, порт, ключ Ed25519 и доступность VPS.") from None
    finally:
        if transport:
            transport.close()
        if sock:
            sock.close()


def run_command(transport, command, seconds, progress=None):
    out = bytearray()
    pending = b""
    reported = None
    expires = time.monotonic() + seconds

    def feed(chunk):
        nonlocal pending, reported
        if progress:
            lines = (pending + chunk).split(b"\n")
            pending = lines.pop()[-400:]
            for line in lines:
                name = line.decode(errors="replace").strip().removeprefix("ACTER_STAGE=")
                if line.startswith(b"ACTER_STAGE=") and name in STAGES:
                    reported = name
                    progress(STAGES[name])
        else:
            out.extend(chunk)
            if len(out) > 16_384:
                raise OperationError("VPS вернул слишком много данных.")

    with transport.open_session(timeout=15) as chan:
        chan.settimeout(15)
        chan.set_combine_stderr(True)
        chan.exec_command(command)
        while time.monotonic() < expires:
            if chan.recv_ready():
                feed(chan.recv(8192))
            elif chan.exit_status_ready():
                if chan.recv_exit_status() != 0:
                    if progress:
                        point = f" Этап: {STAGES[reported]}." if reported else ""
                        raise OperationError("Установка на VPS завершилась ошибкой." + point)
                    raise OperationError("VPS не смог выполнить команду.")
                return bytes(out).decode(errors="replace")
            else:
                time.sleep(0.1)
    raise OperationError("VPS не завершил операцию за отведённое время.")


def node_archive():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(NODE.rglob("*")):
            if path.is_file() and path.name != ".env" and path.suffix != ".pyc" and "tests" not in path.relative_to(NODE).parts:
                archive.add(path, arcname=path.relative_to(NODE), recursive=False)
    return buffer.getvalue()


def node_env(target, token, owner, key):
    values = {
        "PANEL_PASSWORD": secrets.token_urlsafe(26), "DATA_SECRET": secrets.token_hex(32),
        "BROWSER_TOKEN": secrets.token_hex(32), "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_OWNER_ID": owner, "TELEGRAM_POLL": 0, "GATEWAY_KEY": key,
        "PUBLIC_URL": target.url, "PUBLIC_HOST": target.authority, "TLS_HOST": target.address,
    }
    return "".join(f"{name}={value}\n" for name, value in values.items()).encode()


def install(transport, login, target, token, owner, key, progress):
    directory = "/tmp/acter-" + secrets.token_hex(12)
    sudo = "" if login == "root" else "sudo -n "
    staged = {"node.tar.gz": node_archive(), "node.env": node_env(target, token, owner, key),
              "bootstrap.sh": (HERE / "bootstrap.sh").read_bytes()}
    with paramiko.SFTPClient.from_transport(transport) as sftp:
        sftp.get_channel().settimeout(30)
        sftp.mkdir(directory, 0o700)
        try:
            for filename, data in staged.items():
                path = f"{directory}/{filename}"
                with sftp.open(path, "wx") as handle:
                    sftp.chmod(path, 0o600)
                    handle.write(data)
            progress("Файлы переданы по SSH")
            run_command(transport, f"{sudo}bash -- {directory}/bootstrap.sh {directory}", 1200, progress)
        finally:
            for filename in staged:
                with suppress(OSError, paramiko.SSHException):
                    sftp.remove(f"{directory}/{filename}")
            with suppress(OSError, paramiko.SSHException):
                sftp.rmdir(directory)


def metrics(transport):
    output = run_command(transport, "sh -c " + shlex.quote((HERE / "metrics.sh").read_text()), 12)
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    try:
        cpu = float(fields["cpu"])
        mem_used, mem_total, disk_used, disk_total, cores = (
            int(fields[key]) for key in ("mem_used", "mem_total", "disk_used", "disk_total", "cores")
        )
        loads = fields["load"]
        if not 0 <= cpu <= 100 or not (0 <= mem_used <= mem_total and 0 <= disk_used <= disk_total and cores > 0):
            raise ValueError
        if not re.fullmatch(r"\d+(?:\.\d+)?(?: \d+(?:\.\d+)?){2}", loads):
            raise ValueError
    except (KeyError, ValueError):
        raise OperationError("VPS не вернул читаемые метрики Linux.") from None
    gib = 1024 * 1024
    return (
        f"CPU — {cpu:.1f}% · ядер {cores}\n"
        f"ОЗУ — {mem_used/gib:.2f} / {mem_total/gib:.2f} ГиБ\n"
        f"Диск / — {disk_used/gib:.2f} / {disk_total/gib:.2f} ГиБ\n"
        f"Средняя нагрузка за 1, 5 и 15 минут — {loads}"
    )
