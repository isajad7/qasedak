import dataclasses
import os
import subprocess
import tempfile


try:
    import paramiko
except Exception:  # pragma: no cover - optional dependency
    paramiko = None


SECRET_REDACTION = "<redacted>"


class SSHClientError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class SSHCredentials:
    host: str
    user: str
    port: int = 22
    key_path: str = ""
    password: str = ""
    timeout: int = 15


@dataclasses.dataclass(frozen=True)
class SSHCommandResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self):
        return self.exit_code == 0

    def redacted(self):
        return SSHCommandResult(
            exit_code=self.exit_code,
            stdout=redact_secrets(self.stdout),
            stderr=redact_secrets(self.stderr),
        )


@dataclasses.dataclass(frozen=True)
class SSHConnectionStatus:
    ok: bool
    host: str
    user: str
    port: int
    message: str = ""


def redact_secrets(value):
    if not value:
        return value
    text = str(value)
    for marker in ("TELEGRAM_BOT_TOKEN=", "DATABASE_URL=", "DJANGO_SECRET_KEY=", "POSTGRES_PASSWORD="):
        if marker in text:
            parts = []
            for line in text.splitlines():
                if marker in line:
                    parts.append(f"{marker}{SECRET_REDACTION}")
                else:
                    parts.append(line)
            text = "\n".join(parts)
    return text


class SSHClient:
    """Small SSH abstraction.

    Paramiko is used when available, especially for password-based sessions.
    The subprocess fallback supports key-based SSH and SCP without adding
    secrets to command strings.
    """

    def __init__(self, credentials):
        self.credentials = credentials
        self._client = None

    def verify_connection(self):
        try:
            result = self.run("printf qasedak-ok", timeout=self.credentials.timeout)
        except Exception as exc:
            return SSHConnectionStatus(
                ok=False,
                host=self.credentials.host,
                user=self.credentials.user,
                port=self.credentials.port,
                message=exc.__class__.__name__,
            )
        return SSHConnectionStatus(
            ok=result.ok and result.stdout.strip() == "qasedak-ok",
            host=self.credentials.host,
            user=self.credentials.user,
            port=self.credentials.port,
            message="connected" if result.ok else result.redacted().stderr[:300],
        )

    def run(self, command, *, timeout=60, sensitive=False):
        self._validate_command(command)
        if paramiko is not None:
            return self._run_paramiko(command, timeout=timeout).redacted()
        return self._run_subprocess(command, timeout=timeout).redacted()

    def put_file(self, remote_path, content, *, mode="600"):
        if "\x00" in remote_path or "\n" in remote_path:
            raise SSHClientError("Invalid remote path.")
        if paramiko is not None:
            return self._put_file_paramiko(remote_path, content, mode=mode)
        return self._put_file_subprocess(remote_path, content, mode=mode)

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None

    def _validate_command(self, command):
        if not isinstance(command, str) or not command.strip():
            raise SSHClientError("Remote command must be a non-empty string.")
        if "\x00" in command:
            raise SSHClientError("Remote command contains invalid bytes.")
        if len(command) > 12000:
            raise SSHClientError("Remote command is too long.")

    def _connect_paramiko(self):
        if self._client is not None:
            return self._client
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = {
            "hostname": self.credentials.host,
            "port": int(self.credentials.port),
            "username": self.credentials.user,
            "timeout": self.credentials.timeout,
        }
        if self.credentials.key_path:
            kwargs["key_filename"] = self.credentials.key_path
        if self.credentials.password:
            kwargs["password"] = self.credentials.password
        client.connect(**kwargs)
        self._client = client
        return client

    def _run_paramiko(self, command, *, timeout):
        client = self._connect_paramiko()
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        stdin.close()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        return SSHCommandResult(exit_code=exit_code, stdout=out, stderr=err)

    def _put_file_paramiko(self, remote_path, content, *, mode):
        client = self._connect_paramiko()
        with client.open_sftp() as sftp:
            with sftp.file(remote_path, "w") as remote_file:
                remote_file.write(content)
            sftp.chmod(remote_path, int(mode, 8))

    def _ssh_base_argv(self):
        if self.credentials.password:
            raise SSHClientError("Password SSH requires paramiko.")
        argv = [
            "ssh",
            "-p",
            str(int(self.credentials.port)),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ConnectTimeout={int(self.credentials.timeout)}",
        ]
        if self.credentials.key_path:
            argv.extend(["-i", self.credentials.key_path])
        argv.append(f"{self.credentials.user}@{self.credentials.host}")
        return argv

    def _run_subprocess(self, command, *, timeout):
        argv = self._ssh_base_argv()
        argv.append(command)
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return SSHCommandResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def _put_file_subprocess(self, remote_path, content, *, mode):
        if self.credentials.password:
            raise SSHClientError("Password SCP requires paramiko.")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name
        try:
            scp_argv = [
                "scp",
                "-P",
                str(int(self.credentials.port)),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
            ]
            if self.credentials.key_path:
                scp_argv.extend(["-i", self.credentials.key_path])
            scp_argv.extend([temp_path, f"{self.credentials.user}@{self.credentials.host}:{remote_path}"])
            completed = subprocess.run(scp_argv, check=False, capture_output=True, text=True, timeout=60)
            if completed.returncode != 0:
                raise SSHClientError(redact_secrets(completed.stderr) or "SCP upload failed.")
            chmod_result = self.run(f"chmod {mode} {shell_quote(remote_path)}", timeout=30)
            if not chmod_result.ok:
                raise SSHClientError(chmod_result.stderr or "Could not chmod uploaded file.")
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def shell_quote(value):
    import shlex

    return shlex.quote(str(value))
