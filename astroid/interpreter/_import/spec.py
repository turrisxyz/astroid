# Licensed under the LGPL: https://www.gnu.org/licenses/old-licenses/lgpl-2.1.en.html
# For details: https://github.com/PyCQA/astroid/blob/main/LICENSE
# Copyright (c) https://github.com/PyCQA/astroid/blob/main/CONTRIBUTORS.txt

import abc
import enum
import importlib
import importlib.machinery
import importlib.util
import os
import sys
import zipimport
from functools import lru_cache
from pathlib import Path
from typing import List, NamedTuple, Optional, Sequence, Tuple

from astroid.modutils import EXT_LIB_DIRS

from . import util


class ModuleType(enum.Enum):
    """Python module types used for ModuleSpec."""

    C_BUILTIN = enum.auto()
    C_EXTENSION = enum.auto()
    PKG_DIRECTORY = enum.auto()
    PY_CODERESOURCE = enum.auto()
    PY_COMPILED = enum.auto()
    PY_FROZEN = enum.auto()
    PY_RESOURCE = enum.auto()
    PY_SOURCE = enum.auto()
    PY_ZIPMODULE = enum.auto()
    PY_NAMESPACE = enum.auto()


class ModuleSpec(NamedTuple):
    """Defines a class similar to PEP 420's ModuleSpec

    A module spec defines a name of a module, its type, location
    and where submodules can be found, if the module is a package.
    """

    name: str
    type: ModuleType
    location: "str | None" = None
    origin: "str | None" = None
    submodule_search_locations: "Sequence[str] | None" = None


class Finder:
    """A finder is a class which knows how to find a particular module."""

    def __init__(self, path=None):
        self._path = path or sys.path

    @abc.abstractmethod
    def find_module(
        self,
        modname: str,
        module_parts: List[str],
        processed: List[str],
        submodule_path: Optional[List[str]],
    ) -> Optional[ModuleSpec]:
        """Find the given module

        Each finder is responsible for each protocol of finding, as long as
        they all return a ModuleSpec.

        :param str modname: The module which needs to be searched.
        :param list module_parts: It should be a list of strings,
                                  where each part contributes to the module's
                                  namespace.
        :param list processed: What parts from the module parts were processed
                               so far.
        :param list submodule_path: A list of paths where the module
                                    can be looked into.
        :returns: A ModuleSpec, describing how and where the module was found,
                  None, otherwise.
        """

    def contribute_to_path(self, spec, processed):
        """Get a list of extra paths where this finder can search."""


class ImportlibFinder(Finder):
    """A finder based on the importlib module."""

    _SUFFIXES: Sequence[Tuple[str, ModuleType]] = (
        [(s, ModuleType.C_EXTENSION) for s in importlib.machinery.EXTENSION_SUFFIXES]
        + [(s, ModuleType.PY_SOURCE) for s in importlib.machinery.SOURCE_SUFFIXES]
        + [(s, ModuleType.PY_COMPILED) for s in importlib.machinery.BYTECODE_SUFFIXES]
    )

    def find_module(
        self,
        modname: str,
        module_parts: List[str],
        processed: List[str],
        submodule_path: Optional[List[str]],
    ) -> Optional[ModuleSpec]:
        if submodule_path is not None:
            submodule_path = list(submodule_path)
        else:
            try:
                spec = importlib.util.find_spec(modname)
                if spec:
                    if spec.loader is importlib.machinery.BuiltinImporter:
                        return ModuleSpec(
                            name=modname,
                            location=None,
                            type=ModuleType.C_BUILTIN,
                        )
                    if spec.loader is importlib.machinery.FrozenImporter:
                        return ModuleSpec(
                            name=modname,
                            location=getattr(spec.loader_state, "filename", None),
                            type=ModuleType.PY_FROZEN,
                        )
            except ValueError:
                pass
            submodule_path = sys.path

        for entry in submodule_path:
            package_directory = os.path.join(entry, modname)
            for suffix in (".py", importlib.machinery.BYTECODE_SUFFIXES[0]):
                package_file_name = "__init__" + suffix
                file_path = os.path.join(package_directory, package_file_name)
                if os.path.isfile(file_path):
                    return ModuleSpec(
                        name=modname,
                        location=package_directory,
                        type=ModuleType.PKG_DIRECTORY,
                    )
            for suffix, type_ in ImportlibFinder._SUFFIXES:
                file_name = modname + suffix
                file_path = os.path.join(entry, file_name)
                if os.path.isfile(file_path):
                    return ModuleSpec(name=modname, location=file_path, type=type_)
        return None

    def contribute_to_path(self, spec, processed):
        if spec.location is None:
            # Builtin.
            return None

        if _is_setuptools_namespace(spec.location):
            # extend_path is called, search sys.path for module/packages
            # of this name see pkgutil.extend_path documentation
            path = [
                os.path.join(p, *processed)
                for p in sys.path
                if os.path.isdir(os.path.join(p, *processed))
            ]
        elif spec.name == "distutils" and not any(
            spec.location.lower().startswith(ext_lib_dir.lower())
            for ext_lib_dir in EXT_LIB_DIRS
        ):
            # virtualenv below 20.0 patches distutils in an unexpected way
            # so we just find the location of distutils that will be
            # imported to avoid spurious import-error messages
            # https://github.com/PyCQA/pylint/issues/5645
            # A regression test to create this scenario exists in release-tests.yml
            # and can be triggered manually from GitHub Actions
            distutils_spec = importlib.util.find_spec("distutils")
            if distutils_spec and distutils_spec.origin:
                origin_path = Path(
                    distutils_spec.origin
                )  # e.g. .../distutils/__init__.py
                path = [str(origin_path.parent)]  # e.g. .../distutils
            else:
                path = [spec.location]
        else:
            path = [spec.location]
        return path


class ExplicitNamespacePackageFinder(ImportlibFinder):
    """A finder for the explicit namespace packages, generated through pkg_resources."""

    def find_module(self, modname, module_parts, processed, submodule_path):
        if processed:
            modname = ".".join(processed + [modname])
        if util.is_namespace(modname) and modname in sys.modules:
            submodule_path = sys.modules[modname].__path__
            return ModuleSpec(
                name=modname,
                location="",
                origin="namespace",
                type=ModuleType.PY_NAMESPACE,
                submodule_search_locations=submodule_path,
            )
        return None

    def contribute_to_path(self, spec, processed):
        return spec.submodule_search_locations


class ZipFinder(Finder):
    """Finder that knows how to find a module inside zip files."""

    def __init__(self, path):
        super().__init__(path)
        self._zipimporters = _precache_zipimporters(path)

    def find_module(self, modname, module_parts, processed, submodule_path):
        try:
            file_type, filename, path = _search_zip(module_parts, self._zipimporters)
        except ImportError:
            return None

        return ModuleSpec(
            name=modname,
            location=filename,
            origin="egg",
            type=file_type,
            submodule_search_locations=path,
        )


class PathSpecFinder(Finder):
    """Finder based on importlib.machinery.PathFinder."""

    def find_module(self, modname, module_parts, processed, submodule_path):
        spec = importlib.machinery.PathFinder.find_spec(modname, path=submodule_path)
        if spec:
            # origin can be either a string on older Python versions
            # or None in case it is a namespace package:
            # https://github.com/python/cpython/pull/5481
            is_namespace_pkg = spec.origin in {"namespace", None}
            location = spec.origin if not is_namespace_pkg else None
            module_type = ModuleType.PY_NAMESPACE if is_namespace_pkg else None
            spec = ModuleSpec(
                name=spec.name,
                location=location,
                origin=spec.origin,
                type=module_type,
                submodule_search_locations=list(spec.submodule_search_locations or []),
            )
        return spec

    def contribute_to_path(self, spec, processed):
        if spec.type == ModuleType.PY_NAMESPACE:
            return spec.submodule_search_locations
        return None


_SPEC_FINDERS = (
    ImportlibFinder,
    ZipFinder,
    PathSpecFinder,
    ExplicitNamespacePackageFinder,
)


def _is_setuptools_namespace(location):
    try:
        with open(os.path.join(location, "__init__.py"), "rb") as stream:
            data = stream.read(4096)
    except OSError:
        return None
    else:
        extend_path = b"pkgutil" in data and b"extend_path" in data
        declare_namespace = (
            b"pkg_resources" in data and b"declare_namespace(__name__)" in data
        )
        return extend_path or declare_namespace


@lru_cache()
def _cached_set_diff(left, right):
    result = set(left)
    result.difference_update(right)
    return result


def _precache_zipimporters(path=None):
    """
    For each path that has not been already cached
    in the sys.path_importer_cache, create a new zipimporter
    instance and add it into the cache.
    Return a dict associating all paths, stored in the cache, to corresponding
    zipimporter instances.

    :param path: paths that has to be added into the cache
    :return: association between paths stored in the cache and zipimporter instances
    """
    pic = sys.path_importer_cache

    # When measured, despite having the same complexity (O(n)),
    # converting to tuples and then caching the conversion to sets
    # and the set difference is faster than converting to sets
    # and then only caching the set difference.

    req_paths = tuple(path or sys.path)
    cached_paths = tuple(pic)
    new_paths = _cached_set_diff(req_paths, cached_paths)
    # pylint: disable=no-member
    for entry_path in new_paths:
        try:
            pic[entry_path] = zipimport.zipimporter(entry_path)
        except zipimport.ZipImportError:
            continue
    return {
        key: value
        for key, value in pic.items()
        if isinstance(value, zipimport.zipimporter)
    }


def _search_zip(modpath, pic):
    for filepath, importer in list(pic.items()):
        if importer is not None:
            found = importer.find_module(modpath[0])
            if found:
                if not importer.find_module(os.path.sep.join(modpath)):
                    raise ImportError(
                        "No module named %s in %s/%s"
                        % (".".join(modpath[1:]), filepath, modpath)
                    )
                # import code; code.interact(local=locals())
                return (
                    ModuleType.PY_ZIPMODULE,
                    os.path.abspath(filepath) + os.path.sep + os.path.sep.join(modpath),
                    filepath,
                )
    raise ImportError(f"No module named {'.'.join(modpath)}")


def _find_spec_with_path(search_path, modname, module_parts, processed, submodule_path):
    finders = [finder(search_path) for finder in _SPEC_FINDERS]
    for finder in finders:
        spec = finder.find_module(modname, module_parts, processed, submodule_path)
        if spec is None:
            continue
        return finder, spec

    raise ImportError(f"No module named {'.'.join(module_parts)}")


def find_spec(modpath, path=None):
    """Find a spec for the given module.

    :type modpath: list or tuple
    :param modpath:
      split module's name (i.e name of a module or package split
      on '.'), with leading empty strings for explicit relative import

    :type path: list or None
    :param path:
      optional list of path where the module or package should be
      searched (use sys.path if nothing or None is given)

    :rtype: ModuleSpec
    :return: A module spec, which describes how the module was
             found and where.
    """
    _path = path or sys.path

    # Need a copy for not mutating the argument.
    modpath = modpath[:]

    submodule_path = None
    module_parts = modpath[:]
    processed = []

    while modpath:
        modname = modpath.pop(0)
        finder, spec = _find_spec_with_path(
            _path, modname, module_parts, processed, submodule_path or path
        )
        processed.append(modname)
        if modpath:
            submodule_path = finder.contribute_to_path(spec, processed)

        if spec.type == ModuleType.PKG_DIRECTORY:
            spec = spec._replace(submodule_search_locations=submodule_path)

    return spec
