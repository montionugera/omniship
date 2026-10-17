import re
import shlex
from calendar import Day
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields, replace
from datetime import time
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import ClassVar, Self

import yaml

from omniship.config.models import NodeConfig, OmniShipConfig
from omniship.core.execution import CacheSpec, SecretRef
from omniship.core.stage import Stage
from omniship.plugins.api import GeneratedFile
from omniship.plugins.registry import PluginRegistry
from omniship.workflow.model import GitPluginPackage, Pipeline

from .dependencies import LOCK_FILENAME, GitHubActionLock, resolve_git_plugin
from .runtime import PAGES_STAGING_PATH
from .timezones import GitHubTimezone

ARTIFACT_PATH = ".omniship/handoff"
ARTIFACT_IMPORT_PATH = ".omniship/imports"


def _display_name(value: str) -> str:
    words = value.replace("_", "-").split("-")
    return " ".join(
        "GitHub" if word.casefold() == "github" else word.title() for word in words
    )


class GitHubRunner(StrEnum):
    UBUNTU_SLIM = "ubuntu-slim"
    UBUNTU_LATEST = "ubuntu-latest"
    UBUNTU_22_04 = "ubuntu-22.04"
    UBUNTU_24_04 = "ubuntu-24.04"
    UBUNTU_26_04 = "ubuntu-26.04"
    UBUNTU_22_04_ARM = "ubuntu-22.04-arm"
    UBUNTU_24_04_ARM = "ubuntu-24.04-arm"
    UBUNTU_26_04_ARM = "ubuntu-26.04-arm"
    WINDOWS_LATEST = "windows-latest"
    WINDOWS_2022 = "windows-2022"
    WINDOWS_2025 = "windows-2025"
    WINDOWS_2025_VS2026 = "windows-2025-vs2026"
    WINDOWS_11_ARM = "windows-11-arm"
    WINDOWS_11_VS2026_ARM = "windows-11-vs2026-arm"
    MACOS_LATEST = "macos-latest"
    MACOS_14 = "macos-14"
    MACOS_15 = "macos-15"
    MACOS_26 = "macos-26"
    MACOS_15_INTEL = "macos-15-intel"
    MACOS_26_INTEL = "macos-26-intel"
    XCODE_27 = "xcode-27"


class GitHubShell(StrEnum):
    BASH = "bash"
    CMD = "cmd"
    POWERSHELL = "powershell"
    PWSH = "pwsh"
    PYTHON = "python"
    SH = "sh"


class GitHubBootstrap(StrEnum):
    ISOLATED = "isolated"
    WORKSPACE = "workspace"


class GitHubPermission(StrEnum):
    NONE = "none"
    READ = "read"
    WRITE = "write"


_PERMISSION_RANK = {
    GitHubPermission.NONE: 0,
    GitHubPermission.READ: 1,
    GitHubPermission.WRITE: 2,
}


@dataclass(frozen=True)
class GitHubPermissions:
    actions: GitHubPermission | None = None
    artifact_metadata: GitHubPermission | None = None
    attestations: GitHubPermission | None = None
    checks: GitHubPermission | None = None
    code_quality: GitHubPermission | None = None
    contents: GitHubPermission | None = None
    deployments: GitHubPermission | None = None
    discussions: GitHubPermission | None = None
    id_token: GitHubPermission | None = None
    issues: GitHubPermission | None = None
    models: GitHubPermission | None = None
    packages: GitHubPermission | None = None
    pages: GitHubPermission | None = None
    pull_requests: GitHubPermission | None = None
    security_events: GitHubPermission | None = None
    statuses: GitHubPermission | None = None
    vulnerability_alerts: GitHubPermission | None = None

    def __post_init__(self) -> None:
        for permission_field in fields(self):
            value = getattr(self, permission_field.name)
            if value is not None and not isinstance(value, GitHubPermission):
                raise TypeError(
                    f"{permission_field.name} must be a GitHubPermission value"
                )
        for write_or_none in ("id_token",):
            if getattr(self, write_or_none) == GitHubPermission.READ:
                raise ValueError(f"{write_or_none} supports only write or none")
        for read_or_none in ("models", "vulnerability_alerts"):
            if getattr(self, read_or_none) == GitHubPermission.WRITE:
                raise ValueError(f"{read_or_none} supports only read or none")

    def to_document(self) -> dict[str, str]:
        return {
            permission_field.name.replace("_", "-"): value.value
            for permission_field in fields(self)
            if (value := getattr(self, permission_field.name)) is not None
        }

    def satisfies(self, required: GitHubPermissions) -> bool:
        for permission_field in fields(self):
            minimum = getattr(required, permission_field.name)
            if minimum is None:
                continue
            granted = getattr(self, permission_field.name) or GitHubPermission.NONE
            if _PERMISSION_RANK[granted] < _PERMISSION_RANK[minimum]:
                return False
        return True

    def with_minimum(
        self,
        required: GitHubPermissions,
        *,
        node_name: str,
    ) -> GitHubPermissions:
        values: dict[str, GitHubPermission] = {}
        for permission_field in fields(self):
            minimum = getattr(required, permission_field.name)
            granted = getattr(self, permission_field.name)
            if minimum is None:
                continue
            if (
                granted is not None
                and _PERMISSION_RANK[granted] < _PERMISSION_RANK[minimum]
            ):
                scope = permission_field.name.replace("_", "-")
                raise ValueError(
                    f"Node '{node_name}' requires '{scope}: {minimum.value}', "
                    f"but its GitHub job grants '{scope}: {granted.value}'"
                )
            if granted is None:
                values[permission_field.name] = minimum
        return replace(self, **values)


@dataclass(frozen=True)
class GitHubMatrix:
    """Extra axes and combinations for a task's GitHub Actions job."""

    axes: Mapping[str, Iterable[str | int | bool]] = field(default_factory=dict)
    include: Iterable[Mapping[str, str | int | bool]] = ()
    exclude: Iterable[Mapping[str, str | int | bool]] = ()

    def __post_init__(self) -> None:
        axes = {name: tuple(values) for name, values in self.axes.items()}
        if any(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name) is None
            or name in {"runner", "include", "exclude"}
            or not values
            or any(not isinstance(value, (str, int, bool)) for value in values)
            for name, values in axes.items()
        ):
            raise ValueError("GitHub matrix axes require valid names and scalar values")
        for field_name in ("include", "exclude"):
            combinations = tuple(dict(item) for item in getattr(self, field_name))
            if any(
                not item
                or any(
                    re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key) is None
                    or not isinstance(value, (str, int, bool))
                    for key, value in item.items()
                )
                for item in combinations
            ):
                raise ValueError(
                    f"GitHub matrix {field_name} entries must be scalar mappings"
                )
            object.__setattr__(self, field_name, combinations)
        if not axes and not self.include:
            raise ValueError("GitHub matrix requires an axis or include entries")
        object.__setattr__(self, "axes", axes)


@dataclass(frozen=True)
class GitHubContainer:
    """A job or service container on a Linux GitHub runner."""

    image: str
    env: Mapping[str, str | SecretRef] = field(default_factory=dict)
    ports: Iterable[int | str] = ()
    volumes: Iterable[str] = ()
    options: str | None = None
    credentials: Mapping[str, str | SecretRef] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.image, str) or not self.image:
            raise ValueError("GitHub container image must be non-empty")
        for field_name in ("env", "credentials"):
            values = dict(getattr(self, field_name))
            if any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, (str, SecretRef))
                for key, value in values.items()
            ):
                raise TypeError(
                    f"GitHub container {field_name} must map names to strings or secrets"
                )
            object.__setattr__(self, field_name, values)
        ports = tuple(self.ports)
        volumes = tuple(self.volumes)
        if any(not isinstance(port, (int, str)) for port in ports):
            raise TypeError("GitHub container ports must be integers or strings")
        if any(not isinstance(volume, str) or not volume for volume in volumes):
            raise TypeError("GitHub container volumes must be non-empty strings")
        object.__setattr__(self, "ports", ports)
        object.__setattr__(self, "volumes", volumes)

    def to_document(self) -> dict[str, object]:
        def render(value: str | SecretRef) -> str:
            return (
                f"${{{{ secrets.{value.name} }}}}"
                if isinstance(value, SecretRef)
                else value
            )

        document: dict[str, object] = {"image": self.image}
        if self.env:
            document["env"] = {key: render(value) for key, value in self.env.items()}
        if self.ports:
            document["ports"] = list(self.ports)
        if self.volumes:
            document["volumes"] = list(self.volumes)
        if self.options is not None:
            document["options"] = self.options
        if self.credentials:
            document["credentials"] = {
                key: render(value) for key, value in self.credentials.items()
            }
        return document


@dataclass(frozen=True)
class GitHubJob:
    runners: tuple[GitHubRunner, ...]
    fail_fast: bool = False
    max_parallel: int | None = None
    permissions: GitHubPermissions | None = None
    timeout_minutes: int | None = None
    environment: str | None = None
    working_directory: str | None = None
    shell: GitHubShell | None = None
    env: Mapping[str, str | SecretRef | _GitHubExpression] = field(default_factory=dict)
    caches: tuple[CacheSpec, ...] = ()
    matrix: GitHubMatrix | None = None
    container: GitHubContainer | None = None
    services: Mapping[str, GitHubContainer] = field(default_factory=dict)
    before_steps: tuple[GitHubActionStep, ...] = ()
    after_steps: tuple[GitHubActionStep, ...] = ()
    outputs: tuple[str, ...] = ()
    environment_url: str | None = None

    def __post_init__(self) -> None:
        if not self.runners:
            raise ValueError("A GitHub job requires at least one runner")
        if any(not isinstance(runner, GitHubRunner) for runner in self.runners):
            raise TypeError("GitHub job runners must be GitHubRunner values")
        if len(set(self.runners)) != len(self.runners):
            raise ValueError("GitHub job runners must be unique")
        if self.max_parallel is not None and (
            not isinstance(self.max_parallel, int)
            or isinstance(self.max_parallel, bool)
            or self.max_parallel < 1
        ):
            raise ValueError("GitHub max_parallel must be a positive integer")
        if self.permissions is not None and not isinstance(
            self.permissions, GitHubPermissions
        ):
            raise TypeError("permissions must be a GitHubPermissions value")
        if self.timeout_minutes is not None and not 1 <= self.timeout_minutes <= 360:
            raise ValueError("GitHub job timeout must be between 1 and 360 minutes")
        for field_name in ("environment", "working_directory"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value):
                raise TypeError(f"{field_name} must be a non-empty string")
        if self.working_directory is not None:
            normalized_directory = self.working_directory.replace("\\", "/")
            directory = PurePosixPath(normalized_directory)
            if (
                directory.is_absolute()
                or ".." in directory.parts
                or PureWindowsPath(normalized_directory).drive
            ):
                raise ValueError("working_directory must stay inside the workspace")
        if self.shell is not None and not isinstance(self.shell, GitHubShell):
            raise TypeError("shell must be a GitHubShell value")
        normalized_env = dict(self.env)
        if any(
            not isinstance(name, str)
            or not name
            or not isinstance(value, (str, SecretRef, _GitHubExpression))
            for name, value in normalized_env.items()
        ):
            raise TypeError(
                "env must map non-empty names to strings or GitHub references"
            )
        normalized_caches = tuple(self.caches)
        if any(not isinstance(cache, CacheSpec) for cache in normalized_caches):
            raise TypeError("caches must contain CacheSpec values")
        object.__setattr__(self, "env", normalized_env)
        object.__setattr__(self, "caches", normalized_caches)
        if self.matrix is not None and not isinstance(self.matrix, GitHubMatrix):
            raise TypeError("matrix must be a GitHubMatrix value")
        if self.matrix is not None:
            runner_names = {runner.value for runner in self.runners}
            if any(
                item.get("runner") not in runner_names for item in self.matrix.include
            ):
                raise ValueError(
                    "GitHub matrix include entries require a configured runner"
                )
        if self.container is not None and not isinstance(
            self.container, GitHubContainer
        ):
            raise TypeError("container must be a GitHubContainer value")
        services = dict(self.services)
        if any(
            not name or not isinstance(value, GitHubContainer)
            for name, value in services.items()
        ):
            raise TypeError("services must map names to GitHubContainer values")
        if (self.container or services) and any(
            not runner.value.startswith("ubuntu") for runner in self.runners
        ):
            raise ValueError("GitHub containers require Linux runners")
        object.__setattr__(self, "services", services)
        for field_name in ("before_steps", "after_steps"):
            steps = tuple(getattr(self, field_name))
            if any(not isinstance(step, GitHubActionStep) for step in steps):
                raise TypeError(f"{field_name} must contain GitHubActionStep values")
            object.__setattr__(self, field_name, steps)
        outputs = tuple(self.outputs)
        if any(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None for name in outputs
        ):
            raise ValueError("GitHub output names must be identifiers")
        if len(set(outputs)) != len(outputs):
            raise ValueError("GitHub output names must be unique")
        if outputs and (len(self.runners) > 1 or self.matrix is not None):
            raise ValueError("GitHub job outputs require a single matrix combination")
        object.__setattr__(self, "outputs", outputs)
        if self.environment_url is not None and self.environment is None:
            raise ValueError("environment_url requires an environment")


def _normalize_trigger_values(
    values: Iterable[str], *, field_name: str
) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{field_name} must be an iterable of strings")
    normalized = tuple(values)
    if any(not isinstance(value, str) or not value for value in normalized):
        raise TypeError(f"{field_name} must contain non-empty strings")
    return normalized


@dataclass(frozen=True)
class GitHubPush:
    branches: Iterable[str] = ()
    branches_ignore: Iterable[str] = ()
    tags: Iterable[str] = ()
    tags_ignore: Iterable[str] = ()
    paths: Iterable[str] = ()
    paths_ignore: Iterable[str] = ()
    event: ClassVar[str] = "push"

    def __post_init__(self) -> None:
        for field_name in (
            "branches",
            "branches_ignore",
            "tags",
            "tags_ignore",
            "paths",
            "paths_ignore",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_trigger_values(
                    getattr(self, field_name), field_name=field_name
                ),
            )
        self._reject_conflicting_filters("branches", "branches_ignore")
        self._reject_conflicting_filters("tags", "tags_ignore")
        self._reject_conflicting_filters("paths", "paths_ignore")

    def _reject_conflicting_filters(self, include: str, exclude: str) -> None:
        if getattr(self, include) and getattr(self, exclude):
            raise ValueError(f"GitHub push cannot use both {include} and {exclude}")

    def to_document(self) -> dict[str, object]:
        return _trigger_filters_document(self)


@dataclass(frozen=True)
class GitHubPullRequest:
    branches: Iterable[str] = ()
    branches_ignore: Iterable[str] = ()
    paths: Iterable[str] = ()
    paths_ignore: Iterable[str] = ()
    types: Iterable[str] = ()
    event: ClassVar[str] = "pull_request"

    def __post_init__(self) -> None:
        for field_name in (
            "branches",
            "branches_ignore",
            "paths",
            "paths_ignore",
            "types",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_trigger_values(
                    getattr(self, field_name), field_name=field_name
                ),
            )
        self._reject_conflicting_filters("branches", "branches_ignore")
        self._reject_conflicting_filters("paths", "paths_ignore")

    def _reject_conflicting_filters(self, include: str, exclude: str) -> None:
        if getattr(self, include) and getattr(self, exclude):
            raise ValueError(
                f"GitHub pull request cannot use both {include} and {exclude}"
            )

    def to_document(self) -> dict[str, object]:
        return _trigger_filters_document(self)


@dataclass(frozen=True)
class GitHubStringInput:
    name: str
    description: str | None = None
    required: bool = False
    default: str | None = None

    def __post_init__(self) -> None:
        _validate_workflow_input(self.name, self.description, self.required)
        if self.default is not None and not isinstance(self.default, str):
            raise TypeError("GitHub string input default must be a string")

    def to_document(self) -> dict[str, object]:
        return _workflow_input_document(self, input_type="string")


@dataclass(frozen=True)
class GitHubBooleanInput:
    name: str
    description: str | None = None
    required: bool = False
    default: bool | None = None

    def __post_init__(self) -> None:
        _validate_workflow_input(self.name, self.description, self.required)
        if self.default is not None and not isinstance(self.default, bool):
            raise TypeError("GitHub boolean input default must be a boolean")

    def to_document(self) -> dict[str, object]:
        return _workflow_input_document(self, input_type="boolean")


GitHubInput = GitHubStringInput | GitHubBooleanInput


def _validate_workflow_input(
    name: str,
    description: str | None,
    required: bool,
) -> None:
    if not isinstance(name, str) or not name:
        raise TypeError("GitHub workflow input name must be a non-empty string")
    if description is not None and not isinstance(description, str):
        raise TypeError("GitHub workflow input description must be a string")
    if not isinstance(required, bool):
        raise TypeError("GitHub workflow input required must be a boolean")


def _workflow_input_document(
    workflow_input: GitHubInput,
    *,
    input_type: str,
) -> dict[str, object]:
    document: dict[str, object] = {}
    if workflow_input.description is not None:
        document["description"] = workflow_input.description
    if workflow_input.required:
        document["required"] = True
    if workflow_input.default is not None:
        document["default"] = workflow_input.default
    document["type"] = input_type
    return document


@dataclass(frozen=True)
class GitHubWorkflowDispatch:
    inputs: Iterable[GitHubInput] = ()
    event: ClassVar[str] = "workflow_dispatch"

    def __post_init__(self) -> None:
        if isinstance(self.inputs, (str, bytes)):
            raise TypeError("GitHub workflow inputs must be typed input values")
        inputs = tuple(self.inputs)
        if any(
            not isinstance(workflow_input, (GitHubStringInput, GitHubBooleanInput))
            for workflow_input in inputs
        ):
            raise TypeError("GitHub workflow inputs must be typed input values")
        names = [workflow_input.name for workflow_input in inputs]
        if len(set(names)) != len(names):
            raise ValueError("GitHub workflow inputs must have unique names")
        if len(inputs) > 25:
            raise ValueError("GitHub workflow dispatch supports at most 25 inputs")
        object.__setattr__(self, "inputs", inputs)

    def to_document(self) -> dict[str, object]:
        if not self.inputs:
            return {}
        return {
            "inputs": {
                workflow_input.name: workflow_input.to_document()
                for workflow_input in self.inputs
            }
        }


_CRON_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")  # fmt: skip
_CRON_WEEKDAYS = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")
_CRON_FIELDS = (
    ("minute", 0, 59, ()),
    ("hour", 0, 23, ()),
    ("day of month", 1, 31, ()),
    ("month", 1, 12, _CRON_MONTHS),
    ("day of week", 0, 6, _CRON_WEEKDAYS),
)


def _cron_value(value: str, low: int, high: int, names: tuple[str, ...]) -> int | None:
    if re.fullmatch(r"[0-9]+", value):
        number = int(value)
        return number if low <= number <= high else None
    if value.upper() in names:
        return low + names.index(value.upper())
    return None


def _valid_cron_item(item: str, low: int, high: int, names: tuple[str, ...]) -> bool:
    base, has_step, step = item.partition("/")
    if has_step and not (re.fullmatch(r"[0-9]+", step) and int(step) > 0):
        return False
    if base == "*":
        return True
    start, has_range, end = base.partition("-")
    values = [_cron_value(start, low, high, names)]
    if has_range:
        values.append(_cron_value(end, low, high, names))
    if None in values:
        return False
    return not has_range or values[0] <= values[1]


def _validate_cron(expression: str) -> None:
    if not isinstance(expression, str):
        raise TypeError("GitHub schedule cron must be a string")
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError(f"GitHub schedule cron '{expression}' must have 5 fields")
    for part, (label, low, high, names) in zip(parts, _CRON_FIELDS):
        if not all(
            _valid_cron_item(item, low, high, names) for item in part.split(",")
        ):
            raise ValueError(
                f"GitHub schedule cron '{expression}' has an invalid {label} field"
            )


def _cron_clock(at: time) -> tuple[int, int]:
    if not isinstance(at, time):
        raise TypeError("GitHub cron time must be a datetime.time")
    if at.tzinfo is not None:
        raise ValueError(
            "GitHub cron time must not carry a timezone; "
            "set the GitHubSchedule timezone instead"
        )
    if at.second or at.microsecond:
        raise ValueError("GitHub cron time must use whole minutes")
    return at.minute, at.hour


@dataclass(frozen=True)
class GitHubCron:
    """A validated five-field cron expression; prefer the named constructors."""

    expression: str

    def __post_init__(self) -> None:
        _validate_cron(self.expression)

    @classmethod
    def every(cls, *, minutes: int) -> Self:
        if (
            not isinstance(minutes, int)
            or isinstance(minutes, bool)
            or not 5 <= minutes < 60
            or 60 % minutes
        ):
            raise ValueError(
                "GitHub cron interval must be 5, 6, 10, 12, 15, 20, or 30 minutes"
            )
        return cls(f"*/{minutes} * * * *")

    @classmethod
    def hourly(cls, *, minute: int = 0) -> Self:
        if not isinstance(minute, int) or isinstance(minute, bool):
            raise TypeError("GitHub hourly cron minute must be an integer")
        return cls(f"{minute} * * * *")

    @classmethod
    def daily(cls, *, at: time) -> Self:
        minute, hour = _cron_clock(at)
        return cls(f"{minute} {hour} * * *")

    @classmethod
    def weekdays(cls, *, at: time) -> Self:
        minute, hour = _cron_clock(at)
        return cls(f"{minute} {hour} * * 1-5")

    @classmethod
    def weekly(cls, *days: Day, at: time) -> Self:
        if not days:
            raise ValueError("GitHub weekly cron requires at least one day")
        if any(not isinstance(day, Day) for day in days):
            raise TypeError("GitHub weekly cron days must be calendar.Day values")
        minute, hour = _cron_clock(at)
        # calendar.Day counts from Monday = 0; cron counts from Sunday = 0.
        numbers = sorted({(day + 1) % 7 for day in days})
        return cls(f"{minute} {hour} * * {','.join(map(str, numbers))}")

    @classmethod
    def monthly(cls, *, day: int, at: time) -> Self:
        if not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= 28:
            raise ValueError(
                "GitHub monthly cron day must be from 1 to 28 so it runs every month"
            )
        minute, hour = _cron_clock(at)
        return cls(f"{minute} {hour} {day} * *")


@dataclass(frozen=True)
class GitHubSchedule:
    """Cron schedules that start a workflow from the default branch."""

    crons: Iterable[GitHubCron]
    timezone: GitHubTimezone | None = None
    event: ClassVar[str] = "schedule"

    def __post_init__(self) -> None:
        if isinstance(self.crons, (str, bytes)):
            raise TypeError("GitHub schedule crons must be GitHubCron values")
        crons = tuple(self.crons)
        if not crons:
            raise ValueError("GitHub schedule requires at least one cron")
        if any(not isinstance(cron, GitHubCron) for cron in crons):
            raise TypeError("GitHub schedule crons must be GitHubCron values")
        object.__setattr__(self, "crons", crons)
        if self.timezone is not None and not isinstance(self.timezone, GitHubTimezone):
            raise TypeError("GitHub schedule timezone must be a GitHubTimezone value")

    def to_document(self) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        for cron in self.crons:
            entry = {"cron": cron.expression}
            if self.timezone is not None:
                entry["timezone"] = self.timezone.value
            entries.append(entry)
        return entries


GitHubTrigger = GitHubPush | GitHubPullRequest | GitHubWorkflowDispatch | GitHubSchedule


@dataclass(frozen=True, slots=True)
class _GitHubExpression:
    value: str
    output_source: tuple[str, str, str] | None = None

    def render(self) -> str:
        return f"${{{{ {self.value} }}}}"


@dataclass(frozen=True, slots=True)
class GitHubCheckout:
    """An additional repository checkout required by a task."""

    repository: str
    ref: str
    path: str
    persist_credentials: bool = False
    fetch_depth: int = 1
    submodules: bool = False
    token: SecretRef | None = None

    @property
    def name(self) -> str:
        return f"github/checkout:{self.path}"

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None:
            raise ValueError("repository must use the 'owner/name' form")
        if not self.ref:
            raise ValueError("checkout ref cannot be empty")
        normalized_path = self.path.replace("\\", "/")
        path = Path(normalized_path)
        if (
            not normalized_path
            or path.is_absolute()
            or ".." in path.parts
            or re.match(r"^[A-Za-z]:/", normalized_path)
        ):
            raise ValueError("checkout path must stay inside the workspace")
        if not isinstance(self.fetch_depth, int) or self.fetch_depth < 0:
            raise ValueError("fetch_depth must be a non-negative integer")
        if not isinstance(self.persist_credentials, bool) or not isinstance(
            self.submodules, bool
        ):
            raise TypeError("checkout boolean options must be bool values")
        if self.token is not None and not isinstance(self.token, SecretRef):
            raise TypeError("checkout token must be a SecretRef")


@dataclass(frozen=True, slots=True)
class GitHubWorkflowArtifacts:
    """Artifacts imported from a completed GitHub Actions workflow run."""

    repository: str
    run_id: int | GitHubStringInput
    pattern: str
    revision: str | GitHubStringInput | None = None
    require_success: bool = True
    token: SecretRef | None = None

    @property
    def name(self) -> str:
        run = (
            self.run_id.name
            if isinstance(self.run_id, GitHubStringInput)
            else self.run_id
        )
        return f"github/workflow-artifacts:{self.repository}:{run}:{self.pattern}"

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None:
            raise ValueError("repository must use the 'owner/name' form")
        if not isinstance(self.run_id, GitHubStringInput) and (
            not isinstance(self.run_id, int)
            or isinstance(self.run_id, bool)
            or self.run_id < 1
        ):
            raise ValueError(
                "workflow artifact run_id must be a positive integer or input"
            )
        if (
            not isinstance(self.pattern, str)
            or not self.pattern
            or "\n" in self.pattern
        ):
            raise ValueError(
                "workflow artifact pattern must be a non-empty single line"
            )
        if self.revision is not None and not isinstance(
            self.revision, (str, GitHubStringInput)
        ):
            raise TypeError("workflow artifact revision must be a string or input")
        if isinstance(self.revision, str) and not self.revision:
            raise ValueError("workflow artifact revision cannot be empty")
        if not isinstance(self.require_success, bool):
            raise TypeError("require_success must be a boolean")
        if self.token is not None and not isinstance(self.token, SecretRef):
            raise TypeError("workflow artifact token must be a SecretRef")


@dataclass(frozen=True, slots=True)
class GitHubActionStep:
    """A plugin-provided step backed by an OmniShip-locked action."""

    dependency: str
    name: str
    inputs: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.dependency or not self.name:
            raise ValueError("locked action steps require a dependency and name")
        if (
            "@" in self.dependency
            and re.fullmatch(
                r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*@[0-9a-f]{40}",
                self.dependency,
            )
            is None
        ):
            raise ValueError(
                "Explicit GitHub Action references require a full commit SHA"
            )
        object.__setattr__(self, "inputs", dict(self.inputs))


def _splits_artifacts(placement: object) -> bool:
    """Whether a Build job uploads one handoff artifact per matrix entry."""

    return isinstance(placement, GitHubJob) and (
        placement.matrix is not None or len(placement.runners) > 1
    )


def _render_requirement_value(value: int | str | GitHubStringInput) -> str:
    if isinstance(value, GitHubStringInput):
        return f"${{{{ inputs.{value.name} }}}}"
    return str(value)


def _trigger_filters_document(trigger: object) -> dict[str, object]:
    document: dict[str, object] = {}
    for field_info in fields(trigger):
        values = getattr(trigger, field_info.name)
        if values:
            document[field_info.name.replace("_", "-")] = list(values)
    return document


@dataclass(frozen=True)
class GitHubWorkflow:
    file: str
    name: str
    triggers: Iterable[GitHubTrigger] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.file, str) or not self.file:
            raise TypeError("GitHub workflow file must be a non-empty string")
        if "/" in self.file or "\\" in self.file:
            raise ValueError("GitHub workflow file must be a filename")
        if Path(self.file).suffix not in {".yml", ".yaml"}:
            raise ValueError("GitHub workflow file must end in .yml or .yaml")
        if not isinstance(self.name, str) or not self.name:
            raise TypeError("GitHub workflow name must be a non-empty string")
        if self.triggers is None:
            return
        if isinstance(self.triggers, (str, bytes)):
            raise TypeError("GitHub workflow triggers must be typed trigger values")
        triggers = tuple(self.triggers)
        if any(
            not isinstance(
                trigger,
                (
                    GitHubPush,
                    GitHubPullRequest,
                    GitHubWorkflowDispatch,
                    GitHubSchedule,
                ),
            )
            for trigger in triggers
        ):
            raise TypeError("GitHub workflow triggers must be typed trigger values")
        events = [trigger.event for trigger in triggers]
        if len(set(events)) != len(events):
            raise ValueError("GitHub workflow cannot contain duplicate trigger types")
        if any(isinstance(trigger, GitHubSchedule) for trigger in triggers) and any(
            isinstance(trigger, GitHubWorkflowDispatch) and trigger.inputs
            for trigger in triggers
        ):
            raise ValueError(
                "GitHub schedule cannot be combined with workflow dispatch inputs; "
                "scheduled runs receive no inputs, not even defaults"
            )
        object.__setattr__(self, "triggers", triggers)


@dataclass(frozen=True)
class GitHubActions:
    name: ClassVar[str] = "github/actions"
    bootstrap: GitHubBootstrap = GitHubBootstrap.ISOLATED
    default_runner: GitHubRunner = GitHubRunner.UBUNTU_LATEST
    default_permissions: GitHubPermissions = GitHubPermissions(
        contents=GitHubPermission.READ
    )
    check: GitHubWorkflow = GitHubWorkflow(file="check.yml", name="Check")
    build: GitHubWorkflow = GitHubWorkflow(file="build.yml", name="Build")
    ship: GitHubWorkflow = GitHubWorkflow(file="ship.yml", name="Ship")

    def __post_init__(self) -> None:
        if not isinstance(self.bootstrap, GitHubBootstrap):
            raise TypeError("bootstrap must be a GitHubBootstrap value")
        if not isinstance(self.default_runner, GitHubRunner):
            raise TypeError("default_runner must be a GitHubRunner value")
        if not isinstance(self.default_permissions, GitHubPermissions):
            raise TypeError("default_permissions must be a GitHubPermissions value")
        workflows = (self.check, self.build, self.ship)
        if any(not isinstance(workflow, GitHubWorkflow) for workflow in workflows):
            raise TypeError("check, build, and ship must be GitHubWorkflow values")
        filenames = [workflow.file.casefold() for workflow in workflows]
        if len(set(filenames)) != len(filenames):
            raise ValueError("GitHub workflow files must be unique")
        if any(
            isinstance(trigger, GitHubSchedule) for trigger in self.ship.triggers or ()
        ):
            raise ValueError(
                "The Ship workflow cannot use a schedule; scheduled runs would "
                "republish the same release version"
            )

    @property
    def default_job(self) -> GitHubJob:
        return GitHubJob((self.default_runner,))

    @property
    def runner_os(self) -> _GitHubExpression:
        """Reference the operating system of the current GitHub runner."""

        return _GitHubExpression("runner.os")

    @property
    def runner_arch(self) -> _GitHubExpression:
        """Reference the architecture of the current GitHub runner."""

        return _GitHubExpression("runner.arch")

    @staticmethod
    def matrix(name: str) -> _GitHubExpression:
        """Reference an axis of the current task's GitHub matrix."""

        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name) is None:
            raise ValueError("GitHub matrix axis name must be an identifier")
        return _GitHubExpression(f"matrix.{name}")

    @staticmethod
    def hash_files(*patterns: str) -> _GitHubExpression:
        """Create a GitHub ``hashFiles`` expression for cache invalidation."""

        if not patterns or any(
            not isinstance(pattern, str) or not pattern for pattern in patterns
        ):
            raise ValueError("hash_files requires at least one non-empty pattern")
        arguments = ", ".join(f"'{pattern.replace("'", "''")}'" for pattern in patterns)
        return _GitHubExpression(f"hashFiles({arguments})")

    @staticmethod
    def secret(name: str) -> SecretRef:
        """Reference a GitHub repository or environment secret by name."""

        return SecretRef(name)

    @staticmethod
    def checkout(
        *,
        repository: str,
        ref: str,
        path: str,
        persist_credentials: bool = False,
        fetch_depth: int = 1,
        submodules: bool = False,
        token: SecretRef | None = None,
    ) -> GitHubCheckout:
        """Declare an additional source checkout required by a task."""

        return GitHubCheckout(
            repository=repository,
            ref=ref,
            path=path,
            persist_credentials=persist_credentials,
            fetch_depth=fetch_depth,
            submodules=submodules,
            token=token,
        )

    @staticmethod
    def workflow_artifacts(
        *,
        repository: str,
        run_id: int | GitHubStringInput,
        pattern: str,
        revision: str | GitHubStringInput | None = None,
        require_success: bool = True,
        token: SecretRef | None = None,
    ) -> GitHubWorkflowArtifacts:
        """Import validated OmniShip bundles from an earlier workflow run."""

        return GitHubWorkflowArtifacts(
            repository=repository,
            run_id=run_id,
            pattern=pattern,
            revision=revision,
            require_success=require_success,
            token=token,
        )

    @staticmethod
    def cache(
        *,
        paths: Iterable[str],
        key: str | Iterable[str | _GitHubExpression],
        restore_prefixes: Iterable[str] = (),
    ) -> CacheSpec:
        """Build an Actions cache declaration from typed key parts."""

        if isinstance(key, str):
            rendered_key = key
        else:
            parts = tuple(key)
            if not parts or any(
                not isinstance(part, (str, _GitHubExpression)) for part in parts
            ):
                raise TypeError("cache key parts must be strings or GitHub expressions")
            rendered_key = "-".join(
                part.render() if isinstance(part, _GitHubExpression) else part
                for part in parts
            )
        return CacheSpec(
            paths=paths,
            key=rendered_key,
            restore_keys=restore_prefixes,
        )

    def job(
        self,
        *,
        runners: Iterable[GitHubRunner] | None = None,
        fail_fast: bool = False,
        max_parallel: int | None = None,
        permissions: GitHubPermissions | None = None,
        timeout_minutes: int | None = None,
        environment: str | None = None,
        working_directory: str | None = None,
        shell: GitHubShell | None = None,
        env: Mapping[str, str | SecretRef | _GitHubExpression] | None = None,
        caches: Iterable[CacheSpec] = (),
        matrix: GitHubMatrix | None = None,
        container: GitHubContainer | None = None,
        services: Mapping[str, GitHubContainer] | None = None,
        before_steps: Iterable[GitHubActionStep] = (),
        after_steps: Iterable[GitHubActionStep] = (),
        outputs: Iterable[str] = (),
        environment_url: str | None = None,
    ) -> GitHubJob:
        selected_runners = (self.default_runner,) if runners is None else tuple(runners)
        return GitHubJob(
            selected_runners,
            fail_fast=fail_fast,
            max_parallel=max_parallel,
            permissions=permissions,
            timeout_minutes=timeout_minutes,
            environment=environment,
            working_directory=working_directory,
            shell=shell,
            env={} if env is None else env,
            caches=tuple(caches),
            matrix=matrix,
            container=container,
            services={} if services is None else services,
            before_steps=tuple(before_steps),
            after_steps=tuple(after_steps),
            outputs=tuple(outputs),
            environment_url=environment_url,
        )

    @staticmethod
    def output(stage: Stage | str, node: str, name: str) -> _GitHubExpression:
        """Reference a declared output of a task in the same stage."""

        stage_name = stage.value if isinstance(stage, Stage) else stage
        if stage_name not in {item.value for item in Stage}:
            raise ValueError("GitHub output stage must be check, build, or ship")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
            raise ValueError("GitHub output name must be an identifier")
        normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", node).strip("-").lower()
        if not normalized:
            raise ValueError("GitHub output node cannot be empty")
        return _GitHubExpression(
            f"needs.{stage_name}-{normalized}.outputs.{name}",
            output_source=(stage_name, node, name),
        )


class GitHubActionsGenerator:
    name = "github/actions"

    def __init__(self, registry: PluginRegistry) -> None:
        self.registry = registry

    def generate(
        self,
        config: OmniShipConfig,
        source_path: Path,
        config_path: Path,
        pipeline: Pipeline,
    ) -> tuple[GeneratedFile, ...]:
        targets = [
            target for target in pipeline.targets if isinstance(target, GitHubActions)
        ]
        if len(targets) > 1:
            raise ValueError("Pipeline has more than one GitHub Actions target")
        github_nodes = tuple(
            node
            for node in config.ship.values()
            if node.uses in {"github/pages", "github/release", "github/tag"}
        )
        if not targets and not github_nodes:
            return ()
        actions = targets[0] if targets else GitHubActions()
        pages_nodes = [
            node for node in config.ship.values() if node.uses == "github/pages"
        ]
        if len(pages_nodes) > 1:
            raise ValueError("A pipeline can contain only one GitHub Pages deployment")

        workspace_root = source_path.parent.resolve()
        lock_path = workspace_root / LOCK_FILENAME
        action_defaults = GitHubActionLock.defaults()
        action_lock = (
            GitHubActionLock.load(lock_path) if lock_path.is_file() else action_defaults
        )
        action_lock.validate_compatibility(action_defaults)
        locked_plugins = {plugin.name: plugin for plugin in action_lock.plugins}
        resolved_plugins = []
        for plugin in pipeline.plugins:
            if (
                isinstance(plugin, GitPluginPackage)
                and plugin.version is not None
                and plugin.commit is None
            ):
                locked = locked_plugins.get(plugin.name)
                if (
                    isinstance(locked, GitPluginPackage)
                    and locked.repository == plugin.repository
                    and locked.version == plugin.version
                    and locked.commit is not None
                ):
                    plugin = locked
                else:
                    plugin = resolve_git_plugin(plugin)
            resolved_plugins.append(plugin)
        action_lock = action_lock.with_plugins(tuple(resolved_plugins))
        source = source_path.resolve().relative_to(workspace_root)
        pipeline_config = config_path.resolve().relative_to(workspace_root)
        workflow_root = workspace_root / ".github" / "workflows"
        if actions.bootstrap == GitHubBootstrap.WORKSPACE:
            omniship_command = ["uv", "run", "omniship"]
            plugin_index_env: dict[str, str] = {}
        else:
            omniship_command = [
                "uvx",
                "--from",
                f"omniship=={action_lock.omniship_version}",
            ]
            plugin_index_env = {}
            plugin_indexes = {}
            for plugin in action_lock.plugins:
                index = getattr(plugin, "index", None)
                if index is not None:
                    previous = plugin_indexes.get(index.name)
                    if previous is not None and previous != index:
                        raise ValueError(
                            f"Conflicting plugin index definitions: {index.name}"
                        )
                    plugin_indexes[index.name] = index
            for index in plugin_indexes.values():
                omniship_command.extend(["--index", f"{index.name}={index.url}"])
                prefix = index.env_prefix
                if f"{prefix}_PASSWORD" in plugin_index_env:
                    raise ValueError(
                        f"Plugin index environment name collision: {index.name}"
                    )
                plugin_index_env[f"{prefix}_USERNAME"] = index.username
                plugin_index_env[f"{prefix}_PASSWORD"] = (
                    f"${{{{ secrets.{index.password.name} }}}}"
                )
            for plugin in action_lock.plugins:
                omniship_command.extend(["--with", plugin.requirement])
            omniship_command.append("omniship")
        generate_command = [*omniship_command, "generate"]
        if source != Path("workflow.py"):
            generate_command.extend(["--workflow-file", source.as_posix()])
        if pipeline_config != Path("omniship.yaml"):
            generate_command.extend(["--output", pipeline_config.as_posix()])
        generate_command.append("--check")

        def setup_steps() -> list[dict[str, object]]:
            steps: list[dict[str, object]] = [
                {"uses": action_lock.reference("checkout")},
                {"uses": action_lock.reference("setup-uv")},
            ]
            if actions.bootstrap == GitHubBootstrap.WORKSPACE:
                steps.append({"run": "uv sync --all-groups --locked"})
            return steps

        def prepare_job() -> dict[str, object]:
            generate_step: dict[str, object] = {"run": shlex.join(generate_command)}
            if plugin_index_env:
                generate_step["env"] = dict(plugin_index_env)
            return {
                "name": "Check · Prepare",
                "runs-on": actions.default_runner.value,
                "steps": [
                    *setup_steps(),
                    generate_step,
                ],
            }

        def stage_jobs(
            stage: Stage,
            nodes: dict[str, NodeConfig],
            *,
            root_dependency: str,
            has_runtime_inputs: bool,
        ) -> dict[str, dict[str, object]]:
            job_ids: dict[str, str] = {}
            used_job_ids: set[str] = set()
            for node_name in nodes:
                normalized = (
                    re.sub(r"[^a-zA-Z0-9_-]+", "-", node_name).strip("-").lower()
                )
                job_id = f"{stage.value}-{normalized}"
                if job_id in used_job_ids:
                    raise ValueError(
                        f"GitHub Actions job id collision for node "
                        f"'{node_name}': {job_id}"
                    )
                used_job_ids.add(job_id)
                job_ids[node_name] = job_id

            depended_on = {
                dependency for node in nodes.values() for dependency in node.needs
            }
            generated_jobs: dict[str, dict[str, object]] = {}
            terminal_job_ids: list[str] = []
            for node_name, node in nodes.items():
                job_id = job_ids[node_name]
                dependency_jobs = [job_ids[dependency] for dependency in node.needs]
                if not dependency_jobs:
                    dependency_jobs = [root_dependency]
                if node.uses == "github/external-workflow":
                    if node.execution is not None or node.requirements:
                        raise ValueError(
                            f"External workflow '{node_name}' cannot have runner placement "
                            "or OmniShip requirements"
                        )
                    job: dict[str, object] = {
                        "name": f"{stage.value.title()} · {_display_name(node_name)}",
                        "needs": dependency_jobs[0]
                        if len(dependency_jobs) == 1
                        else dependency_jobs,
                        "uses": node.with_["uses"],
                    }
                    if node.with_.get("inputs"):
                        job["with"] = node.with_["inputs"]
                    if node.with_.get("secrets"):
                        job["secrets"] = {
                            name: f"${{{{ secrets.{secret} }}}}"
                            for name, secret in node.with_["secrets"].items()
                        }
                    if node.with_.get("permissions"):
                        job["permissions"] = node.with_["permissions"]
                    generated_jobs[job_id] = job
                    if node_name not in depended_on:
                        terminal_job_ids.append(job_id)
                    continue
                placement = node.execution or actions.default_job
                if not isinstance(placement, GitHubJob):
                    raise ValueError(
                        f"Node '{node_name}' has execution metadata that is not a GitHub job"
                    )
                if node.uses == "github/pages" and len(placement.runners) != 1:
                    raise ValueError(
                        "GitHub Pages deployment requires exactly one runner"
                    )
                references = list(placement.env.values())
                for action in (*placement.before_steps, *placement.after_steps):
                    references.extend(action.inputs.values())
                for reference in references:
                    if (
                        not isinstance(reference, _GitHubExpression)
                        or reference.output_source is None
                    ):
                        continue
                    source_stage, source_node, output_name = reference.output_source
                    producer = nodes.get(source_node)
                    if source_stage != stage.value or source_node not in node.needs:
                        raise ValueError(
                            f"Node '{node_name}' must depend on output producer '{source_node}'"
                        )
                    producer_job = producer.execution or actions.default_job
                    if (
                        not isinstance(producer_job, GitHubJob)
                        or output_name not in producer_job.outputs
                    ):
                        raise ValueError(
                            f"Node '{source_node}' does not declare output '{output_name}'"
                        )

                resolved_requirements = self.registry.resolve_requirements(
                    self.name,
                    node.requirements,
                )
                workflow_artifacts = tuple(
                    requirement
                    for requirement in node.requirements
                    if isinstance(requirement, GitHubWorkflowArtifacts)
                )
                required_permissions = GitHubPermissions()
                if workflow_artifacts:
                    required_permissions = required_permissions.with_minimum(
                        GitHubPermissions(
                            actions=GitHubPermission.READ,
                            contents=GitHubPermission.READ,
                        ),
                        node_name=node_name,
                    )
                for resolved in resolved_requirements:
                    if isinstance(resolved, GitHubPermissions):
                        required_permissions = required_permissions.with_minimum(
                            resolved,
                            node_name=node_name,
                        )
                if node.uses in {"github/release", "github/tag"} and not node.with_.get(
                    "dry_run", False
                ):
                    required_permissions = required_permissions.with_minimum(
                        GitHubPermissions(contents=GitHubPermission.WRITE),
                        node_name=node_name,
                    )
                elif node.uses == "github/pages":
                    required_permissions = required_permissions.with_minimum(
                        GitHubPermissions(
                            actions=GitHubPermission.READ,
                            contents=GitHubPermission.READ,
                            id_token=GitHubPermission.WRITE,
                            pages=GitHubPermission.WRITE,
                        ),
                        node_name=node_name,
                    )
                job_permissions = placement.permissions
                if job_permissions is None:
                    if not actions.default_permissions.satisfies(required_permissions):
                        job_permissions = GitHubPermissions().with_minimum(
                            required_permissions,
                            node_name=node_name,
                        )
                else:
                    job_permissions = job_permissions.with_minimum(
                        required_permissions,
                        node_name=node_name,
                    )

                dependency_jobs = [job_ids[dependency] for dependency in node.needs]
                if not dependency_jobs:
                    dependency_jobs = [root_dependency]

                steps = setup_steps()

                def action_step(value: GitHubActionStep) -> dict[str, object]:
                    step: dict[str, object] = {
                        "name": value.name,
                        "uses": (
                            value.dependency
                            if "@" in value.dependency
                            else action_lock.reference(value.dependency)
                        ),
                    }
                    if value.inputs:
                        step["with"] = {
                            key: (
                                item.render()
                                if isinstance(item, _GitHubExpression)
                                else f"${{{{ secrets.{item.name} }}}}"
                                if isinstance(item, SecretRef)
                                else item
                            )
                            for key, item in value.inputs.items()
                        }
                    return step

                imports_artifacts = False
                for requirement_index, resolved in enumerate(
                    resolved_requirements,
                    start=1,
                ):
                    if isinstance(resolved, GitHubPermissions):
                        continue
                    if isinstance(resolved, GitHubActionStep):
                        steps.append(action_step(resolved))
                        continue
                    if isinstance(resolved, GitHubWorkflowArtifacts):
                        run_id = _render_requirement_value(resolved.run_id)
                        revision = (
                            _render_requirement_value(resolved.revision)
                            if resolved.revision is not None
                            else ""
                        )
                        token = (
                            f"${{{{ secrets.{resolved.token.name} }}}}"
                            if resolved.token is not None
                            else "${{ github.token }}"
                        )
                        success_check = (
                            'if [ "$conclusion" != "success" ]; then\n'
                            '  echo "Source workflow did not succeed: '
                            '$conclusion" >&2\n'
                            "  exit 1\n"
                            "fi\n"
                            if resolved.require_success
                            else ""
                        )
                        steps.append(
                            {
                                "name": f"Review artifacts from run {run_id}",
                                "shell": "bash",
                                "env": {
                                    "GH_TOKEN": token,
                                    "OMNISHIP_REPOSITORY": resolved.repository,
                                    "OMNISHIP_RUN_ID": run_id,
                                    "OMNISHIP_EXPECTED_REVISION": revision,
                                },
                                "run": (
                                    "set -euo pipefail\n"
                                    'endpoint="repos/$OMNISHIP_REPOSITORY/'
                                    'actions/runs/$OMNISHIP_RUN_ID"\n'
                                    'conclusion="$(gh api "$endpoint" '
                                    '--jq .conclusion)"\n'
                                    'head_sha="$(gh api "$endpoint" '
                                    '--jq .head_sha)"\n'
                                    f"{success_check}"
                                    'if [ -n "$OMNISHIP_EXPECTED_REVISION" ] && '
                                    '[ "$head_sha" != '
                                    '"$OMNISHIP_EXPECTED_REVISION" ]; then\n'
                                    '  echo "Source workflow revision mismatch" >&2\n'
                                    "  exit 1\n"
                                    "fi\n"
                                    'echo "OMNISHIP_EXPECTED_ARTIFACT_REVISION='
                                    '$head_sha" >> "$GITHUB_ENV"\n'
                                ),
                            }
                        )
                        steps.append(
                            {
                                "name": f"Download artifacts from run {run_id}",
                                "uses": action_lock.reference("download-artifact"),
                                "with": {
                                    "pattern": resolved.pattern,
                                    "path": (
                                        f"{ARTIFACT_IMPORT_PATH}/external-"
                                        f"{requirement_index}"
                                    ),
                                    "github-token": token,
                                    "repository": resolved.repository,
                                    "run-id": run_id,
                                },
                            }
                        )
                        imports_artifacts = True
                        continue
                    if isinstance(resolved, GitHubCheckout):
                        checkout_inputs: dict[str, object] = {
                            "repository": resolved.repository,
                            "ref": resolved.ref,
                            "path": resolved.path,
                            "persist-credentials": resolved.persist_credentials,
                            "fetch-depth": resolved.fetch_depth,
                            "submodules": resolved.submodules,
                        }
                        if resolved.token is not None:
                            checkout_inputs["token"] = (
                                f"${{{{ secrets.{resolved.token.name} }}}}"
                            )
                        steps.append(
                            {
                                "name": f"Check out {resolved.repository}",
                                "uses": action_lock.reference("checkout"),
                                "with": checkout_inputs,
                            }
                        )
                        continue
                    if isinstance(resolved, (tuple, list)):
                        if any(not isinstance(step, Mapping) for step in resolved):
                            raise TypeError(
                                f"Requirement resolver for node '{node_name}' must "
                                "return GitHub Actions step mappings"
                            )
                        steps.extend(dict(step) for step in resolved)
                        continue
                    if not isinstance(resolved, Mapping):
                        raise TypeError(
                            f"Requirement resolver for node '{node_name}' must return "
                            "a GitHub Actions step mapping"
                        )
                    steps.append(dict(resolved))
                for cache_index, cache in enumerate(placement.caches, start=1):
                    cache_inputs = {
                        "path": "\n".join(cache.paths),
                        "key": cache.key,
                    }
                    if cache.restore_keys:
                        cache_inputs["restore-keys"] = "\n".join(cache.restore_keys)
                    steps.append(
                        {
                            "name": f"Restore cache {cache_index}",
                            "uses": action_lock.reference("cache"),
                            "with": cache_inputs,
                        }
                    )
                steps.extend(action_step(value) for value in placement.before_steps)
                command = [
                    *omniship_command,
                    "run-node",
                    "--stage",
                    stage.value,
                    "--node",
                    node_name,
                    "--config",
                    pipeline_config.as_posix(),
                ]

                if stage == Stage.BUILD and node.needs:
                    for dependency in node.needs:
                        if nodes[dependency].uses == "github/external-workflow":
                            continue
                        dependency_job = job_ids[dependency]
                        # Exact names: a prefix glob would also match jobs such
                        # as build-app-docs when depending on build-app.
                        dependency_pattern = f"omniship-build-{dependency_job}"
                        if _splits_artifacts(
                            nodes[dependency].execution or actions.default_job
                        ):
                            dependency_pattern += ".*"
                        steps.append(
                            {
                                "uses": action_lock.reference("download-artifact"),
                                "with": {
                                    "pattern": dependency_pattern,
                                    "path": f"{ARTIFACT_IMPORT_PATH}/{dependency_job}",
                                },
                            }
                        )
                    imports_artifacts = True

                if stage == Stage.SHIP and any(
                    build_node.uses != "github/external-workflow"
                    for build_node in config.build.values()
                ):
                    steps.append(
                        {
                            "uses": action_lock.reference("download-artifact"),
                            "with": {
                                "pattern": "omniship-build-*",
                                "path": ARTIFACT_IMPORT_PATH,
                            },
                        }
                    )
                    imports_artifacts = True

                if imports_artifacts:
                    command.extend(["--import-artifacts-root", ARTIFACT_IMPORT_PATH])

                if stage == Stage.BUILD:
                    export_path = f"{ARTIFACT_PATH}/{job_id}"
                    command.extend(["--export-artifacts", export_path])

                if placement.working_directory is not None:
                    command.extend(["--working-directory", placement.working_directory])

                run_step: dict[str, object] = {
                    "name": f"Run {_display_name(node_name)}",
                    "run": shlex.join(command),
                }
                if placement.outputs:
                    run_step["id"] = "omniship"
                if placement.shell is not None:
                    run_step["shell"] = placement.shell.value
                step_env = {
                    name: (
                        f"${{{{ secrets.{value.name} }}}}"
                        if isinstance(value, SecretRef)
                        else value.render()
                        if isinstance(value, _GitHubExpression)
                        else value
                    )
                    for name, value in placement.env.items()
                }
                for name, value in plugin_index_env.items():
                    if name in step_env:
                        raise ValueError(
                            f"Job environment conflicts with plugin index credential: {name}"
                        )
                    step_env[name] = value
                if stage in {Stage.BUILD, Stage.SHIP}:
                    step_env["OMNISHIP_REVISION"] = "${{ github.sha }}"
                if has_runtime_inputs:
                    step_env["OMNISHIP_INPUTS"] = "${{ toJSON(inputs) }}"
                if (
                    node.uses in {"github/release", "github/tag"}
                    or placement.permissions is not None
                ):
                    step_env["GITHUB_TOKEN"] = "${{ github.token }}"
                if placement.outputs:
                    step_env["OMNISHIP_OUTPUT"] = "$GITHUB_OUTPUT"
                if step_env:
                    run_step["env"] = step_env
                steps.append(run_step)
                steps.extend(action_step(value) for value in placement.after_steps)

                if node.uses == "github/pages":
                    steps.extend(
                        [
                            {
                                "uses": action_lock.reference("upload-pages-artifact"),
                                "with": {"path": PAGES_STAGING_PATH.as_posix()},
                            },
                            {
                                "name": "Deploy GitHub Pages",
                                "id": "deployment",
                                "uses": action_lock.reference("deploy-pages"),
                            },
                        ]
                    )

                if stage == Stage.BUILD:
                    # "." cannot occur in job ids, so it separates the suffix.
                    artifact_name = f"omniship-build-{job_id}"
                    if placement.matrix is not None:
                        artifact_name += ".${{ strategy.job-index }}"
                    elif len(placement.runners) > 1:
                        artifact_name += ".${{ matrix.runner }}"
                    steps.append(
                        {
                            "uses": action_lock.reference("upload-artifact"),
                            "with": {
                                "name": artifact_name,
                                "path": f"{ARTIFACT_PATH}/{job_id}",
                                "include-hidden-files": True,
                            },
                        }
                    )

                job: dict[str, object] = {
                    "name": f"{stage.value.title()} · {_display_name(node_name)}",
                    "needs": dependency_jobs[0]
                    if len(dependency_jobs) == 1
                    else dependency_jobs,
                }
                if placement.timeout_minutes is not None:
                    job["timeout-minutes"] = placement.timeout_minutes
                if placement.outputs:
                    job["outputs"] = {
                        name: f"${{{{ steps.omniship.outputs.{name} }}}}"
                        for name in placement.outputs
                    }
                if job_permissions is not None:
                    job["permissions"] = job_permissions.to_document()
                if node.uses == "github/pages":
                    job["environment"] = {
                        "name": "github-pages",
                        "url": "${{ steps.deployment.outputs.page_url }}",
                    }
                elif placement.environment is not None:
                    job["environment"] = (
                        {
                            "name": placement.environment,
                            "url": placement.environment_url,
                        }
                        if placement.environment_url is not None
                        else placement.environment
                    )
                if placement.container is not None:
                    job["container"] = placement.container.to_document()
                if placement.services:
                    job["services"] = {
                        name: service.to_document()
                        for name, service in placement.services.items()
                    }
                job["runs-on"] = placement.runners[0].value
                job["steps"] = steps
                if len(placement.runners) > 1 or placement.matrix is not None:
                    matrix: dict[str, object] = {
                        "runner": [runner.value for runner in placement.runners]
                    }
                    if placement.matrix is not None:
                        matrix.update(
                            {
                                name: list(values)
                                for name, values in placement.matrix.axes.items()
                            }
                        )
                        if placement.matrix.exclude:
                            matrix["exclude"] = list(placement.matrix.exclude)
                        if placement.matrix.include:
                            matrix["include"] = list(placement.matrix.include)
                    job["strategy"] = {
                        "fail-fast": placement.fail_fast,
                        "matrix": matrix,
                    }
                    if placement.max_parallel is not None:
                        job["strategy"]["max-parallel"] = placement.max_parallel
                    job["runs-on"] = "${{ matrix.runner }}"
                generated_jobs[job_id] = job
                if node_name not in depended_on:
                    terminal_job_ids.append(job_id)

            barrier_id = f"{stage.value}-complete"
            barrier_needs = terminal_job_ids or [root_dependency]
            generated_jobs[barrier_id] = {
                "name": f"{stage.value.title()} complete",
                "needs": barrier_needs[0] if len(barrier_needs) == 1 else barrier_needs,
                "runs-on": actions.default_runner.value,
                "steps": [{"run": f"echo '{stage.value} stage complete'"}],
            }
            return generated_jobs

        def workflow_events(
            workflow: GitHubWorkflow,
            defaults: tuple[GitHubTrigger, ...],
            *,
            is_callable: bool,
        ) -> dict[str, object]:
            configured = defaults if workflow.triggers is None else workflow.triggers
            events = {trigger.event: trigger.to_document() for trigger in configured}
            if is_callable:
                events["workflow_call"] = {}
            return events

        def has_runtime_inputs(
            workflow: GitHubWorkflow,
            defaults: tuple[GitHubTrigger, ...],
        ) -> bool:
            configured = defaults if workflow.triggers is None else workflow.triggers
            return any(
                isinstance(trigger, GitHubWorkflowDispatch) and bool(trigger.inputs)
                for trigger in configured
            )

        check_jobs = {"prepare": prepare_job()}
        check_jobs.update(
            stage_jobs(
                Stage.CHECK,
                config.check,
                root_dependency="prepare",
                has_runtime_inputs=has_runtime_inputs(
                    actions.check,
                    (GitHubPullRequest(), GitHubPush(branches=("main",))),
                ),
            )
        )
        build_jobs: dict[str, dict[str, object]] = {
            "check": {
                "name": actions.check.name,
                "uses": f"./.github/workflows/{actions.check.file}",
            }
        }
        build_jobs.update(
            stage_jobs(
                Stage.BUILD,
                config.build,
                root_dependency="check",
                has_runtime_inputs=has_runtime_inputs(actions.build, ()),
            )
        )
        ship_jobs: dict[str, dict[str, object]] = {
            "build": {
                "name": actions.build.name,
                "uses": f"./.github/workflows/{actions.build.file}",
            },
        }
        ship_jobs.update(
            stage_jobs(
                Stage.SHIP,
                config.ship,
                root_dependency="build",
                has_runtime_inputs=has_runtime_inputs(
                    actions.ship,
                    (GitHubPush(tags=("v*",)),),
                ),
            )
        )

        ship_document: dict[str, object] = {
            "name": actions.ship.name,
            "on": workflow_events(
                actions.ship,
                (GitHubPush(tags=("v*",)),),
                is_callable=False,
            ),
            "permissions": actions.default_permissions.to_document(),
            "jobs": ship_jobs,
        }
        if pages_nodes:
            ship_document["concurrency"] = {
                "group": "pages",
                "cancel-in-progress": True,
            }

        documents = (
            (
                actions.check,
                {
                    "name": actions.check.name,
                    "on": workflow_events(
                        actions.check,
                        (
                            GitHubPullRequest(),
                            GitHubPush(branches=("main",)),
                        ),
                        is_callable=True,
                    ),
                    "permissions": actions.default_permissions.to_document(),
                    "jobs": check_jobs,
                },
            ),
            (
                actions.build,
                {
                    "name": actions.build.name,
                    "on": workflow_events(
                        actions.build,
                        (),
                        is_callable=True,
                    ),
                    "permissions": actions.default_permissions.to_document(),
                    "jobs": build_jobs,
                },
            ),
            (
                actions.ship,
                ship_document,
            ),
        )
        generated = [GeneratedFile(lock_path, action_lock.render())]
        for workflow, document in documents:
            rendered = yaml.safe_dump(
                document,
                sort_keys=False,
                width=1000,
                allow_unicode=True,
            )
            content = (
                f"# Generated by OmniShip from {source.name}. Do not edit directly.\n"
                f"{rendered}"
            )
            generated.append(GeneratedFile(workflow_root / workflow.file, content))
        return tuple(generated)
