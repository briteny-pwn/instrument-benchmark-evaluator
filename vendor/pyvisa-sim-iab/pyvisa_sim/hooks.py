"""Explicit command hooks for the Instrument Benchmark PyVISA-sim build."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .devices import Device
    from .component import OptionalBytes


@dataclass(frozen=True)
class CommandContext:
    """One command after PyVISA-sim has framed it for a device."""

    device: "Device"
    resource_name: str
    command: bytes


class CommandRejected(RuntimeError):
    """A hook rejected a command before native matching changed state."""

    def __init__(
        self,
        code: int,
        message: str,
        error_key: str = "command_error",
    ) -> None:
        self.code = code
        self.message = message
        self.error_key = error_key
        super().__init__(f"{code}: {message}")


class HookProvider(Protocol):
    def before_command(self, context: CommandContext) -> None:
        raise NotImplementedError

    def dynamic_response(
        self,
        context: CommandContext,
        response: "OptionalBytes",
    ) -> "OptionalBytes":
        raise NotImplementedError

    def after_command(
        self,
        context: CommandContext,
        response: "OptionalBytes",
    ) -> None:
        raise NotImplementedError

    def on_error(
        self,
        context: CommandContext,
        error: CommandRejected,
    ) -> None:
        raise NotImplementedError


_hook_provider: HookProvider | None = None


def install_hook_provider(provider: HookProvider | None) -> None:
    """Install the process-wide provider used by subsequently handled commands."""

    global _hook_provider
    _hook_provider = provider


def get_hook_provider() -> HookProvider | None:
    return _hook_provider
