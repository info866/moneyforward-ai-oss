from __future__ import annotations

import os
import select
import signal
import subprocess
import time
from dataclasses import dataclass

from .office_profile import OfficeProfile, OfficeProfileError, resolve_office_profile


FORBIDDEN_LEGACY_SERVICE = "shared-credential-store"
DEFAULT_SERVICE = None


@dataclass(frozen=True)
class KeychainSecretRef:
    service: str
    account: str


class KeychainClient:
    def __init__(self, service: str | None = None):
        if service == FORBIDDEN_LEGACY_SERVICE:
            raise OfficeProfileError(
                "legacy shared Money Forward Keychain service is disabled; select a dedicated office profile"
            )
        profile = resolve_office_profile(required=False) if service is None else None
        if service is None and profile is None:
            raise OfficeProfileError(
                "Keychain service must be explicit for local/tests or selected through MF_OFFICE_PROFILE"
            )
        if service is not None:
            self.service = service
        else:
            assert profile is not None
            self.service = profile.moneyforward_keychain_service

    @classmethod
    def for_office(cls, profile: OfficeProfile) -> "KeychainClient":
        return cls(profile.moneyforward_keychain_service)

    def get_secret(self, account: str) -> str:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", self.service, "-a", account, "-w"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise KeyError(f"missing keychain secret: service={self.service} account={account}")
        return result.stdout.rstrip("\n")

    def set_secret(self, account: str, value: str) -> None:
        # Validate before touching an existing item. The `-U` path below adds
        # or updates in one security(1) operation, so a prompt failure cannot
        # erase the previously working credential first.
        _validate_keychain_secret_value(value)
        _add_generic_password_from_memory(self.service, account, value)

    def delete_secret(self, account: str, *, missing_ok: bool = True) -> bool:
        result = subprocess.run(
            ["security", "delete-generic-password", "-s", self.service, "-a", account],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
        stderr = str(result.stderr or "").lower()
        missing = result.returncode == 44 or "could not be found" in stderr or "item not found" in stderr
        if missing and missing_ok:
            return False
        raise RuntimeError(f"failed to delete keychain secret: service={self.service} account={account}")

    def ref(self, account: str) -> KeychainSecretRef:
        return KeychainSecretRef(self.service, account)


def _add_generic_password_from_memory(service: str, account: str, value: str) -> None:
    _validate_keychain_secret_value(value)

    pid, terminal_fd = os.forkpty()
    if pid == 0:  # pragma: no cover - replaced by the security process
        os.execvp(
            "security",
            ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w"],
        )

    reaped = False
    try:
        buffer = _wait_for_keychain_prompt(
            pid,
            terminal_fd,
            marker=b"password data for new item:",
            buffer=b"",
        )
        os.write(terminal_fd, value.encode("utf-8") + b"\r")
        _wait_for_keychain_prompt(
            pid,
            terminal_fd,
            marker=b"retype password for new item:",
            buffer=buffer,
        )
        os.write(terminal_fd, value.encode("utf-8") + b"\r")
        status = _wait_for_keychain_exit(pid, terminal_fd)
        reaped = True
        if status != 0:
            raise RuntimeError(f"failed to store keychain secret: service={service} account={account}")
    finally:
        if not reaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        try:
            os.close(terminal_fd)
        except OSError:
            pass


def _validate_keychain_secret_value(value: str) -> None:
    if not value:
        raise ValueError("keychain secret value must not be empty")
    if "\r" in value or "\n" in value:
        raise ValueError("keychain secret value must not contain line breaks")


def _wait_for_keychain_prompt(
    pid: int,
    terminal_fd: int,
    *,
    marker: bytes,
    buffer: bytes,
    timeout_seconds: float = 5.0,
) -> bytes:
    deadline = time.monotonic() + timeout_seconds
    while marker not in buffer.lower() and time.monotonic() < deadline:
        finished_pid, status = os.waitpid(pid, os.WNOHANG)
        if finished_pid:
            exit_code = os.waitstatus_to_exitcode(status)
            raise RuntimeError(f"security command exited before its hidden prompt: status={exit_code}")
        readable, _, _ = select.select([terminal_fd], [], [], 0.1)
        if readable:
            try:
                buffer += os.read(terminal_fd, 4096)
            except OSError:
                break
        if len(buffer) > 16384:
            buffer = buffer[-16384:]
    if marker not in buffer.lower():
        raise RuntimeError("security command did not present its hidden password prompt")
    return buffer


def _wait_for_keychain_exit(pid: int, terminal_fd: int, timeout_seconds: float = 5.0) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        finished_pid, status = os.waitpid(pid, os.WNOHANG)
        if finished_pid:
            return os.waitstatus_to_exitcode(status)
        readable, _, _ = select.select([terminal_fd], [], [], 0.1)
        if readable:
            try:
                os.read(terminal_fd, 4096)
            except OSError:
                pass
    raise RuntimeError("security command timed out while storing the Keychain secret")
