import tomllib
from pathlib import Path

import pytest

from omniship.operations import register_core_plugin
from omniship.plugins.bun import register_bun_plugin
from omniship.plugins.github import (
    GitHub,
    GitHubPages,
    GitHubRelease,
    GitHubTag,
    register_github_plugin,
)
from omniship.plugins.go import register_go_plugin
from omniship.plugins.node import register_node_plugin
from omniship.plugins.packaging import (
    Archive,
    register_packaging_plugin,
)
from omniship.plugins.python import Python, Ruff, register_python_plugin
from omniship.plugins.registry import PluginRegistry
from omniship.plugins.rust import register_rust_plugin
from omniship.plugins.system import SystemPackages
from omniship.plugins.zig import register_zig_plugin
from omniship.runtime import TaskContext


def _operation_names(registry: PluginRegistry) -> set[str]:
    return {definition.name for definition in registry.list_operations()}


def test_official_plugins_have_independent_registration_boundaries() -> None:
    core = PluginRegistry()
    register_core_plugin(core)
    assert _operation_names(core) == {
        "core/command",
        "core/noop",
        "core/python",
    }

    python = PluginRegistry()
    register_python_plugin(python)
    assert _operation_names(python) == {
        "python/ruff",
        "python/pytest",
        "python/pypi-publish",
        "python/wheel",
    }

    github = PluginRegistry()
    register_github_plugin(github)
    assert _operation_names(github) == {
        "github/external-workflow",
        "github/pages",
        "github/release",
        "github/tag",
    }

    packaging = PluginRegistry()
    register_packaging_plugin(packaging)
    assert _operation_names(packaging) == {
        "packaging/sha256-manifest",
        "packaging/tar-gz",
        "packaging/zip",
    }

    expected = {
        register_node_plugin: {
            "node/install",
            "node/test",
            "node/build",
            "node/npm-publish",
        },
        register_bun_plugin: {"bun/install", "bun/test", "bun/build"},
        register_go_plugin: {
            "go/fmt",
            "go/mod-download",
            "go/vet",
            "go/test",
            "go/build",
        },
        register_rust_plugin: {
            "rust/cargo-test",
            "rust/cargo-build",
            "rust/cargo-publish",
        },
        register_zig_plugin: {"zig/test", "zig/build"},
    }
    for register, operations in expected.items():
        registry = PluginRegistry()
        register(registry)
        assert _operation_names(registry) == operations


def test_public_types_are_owned_by_their_provider_packages() -> None:
    assert Ruff.__module__.startswith("omniship.plugins.python")
    assert Python.__module__.startswith("omniship.plugins.python")
    assert GitHub.__module__.startswith("omniship.plugins.github")
    assert GitHubPages.__module__.startswith("omniship.plugins.github")
    assert GitHubRelease.__module__.startswith("omniship.plugins.github")
    assert GitHubTag.__module__.startswith("omniship.plugins.github")
    assert Archive.__module__.startswith("omniship.plugins.packaging")


def test_package_entry_points_discover_official_plugins_separately() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["entry-points"]["omniship.plugins"] == {
        "core": "omniship.operations:register_core_plugin",
        "python": "omniship.plugins.python:register_python_plugin",
        "github": "omniship.plugins.github:register_github_plugin",
        "system": "omniship.plugins.system:register_system_plugin",
        "packaging": "omniship.plugins.packaging:register_packaging_plugin",
        "node": "omniship.plugins.node:register_node_plugin",
        "napi": "omniship.plugins.napi:register_napi_plugin",
        "maturin": "omniship.plugins.maturin:register_maturin_plugin",
        "homebrew": "omniship.plugins.homebrew:register_homebrew_plugin",
        "vercel": "omniship.plugins.vercel:register_vercel_plugin",
        "cloudflare": "omniship.plugins.cloudflare:register_cloudflare_plugin",
        "bun": "omniship.plugins.bun:register_bun_plugin",
        "go": "omniship.plugins.go:register_go_plugin",
        "rust": "omniship.plugins.rust:register_rust_plugin",
        "zig": "omniship.plugins.zig:register_zig_plugin",
    }


def test_python_imperative_capabilities_are_provider_owned(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    context = TaskContext(tmp_path, {})

    assert not hasattr(context, "python")
    assert not hasattr(context, "project")
    assert Python(context).project.version() == "1.2.3"


def test_system_packages_are_typed_and_os_specific() -> None:
    packages = SystemPackages(
        ubuntu=["zlib1g-dev"],
        macos=["zlib"],
        windows=["zlib"],
    )

    assert packages.name == "system/packages"
    assert packages.ubuntu == ("zlib1g-dev",)

    with pytest.raises(ValueError, match="package name"):
        SystemPackages(ubuntu=["zlib1g-dev; whoami"])
