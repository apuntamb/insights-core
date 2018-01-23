import itertools
import logging
import os
import re
import traceback

from collections import defaultdict
from glob import glob

from insights.core import blacklist, dr
from insights.core.filters import get_filters
from insights.core.context import ExecutionContext, FSRoots, HostContext
from insights.core.plugins import datasource, ContentException, is_datasource
from insights.core.serde import deserializer, serializer

log = logging.getLogger(__name__)


COMMANDS = {}


def mangle_command(command, name_max=255):
    """
    Mangle a command line string into something suitable for use as the basename of a filename.
    At minimum this function must remove slashes, but it also does other things to clean
    the basename: removing directory names from the command name, replacing many non-
    characters with undersores, in addition to replacing slashes with dots.

    By default, curly braces, '{' and '}', are replaced with underscore, set 'has_variables'
    to leave curly braces alone.

    This function was copied from the function that insights-client uses to create the name it
    to capture the output of the command.

    Here, server side, it is used to figure out what file in the archive contains the output
    a command.  Server side, the command may contain references to variables (names
    matching curly braces) that will be expanded before the name is actually used as a file name.

    To completly mimic the insights-client behavior, curly braces need to be replaced
    underscores.  If the command has variable references, the curly braces must be left alone.
    Set has_variables, to leave curly braces alone.

    This implementation of 'has_variables' assumes that variable names only contain
    that are not replaced by mangle_command.
    """
    pattern = r"[^\w\-\.\/]+"

    mangledname = re.sub(r"^/(usr/|)(bin|sbin)/", "", command)
    mangledname = re.sub(pattern, "_", mangledname)
    mangledname = re.sub(r"/", ".", mangledname).strip(" ._-")
    mangledname = mangledname[:name_max]
    return mangledname


class ContentProvider(object):
    def __init__(self):
        self.cmd = None
        self.args = None
        self.rc = None
        self.path = None
        self._content = None
        self._exception = None

    def load(self):
        raise NotImplemented()

    @property
    def content(self):
        if self._exception:
            raise self._exception

        if self._content is None:
            try:
                self._content = self.load()
            except Exception as ex:
                self._exception = ex
                raise

        return self._content

    def __repr__(self):
        msg = "<%s(path=%s, cmd=%s)>"
        return msg % (self.__class__.__name__, self.path or "", self.cmd or "")

    def __unicode__(self):
        return self.__repr__()

    def __str__(self):
        return self.__unicode__()


class FileProvider(ContentProvider):
    def __init__(self, relative_path, root="/", filters=None):
        super(FileProvider, self).__init__()
        self.root = root
        self.relative_path = relative_path.lstrip("/")

        self.path = os.path.join(root, self.relative_path)
        self.file_name = os.path.basename(self.path)

        self.filters = filters or set()
        self.validate()

    def validate(self):
        if not blacklist.allow_file("/" + self.relative_path):
            raise dr.SkipComponent()

        if not os.path.exists(self.path):
            raise ContentException("%s does not exist." % self.path)

        if not os.access(self.path, os.R_OK):
            raise ContentException("Cannot access %s" % self.path)

    def __repr__(self):
        return '%s("%s")' % (self.__class__.__name__, self.path)


class RawFileProvider(FileProvider):
    def load(self):
        with open(self.path, 'rb') as f:
            return f.read()


class TextFileProvider(FileProvider):
    def load(self):
        with open(self.path, 'r') as f:
            if self.filters:
                # This should shell out to a grep pipeline
                return [l.rstrip() for l in f if any(s in l for s in self.filters)]
            else:
                return [l.rstrip() for l in f]


class CommandOutputProvider(ContentProvider):
    def __init__(self, cmd, ctx, args=None, content=None, rc=None, split=True, keep_rc=False):
        super(CommandOutputProvider, self).__init__()
        self.cmd = cmd
        self.path = os.path.join("insights_commands", mangle_command(cmd))
        self.ctx = ctx
        # args are already interpolated into cmd. They're stored here for context."
        self.args = args
        self._content = content
        self.rc = rc
        self.split = split
        self.keep_rc = keep_rc
        self.validate()

    def validate(self):
        if not blacklist.allow_command(self.cmd):
            raise dr.SkipComponent()

    def load(self):
        if self.keep_rc:
            self.rc, result = self.ctx.shell_out(self.cmd, self.split, keep_rc=True)
            return result
        else:
            return self.ctx.shell_out(self.cmd, self.split)

    def __repr__(self):
        return 'CommandOutputProvider("%s")' % self.cmd


class RegistryPoint(object):
    """ Marker class for declaring that an element of a `SpecSet` subclass
        is a registry point against which further subclasses can register
        datasource implementations by simply declaring them with the same name.
    """
    def __init__(self, metadata=None, multi_output=False):
        self.metadata = metadata
        self.multi_output = multi_output


def _registry_point(rp):

    @datasource(metadata=rp.metadata, multi_output=rp.multi_output)
    def inner(broker):
        for c in reversed(dr.get_added_dependencies(inner)):
            if c in broker:
                return broker[c]
        raise dr.SkipComponent()
    return inner


class SpecDescriptor(object):
    """ Descriptor Protocol handler that returns the literal function from a
        class during dot (.) access.
    """
    def __init__(self, func):
        self.func = func

    def __get__(self, obj, obj_type):
        return self.func

    def __set__(self, obj, val):
        raise AttributeError()


def _get_ctx_dependencies(func):
    ctxs = []
    try:
        for c in dr.get_dependencies(func):
            if issubclass(c, ExecutionContext):
                ctxs.append(c)
    except:
        pass
    return ctxs


def _register_context_handler(parents, func):
    name = func.__name__
    parents = list(itertools.takewhile(lambda x: name in x.registry, parents))
    if not parents:
        return

    ctx_handlers = parents[-1].context_handlers
    for c in _get_ctx_dependencies(func):
        for old in ctx_handlers[name][c]:
            dr.add_ignore(old, c)
        ctx_handlers[name][c].append(func)


def _resolve_registry_points(cls, base, dct):
    module = cls.__module__
    parents = [x for x in cls.__mro__ if x not in (cls, SpecSet, object)]

    for k, v in dct.items():
        if isinstance(v, RegistryPoint):
            v.__name__ = k
            v = _registry_point(v)
            cls.registry[k] = v

        if is_datasource(v):
            v.__qualname__ = ".".join([cls.__name__, k])
            v.__name__ = k
            v.__module__ = module
            setattr(cls, k, SpecDescriptor(v))
            if k in base.registry:
                point = base.registry[k]
                dr.add_dependency(point, v)
                dr.mark_hidden(v)
                _register_context_handler(parents, v)


class SpecSetMeta(type):
    """ The metaclass that converts RegistryPoint markers to regisry point
        datasources and hooks implementations for them into the registry.
    """
    def __new__(cls, name, bases, dct):
        dct["context_handlers"] = defaultdict(lambda: defaultdict(list))
        dct["registry"] = {}
        return super(SpecSetMeta, cls).__new__(cls, name, bases, dct)

    def __init__(cls, name, bases, dct):
        if name == "SpecSet":
            return
        if len(bases) > 1:
            raise Exception("SpecSet subclasses must inherit from only one class.")
        _resolve_registry_points(cls, bases[0], dct)


class SpecSet(object):
    """ The base class for all spec declarations. Extend this class and define
        your datasources directly or with a `SpecFactory`.
    """
    __metaclass__ = SpecSetMeta


def _get_context(context, alternatives, broker):
    if context:
        if isinstance(context, list):
            return dr.first_of(context, broker)
        return broker.get(context)
    return dr.first_of(alternatives, broker)


def simple_file(path, context=None, kind=TextFileProvider):

    @datasource(context or FSRoots)
    def inner(broker):
        ctx = _get_context(context, FSRoots, broker)
        return kind(ctx.locate_path(path), root=ctx.root, filters=get_filters(inner))
    return inner


def glob_file(patterns, ignore=None, context=None, kind=TextFileProvider):
    if not isinstance(patterns, (list, set)):
        patterns = [patterns]

    @datasource(context or FSRoots, multi_output=True)
    def inner(broker):
        ctx = _get_context(context, FSRoots, broker)
        root = ctx.root
        results = []
        for pattern in patterns:
            pattern = ctx.locate_path(pattern)
            for path in glob(os.path.join(root, pattern.lstrip('/'))):
                if ignore and re.search(ignore, path):
                    continue
                try:
                    results.append(kind(path[len(root):], root=root, filters=get_filters(inner)))
                except:
                    log.debug(traceback.format_exc())
        if results:
            return results
        raise ContentException("[%s] didn't match." % ', '.join(patterns))
    return inner


def first_file(files, context=None, kind=TextFileProvider):

    @datasource(context or FSRoots)
    def inner(broker):
        ctx = _get_context(context, FSRoots, broker)
        root = ctx.root
        for f in files:
            try:
                return kind(ctx.locate_path(f), root=root, filters=get_filters(inner))
            except:
                pass
        raise ContentException("None of [%s] found." % ', '.join(files))
    return inner


def listdir(path, context=None):

    @datasource(context or FSRoots)
    def inner(broker):
        ctx = _get_context(context, FSRoots, broker)
        p = os.path.join(ctx.root, path.lstrip('/'))
        p = ctx.locate_path(p)
        if os.path.isdir(p):
            return os.listdir(p)

        result = glob(p)
        if result:
            return [os.path.basename(r) for r in result]
        raise ContentException("Can't list %s or nothing there." % p)
    return inner


def simple_command(cmd, context=HostContext, split=True, keep_rc=False, timeout=None):

    @datasource(context)
    def inner(broker):
        ctx = broker[context]
        rc = None
        raw = ctx.shell_out(cmd, split=split, keep_rc=keep_rc, timeout=timeout)
        if keep_rc:
            rc, result = raw
        else:
            result = raw
        return CommandOutputProvider(cmd, ctx, split=split, content=result, rc=rc, keep_rc=keep_rc)
    COMMANDS[inner] = cmd
    return inner


def foreach_execute(provider, cmd, context=HostContext, split=True, keep_rc=False, timeout=None):

    @datasource(provider, context, multi_output=True)
    def inner(broker):
        result = []
        source = broker[provider]
        ctx = broker[context]
        if isinstance(source, ContentProvider):
            source = source.content
        if not isinstance(source, (list, set)):
            source = [source]
        for e in source:
            try:
                the_cmd = cmd % e
                rc = None
                raw = ctx.shell_out(the_cmd, split=split, keep_rc=keep_rc, timeout=timeout)
                if keep_rc:
                    rc, output = raw
                else:
                    output = raw

                result.append(CommandOutputProvider(the_cmd, ctx, args=e, content=output, rc=rc, split=split, keep_rc=keep_rc))
            except:
                log.debug(traceback.format_exc())
        if result:
            return result
        raise ContentException("No results found for [%s]" % cmd)
    return inner


def foreach_collect(provider, path, ignore=None, context=HostContext, kind=TextFileProvider):

    @datasource(provider, context, multi_output=True)
    def inner(broker):
        result = []
        source = broker[provider]
        ctx = _get_context(context, FSRoots, broker)
        root = ctx.root
        if isinstance(source, ContentProvider):
            source = source.content
        if not isinstance(source, (list, set)):
            source = [source]
        for e in source:
            pattern = ctx.locate_path(path % e)
            for p in glob(os.path.join(root, pattern.lstrip('/'))):
                if ignore and re.search(ignore, p):
                    continue
                try:
                    result.append(kind(p[len(root):], root=root, filters=get_filters(inner)))
                except:
                    log.debug(traceback.format_exc())
        if result:
            return result
        raise ContentException("No results found for [%s]" % path)
    return inner


def first_of(deps):
    """ Given a list of dependencies, returns the first of the list
        that exists in the broker. At least one must be present, or this
        component won't fire.
    """
    dr.mark_hidden(deps)

    @datasource(deps)
    def inner(broker):
        for c in deps:
            if c in broker:
                return broker[c]

    return inner


@serializer(TextFileProvider)
def serialize_text_provider(obj):
    d = {}
    d["path"] = obj.path
    d["_content"] = obj.content
    return d


@serializer(CommandOutputProvider)
def serialize_command_provider(obj):
    d = {}
    d["rc"] = obj.rc
    d["cmd"] = obj.cmd
    d["args"] = obj.args
    d["path"] = obj.path
    d["_content"] = obj.content
    return d


@deserializer(ContentProvider)
def deserialize_content(_type, obj):
    c = ContentProvider()
    for k, v in obj.items():
        setattr(c, k, v)
    return c
