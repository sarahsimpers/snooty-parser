import datetime
import logging
import os
import pickle
import re
import sys
import tempfile
import time
import urllib.parse
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import formatdate
from pathlib import Path, PurePath, PurePosixPath
from time import mktime
from typing import (
    Callable,
    ClassVar,
    Container,
    Counter,
    Dict,
    Hashable,
    Iterator,
    List,
    Optional,
    Set,
    TextIO,
    Tuple,
    TypeVar,
    cast,
)

import docutils.nodes
import docutils.parsers.rst.directives
import requests
import watchdog.events
import watchdog.observers
import watchdog.observers.api

from . import n
from .types import FileId

logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_K = TypeVar("_K", bound=Hashable)
PAT_INVALID_ID_CHARACTERS = re.compile(r"[^\w_\.\-]")
PAT_URI = re.compile(r"^(?P<schema>[a-z]+)://")
SOURCE_FILE_EXTENSIONS = {".txt", ".rst", ".yaml"}
RST_EXTENSIONS = {".txt", ".rst"}


def reroot_path(
    filename: PurePosixPath, docpath: PurePath, project_root: Path
) -> Tuple[FileId, Path]:
    """Files within a project may refer to other files. Return a canonical path
    relative to the project root."""
    if filename.is_absolute():
        rel_fn = FileId(*filename.parts[1:])
    else:
        rel_fn = FileId(*docpath.parent.joinpath(filename).parts).collapse_dots()
    return rel_fn, project_root.joinpath(rel_fn).resolve()


def is_relative_to(a: Path, b: Path) -> bool:
    try:
        a.relative_to(b)
        return True
    except ValueError:
        return False


def get_files(
    root: Path, extensions: Container[str], must_be_relative_to: Optional[Path] = None
) -> Iterator[Path]:
    """Recursively iterate over files underneath the given root, yielding
    only filenames with the given extensions. Symlinks are followed, but
    any given concrete directory is only scanned once.

    By default, directories above the given root in the filesystem are not
    scanned, but this can be overridden with the must_be_relative_to parameter."""
    root_resolved = root.resolve()
    seen: Set[Path] = set()

    if must_be_relative_to is None:
        must_be_relative_to = root_resolved

    for base, dirs, files in os.walk(root, followlinks=True):
        base_resolved = Path(base).resolve()
        if not is_relative_to(base_resolved, must_be_relative_to):
            # Prevent a race between our checking if a symlink is valid, and our
            # actually entering it.
            continue

        # Preserve both the actual resolved path and the directory name
        dirs_set = dict(((base_resolved.joinpath(d).resolve(), d) for d in dirs))

        # Only recurse into directories which are within our prefix
        dirs[:] = [
            d_name
            for d_path, d_name in ((k, v) for k, v in dirs_set.items() if k not in seen)
            if is_relative_to(
                base_resolved.joinpath(d_path).resolve(), must_be_relative_to
            )
        ]

        seen.update(dirs_set)

        for name in files:
            ext = os.path.splitext(name)[1]
            if ext not in extensions:
                continue

            path = Path(os.path.join(base, name))
            # Detect and ignore symlinks outside of our jail
            if is_relative_to(path.resolve(), must_be_relative_to):
                yield path


def get_line(node: docutils.nodes.Node) -> int:
    """Return the first line number we can find in node's ancestry."""

    def line_of_node(node: docutils.nodes.Node) -> Optional[int]:
        """Sometimes you need node['line']. Sometimes you need node.line.
        Sometimes you want to just run away and herd yaks."""
        if isinstance(node, docutils.nodes.Element) and "line" in node:
            return cast(int, node["line"])

        return node.line

    while line_of_node(node) is None:
        if node.parent is None:
            # This is probably a document node
            return 0
        node = node.parent

    return cast(int, line_of_node(node)) - 1


def ast_dive(ast: n.Node) -> Iterator[n.Node]:
    """Yield each node in an AST in no particular order."""
    children: List[n.Node] = []
    if isinstance(ast, n.Parent):
        children.extend(ast.children)
    children.extend(getattr(ast, "argument", []))
    yield ast
    for child in children:
        yield from ast_dive(child)


def add_doc_target_ext(target: str, docpath: PurePath, project_root: Path) -> Path:
    """Given the target file of a doc role, add the appropriate extension and return full file path"""
    # Add .txt to end of doc role target path
    target_path = PurePosixPath(target)
    # Adding the current suffix first takes into account dotted targets
    new_suffix = target_path.suffix + ".txt"
    target_path = target_path.with_suffix(new_suffix)

    fileid, resolved_target_path = reroot_path(target_path, docpath, project_root)
    return resolved_target_path


class FileWatcher:
    """A monitor for file changes."""

    class AssetChangedHandler(watchdog.events.FileSystemEventHandler):
        """A filesystem event handler which flags pages as having changed
        after an included asset has changed."""

        def __init__(
            self,
            directories: Dict[Path, "FileWatcher.AssetWatch"],
            on_event: Callable[[watchdog.events.FileSystemEvent], None],
        ) -> None:
            super().__init__()
            self.directories = directories
            self.on_event = on_event

        def dispatch(self, event: watchdog.events.FileSystemEvent) -> None:
            """Delegate filesystem events."""
            path = Path(event.src_path)
            if (
                path.parent in self.directories
                and path.name in self.directories[path.parent].filenames
            ):
                self.on_event(event)

    @dataclass
    class AssetWatch:
        """Track files in a directory to watch. This reflects the underlying interface
        exposed by watchdog."""

        filenames: Counter[str]
        watch_handle: watchdog.observers.api.ObservedWatch

        def __len__(self) -> int:
            return len(self.filenames)

    def __init__(
        self, on_event: Callable[[watchdog.events.FileSystemEvent], None]
    ) -> None:
        self.observer = watchdog.observers.Observer()
        self.directories: Dict[Path, FileWatcher.AssetWatch] = {}
        self.handler = self.AssetChangedHandler(self.directories, on_event)

    def watch_file(self, path: Path) -> None:
        """Start reporting upon changes to a file."""
        directory = path.parent
        logger.debug("Starting watch: %s", path)
        if directory in self.directories:
            self.directories[directory].filenames[path.name] += 1
            return

        watch = self.observer.schedule(self.handler, str(directory))
        self.directories[directory] = self.AssetWatch(Counter({path.name: 1}), watch)

    def end_watch(self, path: Path) -> None:
        """Stop watching a file."""
        directory = path.parent
        if directory not in self.directories:
            return

        watch = self.directories[directory]
        watch.filenames[path.name] -= 1
        if watch.filenames[path.name] <= 0:
            del watch.filenames[path.name]

        # If there are no files remaining in this watch directory, unwatch it.
        if not watch.filenames:
            self.observer.unschedule(watch.watch_handle)
            logger.info("Stopping watch: %s", path)
            del self.directories[directory]

    def start(self) -> None:
        """Start a thread watching for file changes."""
        self.observer.start()

    def stop(self, join: bool = False) -> None:
        """Stop this file watcher."""
        self.observer.stop()
        if join:
            self.observer.join()

    def __enter__(self) -> "FileWatcher":
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    def __len__(self) -> int:
        return sum(len(w) for w in self.directories.values())


def option_string(argument: Optional[str]) -> Optional[str]:
    """
    Check for a valid string option and return it. If no argument is given,
    raise ``ValueError``.
    """
    if argument and argument.strip():
        return argument
    raise ValueError("Must supply string argument to option")


def option_bool(argument: Optional[str]) -> bool:
    """
    Check for a valid boolean option return it. If no argument is given,
    treat it as a flag, and return True.
    """
    if argument and argument.strip():
        output = docutils.parsers.rst.directives.choice(argument, ("true", "false"))
        return output == "true"
    return True


def option_flag(argument: Optional[str]) -> bool:
    """
    Variant of the docutils flag handler.
    Check for a valid flag option (no argument) and return ``True``.
    (Directive option conversion function.)

    Raise ``ValueError`` if an argument is found.
    """
    if argument and argument.strip():
        raise ValueError('no argument is allowed; "%s" supplied' % argument)
    return True


def split_domain(name: str) -> Tuple[str, str]:
    """Split a fully-qualified reStructuredText directive or role name into
    its (domain, name) pair.

    For example, "mongodb:ref" becomes ("mongodb", "ref"), while simply
    "ref" becomes ("", "ref").
    """
    parts = name.split(":", 1)
    if len(parts) == 1:
        return "", parts[0]
    return parts[0], parts[1]


def fast_deep_copy(v: _T) -> _T:
    """Time-efficiently create deep copy of trusted data.
    This implementation currently invokes pickle, so should NOT be called on untrusted objects."""
    return cast(_T, pickle.loads(pickle.dumps(v)))


def make_html5_id(orig: str) -> str:
    """Turn an ID into a valid HTML5 element ID."""
    clean_id = PAT_INVALID_ID_CHARACTERS.sub("-", orig)
    if not clean_id:
        clean_id = "unnamed"
    return clean_id


class PerformanceLogger:
    _singleton: Optional["PerformanceLogger"] = None

    def __init__(self) -> None:
        self._times: Dict[str, List[float]] = defaultdict(list)

    @contextmanager
    def start(self, name: str) -> Iterator[None]:
        start_time = time.perf_counter()
        try:
            yield None
        finally:
            self._times[name].append(time.perf_counter() - start_time)

    def times(self) -> Dict[str, float]:
        return {k: min(v) for k, v in self._times.items()}

    def print(self, file: TextIO = sys.stdout) -> None:
        times = self.times()
        title_column_width = max(len(x) for x in times.keys())
        for name, entry_time in times.items():
            print(f"{name:{title_column_width}} {entry_time:.2f}", file=file)

    @classmethod
    def singleton(cls) -> "PerformanceLogger":
        assert cls._singleton is not None
        return cls._singleton


PerformanceLogger._singleton = PerformanceLogger()


class HTTPCache:
    _singleton: ClassVar[Optional["HTTPCache"]] = None
    DEFAULT_CACHE_DIR: ClassVar[Path] = Path.home().joinpath(".cache", "snooty")

    @classmethod
    def get(cls) -> "HTTPCache":
        if cls._singleton is None:
            cls._singleton = cls()

        return cls._singleton

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        self.cache_dir = self.DEFAULT_CACHE_DIR if not cache_dir else cache_dir

    def __getitem__(self, url: str) -> bytes:
        logger.debug(f"Fetching: {url}")

        # Make our user's cache directory if it doesn't exist
        filename = urllib.parse.quote(url, safe="")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        inventory_path = self.cache_dir.joinpath(filename)

        # Only re-request if more than an hour old
        request_headers: Dict[str, str] = {}
        mtime: Optional[datetime.datetime] = None
        try:
            mtime = datetime.datetime.fromtimestamp(inventory_path.stat().st_mtime)
        except FileNotFoundError:
            pass

        if mtime is not None:
            if (datetime.datetime.now() - mtime) < datetime.timedelta(hours=1):
                request_headers["If-Modified-Since"] = formatdate(
                    mktime(mtime.timetuple()), usegmt=True
                )

        res = requests.get(url, headers=request_headers)

        res.raise_for_status()
        if res.status_code == 304:
            return inventory_path.read_bytes()

        # Atomically (re)write the cache file entry
        try:
            tempf = tempfile.NamedTemporaryFile(dir=self.cache_dir)
            tempf.write(res.content)
            os.replace(tempf.name, inventory_path)
        finally:
            try:
                tempf.close()
            except FileNotFoundError:
                pass

        return res.content
