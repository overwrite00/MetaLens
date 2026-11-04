"""Registry of synthetic sample files generated at runtime (no binaries in the repo).

Every format phase owns exactly one module of this package and only appends to
that module's ``SAMPLES`` list, so parallel phases never touch the same file.
``ALL`` concatenates them in a fixed order; ``tests/test_contract.py`` runs the
generic contract (plan B/E) on every entry.

Registering a sample (inside e.g. ``tests/factories/image.py``)::

    from pathlib import Path
    from . import registrar

    SAMPLES = []
    sample = registrar(SAMPLES, __name__)

    @sample("jpeg_camera", volatile=("img:*",), mutations={"exif:Orientation": 6})
    def jpeg_camera(tmp_path: Path) -> Path:
        path = tmp_path / "camera.jpg"
        ...  # build the file
        return path

Builders receive an empty, sample-private directory and must return the path
of a regular file they created inside it. Builders must be deterministic and
must not touch the network or anything outside that directory.

Manual inspection: ``python -m tests.factories --out <dir> [name ...]`` (from ``python/``).
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

__all__ = ["Sample", "registrar", "collect", "by_name", "MODULE_NAMES", "ALL"]

Builder = Callable[[Path], Path]

# Fixed order = order of the contract test ids. Add a module here only together
# with the new file; never reorder.
MODULE_NAMES: tuple[str, ...] = ("image", "audio", "mp4", "pdf", "office", "ole", "hachoir", "fs", "heif", "packages", "mkv")


@dataclass(frozen=True, eq=False)
class Sample:
    """One synthetic sample file and what the contract may expect from it.

    name:       unique id across all modules (``[a-z0-9_]+``); also the pytest id.
    build:      ``builder(tmp_path) -> Path``.
    module:     registering module (filled by ``registrar``).
    platforms:  ``sys.platform`` prefixes the sample runs on; empty = everywhere
                (e.g. ``("win32",)`` for hidden-attribute samples, ``("linux",)`` for xattr).
    valid:      a valid file must be read with ``read_errors == []``.
    volatile:   ``fnmatch`` patterns of keys allowed to change after a mutation of
                another key (computed values such as ``fs:atime`` or a size).
    mutations:  explicit new value per key, used instead of the generic mutation
                (e.g. an enum-like EXIF Ref, or a format limit on the generic text).
    handler:    handler instance to use instead of ``HandlerRegistry.get(path)``;
                only for in-test fakes. Factory modules should leave it ``None``.
    tags:       free labels (e.g. ``{"read_only"}``) for targeted tests.
    """
    name: str
    build: Builder
    module: str = ""
    platforms: tuple[str, ...] = ()
    valid: bool = True
    volatile: tuple[str, ...] = ()
    mutations: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    handler: Any = None
    tags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.name or not all(c.islower() or c.isdigit() or c == "_" for c in self.name) \
                or not self.name.isascii():
            raise ValueError(f"Invalid sample name {self.name!r}: use [a-z0-9_]+")
        if not callable(self.build):
            raise TypeError(f"Sample {self.name!r}: build must be callable")
        object.__setattr__(self, "platforms", tuple(self.platforms))
        object.__setattr__(self, "volatile", tuple(self.volatile))
        object.__setattr__(self, "tags", frozenset(self.tags))
        object.__setattr__(self, "mutations", MappingProxyType(dict(self.mutations)))

    @property
    def available(self) -> bool:
        """True when the sample can run on the current platform."""
        return not self.platforms or any(sys.platform.startswith(p) for p in self.platforms)

    def make(self, directory: Path) -> Path:
        """Build the sample inside ``directory`` (created if needed) and return its path."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = Path(self.build(directory))
        resolved, root = path.resolve(), directory.resolve()
        if root not in resolved.parents:
            raise AssertionError(f"Sample {self.name!r} built {path} outside {directory}")
        if not resolved.is_file():
            raise AssertionError(f"Sample {self.name!r} did not create a regular file: {path}")
        return path

    def __repr__(self) -> str:
        return f"Sample({self.name!r}, module={self.module!r})"


def registrar(samples: list[Sample], module: str) -> Callable[..., Callable[[Builder], Builder]]:
    """Return a ``@sample(name, **options)`` decorator appending to ``samples``.

    ``options`` are the ``Sample`` fields other than ``name``/``build``/``module``.
    The decorated builder is returned unchanged, so it stays directly callable.
    """
    short = module.rsplit(".", 1)[-1]

    def sample(name: str, **options: Any) -> Callable[[Builder], Builder]:
        def decorate(build: Builder) -> Builder:
            if any(s.name == name for s in samples):
                raise ValueError(f"Duplicate sample name {name!r} in {short}")
            samples.append(Sample(name=name, build=build, module=short, **options))
            return build
        return decorate

    return sample


def collect() -> list[Sample]:
    """Import every factory module and return all samples (unique names, fixed order)."""
    result: list[Sample] = []
    seen: dict[str, str] = {}
    for name in MODULE_NAMES:
        module = importlib.import_module(f"{__name__}.{name}")
        samples = getattr(module, "SAMPLES", None)
        if not isinstance(samples, list):
            raise TypeError(f"{module.__name__}.SAMPLES must be a list")
        for s in samples:
            if not isinstance(s, Sample):
                raise TypeError(f"{module.__name__}.SAMPLES contains {s!r}, not a Sample")
            if s.name in seen:
                raise ValueError(f"Sample {s.name!r} registered by both {seen[s.name]} and {name}")
            seen[s.name] = name
            result.append(s)
    return result


ALL: list[Sample] = collect()


def by_name(name: str) -> Sample:
    for s in ALL:
        if s.name == name:
            return s
    raise KeyError(name)
