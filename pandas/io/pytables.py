"""
High level interface to PyTables for reading and writing pandas data structures
to disk
"""

import copy
from datetime import date
import itertools
import os
import re
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Hashable,
    List,
    Optional,
    Tuple,
    Type,
    Union,
)
import warnings

import numpy as np

from pandas._config import config, get_option

from pandas._libs import lib, writers as libwriters
from pandas._libs.tslibs import timezones
from pandas.compat._optional import import_optional_dependency
from pandas.errors import PerformanceWarning

from pandas.core.dtypes.common import (
    ensure_object,
    is_categorical_dtype,
    is_datetime64_dtype,
    is_datetime64tz_dtype,
    is_extension_array_dtype,
    is_list_like,
    is_timedelta64_dtype,
)
from pandas.core.dtypes.missing import array_equivalent

from pandas import (
    DataFrame,
    DatetimeIndex,
    Index,
    Int64Index,
    MultiIndex,
    PeriodIndex,
    Series,
    TimedeltaIndex,
    concat,
    isna,
)
from pandas._typing import FrameOrSeries
from pandas.core.arrays.categorical import Categorical
import pandas.core.common as com
from pandas.core.computation.pytables import PyTablesExpr, maybe_expression
from pandas.core.index import ensure_index
from pandas.core.internals import BlockManager, _block_shape, make_block

from pandas.io.common import _stringify_path
from pandas.io.formats.printing import adjoin, pprint_thing

if TYPE_CHECKING:
    from tables import File, Node  # noqa:F401


# versioning attribute
_version = "0.15.2"

# encoding
_default_encoding = "UTF-8"


def _ensure_decoded(s):
    """ if we have bytes, decode them to unicode """
    if isinstance(s, np.bytes_):
        s = s.decode("UTF-8")
    return s


def _ensure_encoding(encoding):
    # set the encoding if we need
    if encoding is None:
        encoding = _default_encoding

    return encoding


def _ensure_str(name):
    """
    Ensure that an index / column name is a str (python 3); otherwise they
    may be np.string dtype. Non-string dtypes are passed through unchanged.

    https://github.com/pandas-dev/pandas/issues/13492
    """
    if isinstance(name, str):
        name = str(name)
    return name


Term = PyTablesExpr


def _ensure_term(where, scope_level: int):
    """
    ensure that the where is a Term or a list of Term
    this makes sure that we are capturing the scope of variables
    that are passed
    create the terms here with a frame_level=2 (we are 2 levels down)
    """

    # only consider list/tuple here as an ndarray is automatically a coordinate
    # list
    level = scope_level + 1
    if isinstance(where, (list, tuple)):
        wlist = []
        for w in filter(lambda x: x is not None, where):
            if not maybe_expression(w):
                wlist.append(w)
            else:
                wlist.append(Term(w, scope_level=level))
        where = wlist
    elif maybe_expression(where):
        where = Term(where, scope_level=level)
    return where if where is None or len(where) else None


class PossibleDataLossError(Exception):
    pass


class ClosedFileError(Exception):
    pass


class IncompatibilityWarning(Warning):
    pass


incompatibility_doc = """
where criteria is being ignored as this version [%s] is too old (or
not-defined), read the file in and write it out to a new file to upgrade (with
the copy_to method)
"""


class AttributeConflictWarning(Warning):
    pass


attribute_conflict_doc = """
the [%s] attribute of the existing index is [%s] which conflicts with the new
[%s], resetting the attribute to None
"""


class DuplicateWarning(Warning):
    pass


duplicate_doc = """
duplicate entries in table, taking most recently appended
"""

performance_doc = """
your performance may suffer as PyTables will pickle object types that it cannot
map directly to c-types [inferred_type->%s,key->%s] [items->%s]
"""

# formats
_FORMAT_MAP = {"f": "fixed", "fixed": "fixed", "t": "table", "table": "table"}

format_deprecate_doc = """
the table keyword has been deprecated
use the format='fixed(f)|table(t)' keyword instead
  fixed(f) : specifies the Fixed format
             and is the default for put operations
  table(t) : specifies the Table format
             and is the default for append operations
"""

# storer class map
_STORER_MAP = {
    "series": "SeriesFixed",
    "frame": "FrameFixed",
}

# table class map
_TABLE_MAP = {
    "generic_table": "GenericTable",
    "appendable_series": "AppendableSeriesTable",
    "appendable_multiseries": "AppendableMultiSeriesTable",
    "appendable_frame": "AppendableFrameTable",
    "appendable_multiframe": "AppendableMultiFrameTable",
    "worm": "WORMTable",
}

# axes map
_AXES_MAP = {DataFrame: [0]}

# register our configuration options
dropna_doc = """
: boolean
    drop ALL nan rows when appending to a table
"""
format_doc = """
: format
    default format writing format, if None, then
    put will default to 'fixed' and append will default to 'table'
"""

with config.config_prefix("io.hdf"):
    config.register_option("dropna_table", False, dropna_doc, validator=config.is_bool)
    config.register_option(
        "default_format",
        None,
        format_doc,
        validator=config.is_one_of_factory(["fixed", "table", None]),
    )

# oh the troubles to reduce import time
_table_mod = None
_table_file_open_policy_is_strict = False


def _tables():
    global _table_mod
    global _table_file_open_policy_is_strict
    if _table_mod is None:
        import tables

        _table_mod = tables

        # set the file open policy
        # return the file open policy; this changes as of pytables 3.1
        # depending on the HDF5 version
        try:
            _table_file_open_policy_is_strict = (
                tables.file._FILE_OPEN_POLICY == "strict"
            )
        except AttributeError:
            pass

    return _table_mod


# interface to/from ###


def to_hdf(
    path_or_buf,
    key: str,
    value: FrameOrSeries,
    mode: str = "a",
    complevel: Optional[int] = None,
    complib: Optional[str] = None,
    append: bool = False,
    format: Optional[str] = None,
    errors: str = "strict",
    encoding: str = "UTF-8",
    **kwargs,
):
    """ store this object, close it if we opened it """

    if append:
        f = lambda store: store.append(
            key, value, format=format, errors=errors, encoding=encoding, **kwargs
        )
    else:
        f = lambda store: store.put(
            key, value, format=format, errors=errors, encoding=encoding, **kwargs
        )

    path_or_buf = _stringify_path(path_or_buf)
    if isinstance(path_or_buf, str):
        with HDFStore(
            path_or_buf, mode=mode, complevel=complevel, complib=complib
        ) as store:
            f(store)
    else:
        f(path_or_buf)


def read_hdf(path_or_buf, key=None, mode: str = "r", **kwargs):
    """
    Read from the store, close it if we opened it.

    Retrieve pandas object stored in file, optionally based on where
    criteria

    Parameters
    ----------
    path_or_buf : str, path object, pandas.HDFStore or file-like object
        Any valid string path is acceptable. The string could be a URL. Valid
        URL schemes include http, ftp, s3, and file. For file URLs, a host is
        expected. A local file could be: ``file://localhost/path/to/table.h5``.

        If you want to pass in a path object, pandas accepts any
        ``os.PathLike``.

        Alternatively, pandas accepts an open :class:`pandas.HDFStore` object.

        By file-like object, we refer to objects with a ``read()`` method,
        such as a file handler (e.g. via builtin ``open`` function)
        or ``StringIO``.

        .. versionadded:: 0.21.0 support for __fspath__ protocol.

    key : object, optional
        The group identifier in the store. Can be omitted if the HDF file
        contains a single pandas object.
    mode : {'r', 'r+', 'a'}, optional
        Mode to use when opening the file. Ignored if path_or_buf is a
        :class:`pandas.HDFStore`. Default is 'r'.
    where : list, optional
        A list of Term (or convertible) objects.
    start : int, optional
        Row number to start selection.
    stop  : int, optional
        Row number to stop selection.
    columns : list, optional
        A list of columns names to return.
    iterator : bool, optional
        Return an iterator object.
    chunksize : int, optional
        Number of rows to include in an iteration when using an iterator.
    errors : str, default 'strict'
        Specifies how encoding and decoding errors are to be handled.
        See the errors argument for :func:`open` for a full list
        of options.
    **kwargs
        Additional keyword arguments passed to HDFStore.

    Returns
    -------
    item : object
        The selected object. Return type depends on the object stored.

    See Also
    --------
    DataFrame.to_hdf : Write a HDF file from a DataFrame.
    HDFStore : Low-level access to HDF files.

    Examples
    --------
    >>> df = pd.DataFrame([[1, 1.0, 'a']], columns=['x', 'y', 'z'])
    >>> df.to_hdf('./store.h5', 'data')
    >>> reread = pd.read_hdf('./store.h5')
    """

    if mode not in ["r", "r+", "a"]:
        raise ValueError(
            f"mode {mode} is not allowed while performing a read. "
            f"Allowed modes are r, r+ and a."
        )
    # grab the scope
    if "where" in kwargs:
        kwargs["where"] = _ensure_term(kwargs["where"], scope_level=1)

    if isinstance(path_or_buf, HDFStore):
        if not path_or_buf.is_open:
            raise IOError("The HDFStore must be open for reading.")

        store = path_or_buf
        auto_close = False
    else:
        path_or_buf = _stringify_path(path_or_buf)
        if not isinstance(path_or_buf, str):
            raise NotImplementedError(
                "Support for generic buffers has not been implemented."
            )
        try:
            exists = os.path.exists(path_or_buf)

        # if filepath is too long
        except (TypeError, ValueError):
            exists = False

        if not exists:
            raise FileNotFoundError(f"File {path_or_buf} does not exist")

        store = HDFStore(path_or_buf, mode=mode, **kwargs)
        # can't auto open/close if we are using an iterator
        # so delegate to the iterator
        auto_close = True

    try:
        if key is None:
            groups = store.groups()
            if len(groups) == 0:
                raise ValueError("No dataset in HDF5 file.")
            candidate_only_group = groups[0]

            # For the HDF file to have only one dataset, all other groups
            # should then be metadata groups for that candidate group. (This
            # assumes that the groups() method enumerates parent groups
            # before their children.)
            for group_to_check in groups[1:]:
                if not _is_metadata_of(group_to_check, candidate_only_group):
                    raise ValueError(
                        "key must be provided when HDF5 file "
                        "contains multiple datasets."
                    )
            key = candidate_only_group._v_pathname
        return store.select(key, auto_close=auto_close, **kwargs)
    except (ValueError, TypeError, KeyError):
        if not isinstance(path_or_buf, HDFStore):
            # if there is an error, close the store if we opened it.
            try:
                store.close()
            except AttributeError:
                pass

        raise


def _is_metadata_of(group, parent_group) -> bool:
    """Check if a given group is a metadata group for a given parent_group."""
    if group._v_depth <= parent_group._v_depth:
        return False

    current = group
    while current._v_depth > 1:
        parent = current._v_parent
        if parent == parent_group and current._v_name == "meta":
            return True
        current = current._v_parent
    return False


class HDFStore:
    """
    Dict-like IO interface for storing pandas objects in PyTables.

    Either Fixed or Table format.

    Parameters
    ----------
    path : string
        File path to HDF5 file
    mode : {'a', 'w', 'r', 'r+'}, default 'a'

        ``'r'``
            Read-only; no data can be modified.
        ``'w'``
            Write; a new file is created (an existing file with the same
            name would be deleted).
        ``'a'``
            Append; an existing file is opened for reading and writing,
            and if the file does not exist it is created.
        ``'r+'``
            It is similar to ``'a'``, but the file must already exist.
    complevel : int, 0-9, default None
            Specifies a compression level for data.
            A value of 0 or None disables compression.
    complib : {'zlib', 'lzo', 'bzip2', 'blosc'}, default 'zlib'
            Specifies the compression library to be used.
            As of v0.20.2 these additional compressors for Blosc are supported
            (default if no compressor specified: 'blosc:blosclz'):
            {'blosc:blosclz', 'blosc:lz4', 'blosc:lz4hc', 'blosc:snappy',
             'blosc:zlib', 'blosc:zstd'}.
            Specifying a compression library which is not available issues
            a ValueError.
    fletcher32 : bool, default False
            If applying compression use the fletcher32 checksum

    Examples
    --------
    >>> bar = pd.DataFrame(np.random.randn(10, 4))
    >>> store = pd.HDFStore('test.h5')
    >>> store['foo'] = bar   # write to HDF5
    >>> bar = store['foo']   # retrieve
    >>> store.close()
    """

    _handle: Optional["File"]
    _complevel: int
    _fletcher32: bool

    def __init__(
        self,
        path,
        mode=None,
        complevel: Optional[int] = None,
        complib=None,
        fletcher32: bool = False,
        **kwargs,
    ):

        if "format" in kwargs:
            raise ValueError("format is not a defined argument for HDFStore")

        tables = import_optional_dependency("tables")

        if complib is not None and complib not in tables.filters.all_complibs:
            raise ValueError(
                f"complib only supports {tables.filters.all_complibs} compression."
            )

        if complib is None and complevel is not None:
            complib = tables.filters.default_complib

        self._path = _stringify_path(path)
        if mode is None:
            mode = "a"
        self._mode = mode
        self._handle = None
        self._complevel = complevel if complevel else 0
        self._complib = complib
        self._fletcher32 = fletcher32
        self._filters = None
        self.open(mode=mode, **kwargs)

    def __fspath__(self):
        return self._path

    @property
    def root(self):
        """ return the root node """
        self._check_if_open()
        return self._handle.root

    @property
    def filename(self):
        return self._path

    def __getitem__(self, key: str):
        return self.get(key)

    def __setitem__(self, key: str, value):
        self.put(key, value)

    def __delitem__(self, key: str):
        return self.remove(key)

    def __getattr__(self, name: str):
        """ allow attribute access to get stores """
        try:
            return self.get(name)
        except (KeyError, ClosedFileError):
            pass
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def __contains__(self, key: str) -> bool:
        """ check for existence of this key
              can match the exact pathname or the pathnm w/o the leading '/'
              """
        node = self.get_node(key)
        if node is not None:
            name = node._v_pathname
            if name == key or name[1:] == key:
                return True
        return False

    def __len__(self) -> int:
        return len(self.groups())

    def __repr__(self) -> str:
        pstr = pprint_thing(self._path)
        return f"{type(self)}\nFile path: {pstr}\n"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def keys(self) -> List[str]:
        """
        Return a list of keys corresponding to objects stored in HDFStore.

        Returns
        -------
        list
            List of ABSOLUTE path-names (e.g. have the leading '/').
        """
        return [n._v_pathname for n in self.groups()]

    def __iter__(self):
        return iter(self.keys())

    def items(self):
        """
        iterate on key->group
        """
        for g in self.groups():
            yield g._v_pathname, g

    iteritems = items

    def open(self, mode: str = "a", **kwargs):
        """
        Open the file in the specified mode

        Parameters
        ----------
        mode : {'a', 'w', 'r', 'r+'}, default 'a'
            See HDFStore docstring or tables.open_file for info about modes
        """
        tables = _tables()

        if self._mode != mode:

            # if we are changing a write mode to read, ok
            if self._mode in ["a", "w"] and mode in ["r", "r+"]:
                pass
            elif mode in ["w"]:

                # this would truncate, raise here
                if self.is_open:
                    raise PossibleDataLossError(
                        f"Re-opening the file [{self._path}] with mode [{self._mode}] "
                        "will delete the current file!"
                    )

            self._mode = mode

        # close and reopen the handle
        if self.is_open:
            self.close()

        if self._complevel and self._complevel > 0:
            self._filters = _tables().Filters(
                self._complevel, self._complib, fletcher32=self._fletcher32
            )

        try:
            self._handle = tables.open_file(self._path, self._mode, **kwargs)
        except IOError as err:  # pragma: no cover
            if "can not be written" in str(err):
                print(f"Opening {self._path} in read-only mode")
                self._handle = tables.open_file(self._path, "r", **kwargs)
            else:
                raise

        except ValueError as err:

            # trap PyTables >= 3.1 FILE_OPEN_POLICY exception
            # to provide an updated message
            if "FILE_OPEN_POLICY" in str(err):
                hdf_version = tables.get_hdf5_version()
                err = ValueError(
                    f"PyTables [{tables.__version__}] no longer supports "
                    "opening multiple files\n"
                    "even in read-only mode on this HDF5 version "
                    f"[{hdf_version}]. You can accept this\n"
                    "and not open the same file multiple times at once,\n"
                    "upgrade the HDF5 version, or downgrade to PyTables 3.0.0 "
                    "which allows\n"
                    "files to be opened multiple times at once\n"
                )

            raise err

        except Exception as err:

            # trying to read from a non-existent file causes an error which
            # is not part of IOError, make it one
            if self._mode == "r" and "Unable to open/create file" in str(err):
                raise IOError(str(err))
            raise

    def close(self):
        """
        Close the PyTables file handle
        """
        if self._handle is not None:
            self._handle.close()
        self._handle = None

    @property
    def is_open(self) -> bool:
        """
        return a boolean indicating whether the file is open
        """
        if self._handle is None:
            return False
        return bool(self._handle.isopen)

    def flush(self, fsync: bool = False):
        """
        Force all buffered modifications to be written to disk.

        Parameters
        ----------
        fsync : bool (default False)
          call ``os.fsync()`` on the file handle to force writing to disk.

        Notes
        -----
        Without ``fsync=True``, flushing may not guarantee that the OS writes
        to disk. With fsync, the operation will block until the OS claims the
        file has been written; however, other caching layers may still
        interfere.
        """
        if self._handle is not None:
            self._handle.flush()
            if fsync:
                try:
                    os.fsync(self._handle.fileno())
                except OSError:
                    pass

    def get(self, key: str):
        """
        Retrieve pandas object stored in file.

        Parameters
        ----------
        key : str

        Returns
        -------
        object
            Same type as object stored in file.
        """
        group = self.get_node(key)
        if group is None:
            raise KeyError(f"No object named {key} in the file")
        return self._read_group(group)

    def select(
        self,
        key: str,
        where=None,
        start=None,
        stop=None,
        columns=None,
        iterator=False,
        chunksize=None,
        auto_close: bool = False,
        **kwargs,
    ):
        """
        Retrieve pandas object stored in file, optionally based on where criteria.

        Parameters
        ----------
        key : str
                Object being retrieved from file.
        where : list, default None
                List of Term (or convertible) objects, optional.
        start : int, default None
                Row number to start selection.
        stop : int, default None
                Row number to stop selection.
        columns : list, default None
                A list of columns that if not None, will limit the return columns.
        iterator : bool, default False
                Returns an iterator.
        chunksize : int, default None
                Number or rows to include in iteration, return an iterator.
        auto_close : bool, default False
            Should automatically close the store when finished.

        Returns
        -------
        object
            Retrieved object from file.
        """
        group = self.get_node(key)
        if group is None:
            raise KeyError(f"No object named {key} in the file")

        # create the storer and axes
        where = _ensure_term(where, scope_level=1)
        s = self._create_storer(group)
        s.infer_axes()

        # function to call on iteration
        def func(_start, _stop, _where):
            return s.read(start=_start, stop=_stop, where=_where, columns=columns)

        # create the iterator
        it = TableIterator(
            self,
            s,
            func,
            where=where,
            nrows=s.nrows,
            start=start,
            stop=stop,
            iterator=iterator,
            chunksize=chunksize,
            auto_close=auto_close,
        )

        return it.get_result()

    def select_as_coordinates(
        self,
        key: str,
        where=None,
        start: Optional[int] = None,
        stop: Optional[int] = None,
    ):
        """
        return the selection as an Index

        Parameters
        ----------
        key : str
        where : list of Term (or convertible) objects, optional
        start : integer (defaults to None), row number to start selection
        stop  : integer (defaults to None), row number to stop selection
        """
        where = _ensure_term(where, scope_level=1)
        tbl = self.get_storer(key)
        if not isinstance(tbl, Table):
            raise TypeError("can only read_coordinates with a table")
        return tbl.read_coordinates(where=where, start=start, stop=stop)

    def select_column(self, key: str, column: str, **kwargs):
        """
        return a single column from the table. This is generally only useful to
        select an indexable

        Parameters
        ----------
        key : str
        column: str
            The column of interest.

        Raises
        ------
        raises KeyError if the column is not found (or key is not a valid
            store)
        raises ValueError if the column can not be extracted individually (it
            is part of a data block)

        """
        tbl = self.get_storer(key)
        if not isinstance(tbl, Table):
            raise TypeError("can only read_column with a table")
        return tbl.read_column(column=column, **kwargs)

    def select_as_multiple(
        self,
        keys,
        where=None,
        selector=None,
        columns=None,
        start=None,
        stop=None,
        iterator=False,
        chunksize=None,
        auto_close: bool = False,
        **kwargs,
    ):
        """
        Retrieve pandas objects from multiple tables.

        Parameters
        ----------
        keys : a list of the tables
        selector : the table to apply the where criteria (defaults to keys[0]
            if not supplied)
        columns : the columns I want back
        start : integer (defaults to None), row number to start selection
        stop  : integer (defaults to None), row number to stop selection
        iterator : boolean, return an iterator, default False
        chunksize : nrows to include in iteration, return an iterator
        auto_close : bool, default False
            Should automatically close the store when finished.

        Raises
        ------
        raises KeyError if keys or selector is not found or keys is empty
        raises TypeError if keys is not a list or tuple
        raises ValueError if the tables are not ALL THE SAME DIMENSIONS
        """

        # default to single select
        where = _ensure_term(where, scope_level=1)
        if isinstance(keys, (list, tuple)) and len(keys) == 1:
            keys = keys[0]
        if isinstance(keys, str):
            return self.select(
                key=keys,
                where=where,
                columns=columns,
                start=start,
                stop=stop,
                iterator=iterator,
                chunksize=chunksize,
                **kwargs,
            )

        if not isinstance(keys, (list, tuple)):
            raise TypeError("keys must be a list/tuple")

        if not len(keys):
            raise ValueError("keys must have a non-zero length")

        if selector is None:
            selector = keys[0]

        # collect the tables
        tbls = [self.get_storer(k) for k in keys]
        s = self.get_storer(selector)

        # validate rows
        nrows = None
        for t, k in itertools.chain([(s, selector)], zip(tbls, keys)):
            if t is None:
                raise KeyError(f"Invalid table [{k}]")
            if not t.is_table:
                raise TypeError(
                    f"object [{t.pathname}] is not a table, and cannot be used in all "
                    "select as multiple"
                )

            if nrows is None:
                nrows = t.nrows
            elif t.nrows != nrows:
                raise ValueError("all tables must have exactly the same nrows!")

        # The isinstance checks here are redundant with the check above,
        #  but necessary for mypy; see GH#29757
        _tbls = [x for x in tbls if isinstance(x, Table)]

        # axis is the concentration axes
        axis = list({t.non_index_axes[0][0] for t in _tbls})[0]

        def func(_start, _stop, _where):

            # retrieve the objs, _where is always passed as a set of
            # coordinates here
            objs = [
                t.read(
                    where=_where, columns=columns, start=_start, stop=_stop, **kwargs
                )
                for t in tbls
            ]

            # concat and return
            return concat(objs, axis=axis, verify_integrity=False)._consolidate()

        # create the iterator
        it = TableIterator(
            self,
            s,
            func,
            where=where,
            nrows=nrows,
            start=start,
            stop=stop,
            iterator=iterator,
            chunksize=chunksize,
            auto_close=auto_close,
        )

        return it.get_result(coordinates=True)

    def put(self, key: str, value, format=None, append=False, **kwargs):
        """
        Store object in HDFStore.

        Parameters
        ----------
        key : str
        value : {Series, DataFrame}
        format : 'fixed(f)|table(t)', default is 'fixed'
            fixed(f) : Fixed format
                       Fast writing/reading. Not-appendable, nor searchable.
            table(t) : Table format
                       Write as a PyTables Table structure which may perform
                       worse but allow more flexible operations like searching
                       / selecting subsets of the data.
        append   : bool, default False
            This will force Table format, append the input data to the
            existing.
        data_columns : list, default None
            List of columns to create as data columns, or True to
            use all columns. See `here
            <http://pandas.pydata.org/pandas-docs/stable/user_guide/io.html#query-via-data-columns>`__.
        encoding : str, default None
            Provide an encoding for strings.
        dropna   : bool, default False, do not write an ALL nan row to
            The store settable by the option 'io.hdf.dropna_table'.
        """
        if format is None:
            format = get_option("io.hdf.default_format") or "fixed"
        kwargs = self._validate_format(format, kwargs)
        self._write_to_group(key, value, append=append, **kwargs)

    def remove(self, key: str, where=None, start=None, stop=None):
        """
        Remove pandas object partially by specifying the where condition

        Parameters
        ----------
        key : string
            Node to remove or delete rows from
        where : list of Term (or convertible) objects, optional
        start : integer (defaults to None), row number to start selection
        stop  : integer (defaults to None), row number to stop selection

        Returns
        -------
        number of rows removed (or None if not a Table)

        Raises
        ------
        raises KeyError if key is not a valid store

        """
        where = _ensure_term(where, scope_level=1)
        try:
            s = self.get_storer(key)
        except KeyError:
            # the key is not a valid store, re-raising KeyError
            raise
        except Exception:
            # In tests we get here with ClosedFileError, TypeError, and
            #  _table_mod.NoSuchNodeError.  TODO: Catch only these?

            if where is not None:
                raise ValueError(
                    "trying to remove a node with a non-None where clause!"
                )

            # we are actually trying to remove a node (with children)
            node = self.get_node(key)
            if node is not None:
                node._f_remove(recursive=True)
                return None

        # remove the node
        if com.all_none(where, start, stop):
            s.group._f_remove(recursive=True)

        # delete from the table
        else:
            if not s.is_table:
                raise ValueError(
                    "can only remove with where on objects written as tables"
                )
            return s.delete(where=where, start=start, stop=stop)

    def append(
        self,
        key: str,
        value,
        format=None,
        append=True,
        columns=None,
        dropna: Optional[bool] = None,
        **kwargs,
    ):
        """
        Append to Table in file. Node must already exist and be Table
        format.

        Parameters
        ----------
        key : str
        value : {Series, DataFrame}
        format : 'table' is the default
            table(t) : table format
                       Write as a PyTables Table structure which may perform
                       worse but allow more flexible operations like searching
                       / selecting subsets of the data.
        append       : bool, default True
            Append the input data to the existing.
        data_columns : list of columns, or True, default None
            List of columns to create as indexed data columns for on-disk
            queries, or True to use all columns. By default only the axes
            of the object are indexed. See `here
            <http://pandas.pydata.org/pandas-docs/stable/user_guide/io.html#query-via-data-columns>`__.
        min_itemsize : dict of columns that specify minimum string sizes
        nan_rep      : string to use as string nan representation
        chunksize    : size to chunk the writing
        expectedrows : expected TOTAL row size of this table
        encoding     : default None, provide an encoding for strings
        dropna : bool, default False
            Do not write an ALL nan row to the store settable
            by the option 'io.hdf.dropna_table'.

        Notes
        -----
        Does *not* check if data being appended overlaps with existing
        data in the table, so be careful
        """
        if columns is not None:
            raise TypeError(
                "columns is not a supported keyword in append, try data_columns"
            )

        if dropna is None:
            dropna = get_option("io.hdf.dropna_table")
        if format is None:
            format = get_option("io.hdf.default_format") or "table"
        kwargs = self._validate_format(format, kwargs)
        self._write_to_group(key, value, append=append, dropna=dropna, **kwargs)

    def append_to_multiple(
        self,
        d: Dict,
        value,
        selector,
        data_columns=None,
        axes=None,
        dropna=False,
        **kwargs,
    ):
        """
        Append to multiple tables

        Parameters
        ----------
        d : a dict of table_name to table_columns, None is acceptable as the
            values of one node (this will get all the remaining columns)
        value : a pandas object
        selector : a string that designates the indexable table; all of its
            columns will be designed as data_columns, unless data_columns is
            passed, in which case these are used
        data_columns : list of columns to create as data columns, or True to
            use all columns
        dropna : if evaluates to True, drop rows from all tables if any single
                 row in each table has all NaN. Default False.

        Notes
        -----
        axes parameter is currently not accepted

        """
        if axes is not None:
            raise TypeError(
                "axes is currently not accepted as a parameter to"
                " append_to_multiple; you can create the "
                "tables independently instead"
            )

        if not isinstance(d, dict):
            raise ValueError(
                "append_to_multiple must have a dictionary specified as the "
                "way to split the value"
            )

        if selector not in d:
            raise ValueError(
                "append_to_multiple requires a selector that is in passed dict"
            )

        # figure out the splitting axis (the non_index_axis)
        axis = list(set(range(value.ndim)) - set(_AXES_MAP[type(value)]))[0]

        # figure out how to split the value
        remain_key = None
        remain_values: List = []
        for k, v in d.items():
            if v is None:
                if remain_key is not None:
                    raise ValueError(
                        "append_to_multiple can only have one value in d that "
                        "is None"
                    )
                remain_key = k
            else:
                remain_values.extend(v)
        if remain_key is not None:
            ordered = value.axes[axis]
            ordd = ordered.difference(Index(remain_values))
            ordd = sorted(ordered.get_indexer(ordd))
            d[remain_key] = ordered.take(ordd)

        # data_columns
        if data_columns is None:
            data_columns = d[selector]

        # ensure rows are synchronized across the tables
        if dropna:
            idxs = (value[cols].dropna(how="all").index for cols in d.values())
            valid_index = next(idxs)
            for index in idxs:
                valid_index = valid_index.intersection(index)
            value = value.loc[valid_index]

        # append
        for k, v in d.items():
            dc = data_columns if k == selector else None

            # compute the val
            val = value.reindex(v, axis=axis)

            self.append(k, val, data_columns=dc, **kwargs)

    def create_table_index(self, key: str, **kwargs):
        """
        Create a pytables index on the table.

        Parameters
        ----------
        key : str

        Raises
        ------
        TypeError: raises if the node is not a table
        """

        # version requirements
        _tables()
        s = self.get_storer(key)
        if s is None:
            return

        if not isinstance(s, Table):
            raise TypeError("cannot create table index on a Fixed format store")
        s.create_index(**kwargs)

    def groups(self):
        """
        Return a list of all the top-level nodes.

        Each node returned is not a pandas storage object.

        Returns
        -------
        list
            List of objects.
        """
        _tables()
        self._check_if_open()
        return [
            g
            for g in self._handle.walk_groups()
            if (
                not isinstance(g, _table_mod.link.Link)
                and (
                    getattr(g._v_attrs, "pandas_type", None)
                    or getattr(g, "table", None)
                    or (isinstance(g, _table_mod.table.Table) and g._v_name != "table")
                )
            )
        ]

    def walk(self, where="/"):
        """
        Walk the pytables group hierarchy for pandas objects.

        This generator will yield the group path, subgroups and pandas object
        names for each group.

        Any non-pandas PyTables objects that are not a group will be ignored.

        The `where` group itself is listed first (preorder), then each of its
        child groups (following an alphanumerical order) is also traversed,
        following the same procedure.

        .. versionadded:: 0.24.0

        Parameters
        ----------
        where : str, default "/"
            Group where to start walking.

        Yields
        ------
        path : str
            Full path to a group (without trailing '/').
        groups : list
            Names (strings) of the groups contained in `path`.
        leaves : list
            Names (strings) of the pandas objects contained in `path`.
        """
        _tables()
        self._check_if_open()
        for g in self._handle.walk_groups(where):
            if getattr(g._v_attrs, "pandas_type", None) is not None:
                continue

            groups = []
            leaves = []
            for child in g._v_children.values():
                pandas_type = getattr(child._v_attrs, "pandas_type", None)
                if pandas_type is None:
                    if isinstance(child, _table_mod.group.Group):
                        groups.append(child._v_name)
                else:
                    leaves.append(child._v_name)

            yield (g._v_pathname.rstrip("/"), groups, leaves)

    def get_node(self, key: str) -> Optional["Node"]:
        """ return the node with the key or None if it does not exist """
        self._check_if_open()
        if not key.startswith("/"):
            key = "/" + key

        assert self._handle is not None
        assert _table_mod is not None  # for mypy
        try:
            node = self._handle.get_node(self.root, key)
        except _table_mod.exceptions.NoSuchNodeError:
            return None

        assert isinstance(node, _table_mod.Node), type(node)
        return node

    def get_storer(self, key: str) -> Union["GenericFixed", "Table"]:
        """ return the storer object for a key, raise if not in the file """
        group = self.get_node(key)
        if group is None:
            raise KeyError(f"No object named {key} in the file")

        s = self._create_storer(group)
        s.infer_axes()
        return s

    def copy(
        self,
        file,
        mode="w",
        propindexes: bool = True,
        keys=None,
        complib=None,
        complevel: Optional[int] = None,
        fletcher32: bool = False,
        overwrite=True,
    ):
        """
        Copy the existing store to a new file, updating in place.

        Parameters
        ----------
        propindexes: bool, default True
            Restore indexes in copied file.
        keys       : list of keys to include in the copy (defaults to all)
        overwrite  : overwrite (remove and replace) existing nodes in the
            new store (default is True)
        mode, complib, complevel, fletcher32 same as in HDFStore.__init__

        Returns
        -------
        open file handle of the new store
        """
        new_store = HDFStore(
            file, mode=mode, complib=complib, complevel=complevel, fletcher32=fletcher32
        )
        if keys is None:
            keys = list(self.keys())
        if not isinstance(keys, (tuple, list)):
            keys = [keys]
        for k in keys:
            s = self.get_storer(k)
            if s is not None:

                if k in new_store:
                    if overwrite:
                        new_store.remove(k)

                data = self.select(k)
                if isinstance(s, Table):

                    index: Union[bool, list] = False
                    if propindexes:
                        index = [a.name for a in s.axes if a.is_indexed]
                    new_store.append(
                        k,
                        data,
                        index=index,
                        data_columns=getattr(s, "data_columns", None),
                        encoding=s.encoding,
                    )
                else:
                    new_store.put(k, data, encoding=s.encoding)

        return new_store

    def info(self) -> str:
        """
        Print detailed information on the store.

        .. versionadded:: 0.21.0

        Returns
        -------
        str
        """
        path = pprint_thing(self._path)
        output = f"{type(self)}\nFile path: {path}\n"

        if self.is_open:
            lkeys = sorted(self.keys())
            if len(lkeys):
                keys = []
                values = []

                for k in lkeys:
                    try:
                        s = self.get_storer(k)
                        if s is not None:
                            keys.append(pprint_thing(s.pathname or k))
                            values.append(pprint_thing(s or "invalid_HDFStore node"))
                    except Exception as detail:
                        keys.append(k)
                        dstr = pprint_thing(detail)
                        values.append(f"[invalid_HDFStore node: {dstr}]")

                output += adjoin(12, keys, values)
            else:
                output += "Empty"
        else:
            output += "File is CLOSED"

        return output

    # ------------------------------------------------------------------------
    # private methods

    def _check_if_open(self):
        if not self.is_open:
            raise ClosedFileError(f"{self._path} file is not open!")

    def _validate_format(self, format: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """ validate / deprecate formats; return the new kwargs """
        kwargs = kwargs.copy()

        # validate
        try:
            kwargs["format"] = _FORMAT_MAP[format.lower()]
        except KeyError:
            raise TypeError(f"invalid HDFStore format specified [{format}]")

        return kwargs

    def _create_storer(
        self,
        group,
        format=None,
        value=None,
        encoding: str = "UTF-8",
        errors: str = "strict",
    ) -> Union["GenericFixed", "Table"]:
        """ return a suitable class to operate """

        def error(t):
            # return instead of raising so mypy can tell where we are raising
            return TypeError(
                f"cannot properly create the storer for: [{t}] [group->"
                f"{group},value->{type(value)},format->{format}"
            )

        pt = _ensure_decoded(getattr(group._v_attrs, "pandas_type", None))
        tt = _ensure_decoded(getattr(group._v_attrs, "table_type", None))

        # infer the pt from the passed value
        if pt is None:
            if value is None:

                _tables()
                assert _table_mod is not None  # for mypy
                if getattr(group, "table", None) or isinstance(
                    group, _table_mod.table.Table
                ):
                    pt = "frame_table"
                    tt = "generic_table"
                else:
                    raise TypeError(
                        "cannot create a storer if the object is not existing "
                        "nor a value are passed"
                    )
            else:
                _TYPE_MAP = {Series: "series", DataFrame: "frame"}
                try:
                    pt = _TYPE_MAP[type(value)]
                except KeyError:
                    raise error("_TYPE_MAP")

                # we are actually a table
                if format == "table":
                    pt += "_table"

        # a storer node
        if "table" not in pt:
            try:
                return globals()[_STORER_MAP[pt]](
                    self, group, encoding=encoding, errors=errors
                )
            except KeyError:
                raise error("_STORER_MAP")

        # existing node (and must be a table)
        if tt is None:

            # if we are a writer, determine the tt
            if value is not None:

                if pt == "series_table":
                    index = getattr(value, "index", None)
                    if index is not None:
                        if index.nlevels == 1:
                            tt = "appendable_series"
                        elif index.nlevels > 1:
                            tt = "appendable_multiseries"
                elif pt == "frame_table":
                    index = getattr(value, "index", None)
                    if index is not None:
                        if index.nlevels == 1:
                            tt = "appendable_frame"
                        elif index.nlevels > 1:
                            tt = "appendable_multiframe"
                elif pt == "wide_table":
                    tt = "appendable_panel"
                elif pt == "ndim_table":
                    tt = "appendable_ndim"

            else:

                # distinguish between a frame/table
                tt = "legacy_panel"
                try:
                    fields = group.table._v_attrs.fields
                    if len(fields) == 1 and fields[0] == "value":
                        tt = "legacy_frame"
                except IndexError:
                    pass

        try:
            return globals()[_TABLE_MAP[tt]](
                self, group, encoding=encoding, errors=errors
            )
        except KeyError:
            raise error("_TABLE_MAP")

    def _write_to_group(
        self,
        key: str,
        value,
        format,
        axes=None,
        index=True,
        append=False,
        complib=None,
        complevel: Optional[int] = None,
        fletcher32=None,
        min_itemsize=None,
        chunksize=None,
        expectedrows=None,
        dropna=False,
        nan_rep=None,
        data_columns=None,
        encoding=None,
        errors: str = "strict",
    ):
        group = self.get_node(key)

        # we make this assertion for mypy; the get_node call will already
        #  have raised if this is incorrect
        assert self._handle is not None

        # remove the node if we are not appending
        if group is not None and not append:
            self._handle.remove_node(group, recursive=True)
            group = None

        # we don't want to store a table node at all if are object is 0-len
        # as there are not dtypes
        if getattr(value, "empty", None) and (format == "table" or append):
            return

        if group is None:
            paths = key.split("/")

            # recursively create the groups
            path = "/"
            for p in paths:
                if not len(p):
                    continue
                new_path = path
                if not path.endswith("/"):
                    new_path += "/"
                new_path += p
                group = self.get_node(new_path)
                if group is None:
                    group = self._handle.create_group(path, p)
                path = new_path

        s = self._create_storer(group, format, value, encoding=encoding, errors=errors)
        if append:
            # raise if we are trying to append to a Fixed format,
            #       or a table that exists (and we are putting)
            if not s.is_table or (s.is_table and format == "fixed" and s.is_exists):
                raise ValueError("Can only append to Tables")
            if not s.is_exists:
                s.set_object_info()
        else:
            s.set_object_info()

        if not s.is_table and complib:
            raise ValueError("Compression not supported on Fixed format stores")

        # write the object
        s.write(
            obj=value,
            axes=axes,
            append=append,
            complib=complib,
            complevel=complevel,
            fletcher32=fletcher32,
            min_itemsize=min_itemsize,
            chunksize=chunksize,
            expectedrows=expectedrows,
            dropna=dropna,
            nan_rep=nan_rep,
            data_columns=data_columns,
        )

        if isinstance(s, Table) and index:
            s.create_index(columns=index)

    def _read_group(self, group: "Node", **kwargs):
        s = self._create_storer(group)
        s.infer_axes()
        return s.read(**kwargs)


class TableIterator:
    """ define the iteration interface on a table

        Parameters
        ----------

        store : the reference store
        s     : the referred storer
        func  : the function to execute the query
        where : the where of the query
        nrows : the rows to iterate on
        start : the passed start value (default is None)
        stop  : the passed stop value (default is None)
        iterator : bool, default False
            Whether to use the default iterator.
        chunksize : the passed chunking value (default is 100000)
        auto_close : boolean, automatically close the store at the end of
            iteration, default is False
        """

    chunksize: Optional[int]
    store: HDFStore
    s: Union["GenericFixed", "Table"]

    def __init__(
        self,
        store: HDFStore,
        s: Union["GenericFixed", "Table"],
        func,
        where,
        nrows,
        start=None,
        stop=None,
        iterator: bool = False,
        chunksize: Optional[int] = None,
        auto_close: bool = False,
    ):
        self.store = store
        self.s = s
        self.func = func
        self.where = where

        # set start/stop if they are not set if we are a table
        if self.s.is_table:
            if nrows is None:
                nrows = 0
            if start is None:
                start = 0
            if stop is None:
                stop = nrows
            stop = min(nrows, stop)

        self.nrows = nrows
        self.start = start
        self.stop = stop

        self.coordinates = None
        if iterator or chunksize is not None:
            if chunksize is None:
                chunksize = 100000
            self.chunksize = int(chunksize)
        else:
            self.chunksize = None

        self.auto_close = auto_close

    def __iter__(self):

        # iterate
        current = self.start
        while current < self.stop:

            stop = min(current + self.chunksize, self.stop)
            value = self.func(None, None, self.coordinates[current:stop])
            current = stop
            if value is None or not len(value):
                continue

            yield value

        self.close()

    def close(self):
        if self.auto_close:
            self.store.close()

    def get_result(self, coordinates: bool = False):

        #  return the actual iterator
        if self.chunksize is not None:
            if not isinstance(self.s, Table):
                raise TypeError("can only use an iterator or chunksize on a table")

            self.coordinates = self.s.read_coordinates(where=self.where)

            return self

        # if specified read via coordinates (necessary for multiple selections
        if coordinates:
            if not isinstance(self.s, Table):
                raise TypeError("can only read_coordinates on a table")
            where = self.s.read_coordinates(
                where=self.where, start=self.start, stop=self.stop
            )
        else:
            where = self.where

        # directly return the result
        results = self.func(self.start, self.stop, where)
        self.close()
        return results


class IndexCol:
    """ an index column description class

        Parameters
        ----------

        axis   : axis which I reference
        values : the ndarray like converted values
        kind   : a string description of this type
        typ    : the pytables type
        pos    : the position in the pytables

        """

    is_an_indexable = True
    is_data_indexable = True
    _info_fields = ["freq", "tz", "index_name"]

    name: str
    cname: str

    def __init__(
        self,
        name: str,
        values=None,
        kind=None,
        typ=None,
        cname: Optional[str] = None,
        itemsize=None,
        axis=None,
        pos=None,
        freq=None,
        tz=None,
        index_name=None,
    ):

        if not isinstance(name, str):
            raise ValueError("`name` must be a str.")

        self.values = values
        self.kind = kind
        self.typ = typ
        self.itemsize = itemsize
        self.name = name
        self.cname = cname or name
        self.axis = axis
        self.pos = pos
        self.freq = freq
        self.tz = tz
        self.index_name = index_name
        self.table = None
        self.meta = None
        self.metadata = None

        if pos is not None:
            self.set_pos(pos)

        # These are ensured as long as the passed arguments match the
        #  constructor annotations.
        assert isinstance(self.name, str)
        assert isinstance(self.cname, str)

    @property
    def kind_attr(self) -> str:
        return f"{self.name}_kind"

    def set_pos(self, pos: int):
        """ set the position of this column in the Table """
        self.pos = pos
        if pos is not None and self.typ is not None:
            self.typ._v_pos = pos

    def __repr__(self) -> str:
        temp = tuple(
            map(pprint_thing, (self.name, self.cname, self.axis, self.pos, self.kind))
        )
        return ",".join(
            (
                f"{key}->{value}"
                for key, value in zip(["name", "cname", "axis", "pos", "kind"], temp)
            )
        )

    def __eq__(self, other: Any) -> bool:
        """ compare 2 col items """
        return all(
            getattr(self, a, None) == getattr(other, a, None)
            for a in ["name", "cname", "axis", "pos"]
        )

    def __ne__(self, other) -> bool:
        return not self.__eq__(other)

    @property
    def is_indexed(self) -> bool:
        """ return whether I am an indexed column """
        if not hasattr(self.table, "cols"):
            # e.g. if infer hasn't been called yet, self.table will be None.
            return False
        # GH#29692 mypy doesn't recognize self.table as having a "cols" attribute
        #  'error: "None" has no attribute "cols"'
        return getattr(self.table.cols, self.cname).is_indexed  # type: ignore

    def copy(self):
        new_self = copy.copy(self)
        return new_self

    def infer(self, handler: "Table"):
        """infer this column from the table: create and return a new object"""
        table = handler.table
        new_self = self.copy()
        new_self.table = table
        new_self.get_attr()
        new_self.read_metadata(handler)
        return new_self

    def convert(
        self, values: np.ndarray, nan_rep, encoding, errors, start=None, stop=None
    ):
        """ set the values from this selection: take = take ownership """

        # values is a recarray
        if values.dtype.fields is not None:
            values = values[self.cname]

        values = _maybe_convert(values, self.kind, encoding, errors)

        kwargs = dict()
        if self.freq is not None:
            kwargs["freq"] = _ensure_decoded(self.freq)
        if self.index_name is not None:
            kwargs["name"] = _ensure_decoded(self.index_name)
        # making an Index instance could throw a number of different errors
        try:
            self.values = Index(values, **kwargs)
        except ValueError:
            # if the output freq is different that what we recorded,
            # it should be None (see also 'doc example part 2')
            if "freq" in kwargs:
                kwargs["freq"] = None
            self.values = Index(values, **kwargs)

        self.values = _set_tz(self.values, self.tz)

    def take_data(self):
        """ return the values & release the memory """
        self.values, values = None, self.values
        return values

    @property
    def attrs(self):
        return self.table._v_attrs

    @property
    def description(self):
        return self.table.description

    @property
    def col(self):
        """ return my current col description """
        return getattr(self.description, self.cname, None)

    @property
    def cvalues(self):
        """ return my cython values """
        return self.values

    def __iter__(self):
        return iter(self.values)

    def maybe_set_size(self, min_itemsize=None):
        """ maybe set a string col itemsize:
               min_itemsize can be an integer or a dict with this columns name
               with an integer size """
        if _ensure_decoded(self.kind) == "string":

            if isinstance(min_itemsize, dict):
                min_itemsize = min_itemsize.get(self.name)

            if min_itemsize is not None and self.typ.itemsize < min_itemsize:
                self.typ = _tables().StringCol(itemsize=min_itemsize, pos=self.pos)

    def validate(self, handler, append):
        self.validate_names()

    def validate_names(self):
        pass

    def validate_and_set(self, handler: "AppendableTable", append: bool):
        self.table = handler.table
        self.validate_col()
        self.validate_attr(append)
        self.validate_metadata(handler)
        self.write_metadata(handler)
        self.set_attr()

    def validate_col(self, itemsize=None):
        """ validate this column: return the compared against itemsize """

        # validate this column for string truncation (or reset to the max size)
        if _ensure_decoded(self.kind) == "string":
            c = self.col
            if c is not None:
                if itemsize is None:
                    itemsize = self.itemsize
                if c.itemsize < itemsize:
                    raise ValueError(
                        f"Trying to store a string with len [{itemsize}] in "
                        f"[{self.cname}] column but\nthis column has a limit of "
                        f"[{c.itemsize}]!\nConsider using min_itemsize to "
                        "preset the sizes on these columns"
                    )
                return c.itemsize

        return None

    def validate_attr(self, append: bool):
        # check for backwards incompatibility
        if append:
            existing_kind = getattr(self.attrs, self.kind_attr, None)
            if existing_kind is not None and existing_kind != self.kind:
                raise TypeError(
                    f"incompatible kind in col [{existing_kind} - {self.kind}]"
                )

    def update_info(self, info):
        """ set/update the info for this indexable with the key/value
            if there is a conflict raise/warn as needed """

        for key in self._info_fields:

            value = getattr(self, key, None)
            idx = _get_info(info, self.name)

            existing_value = idx.get(key)
            if key in idx and value is not None and existing_value != value:

                # frequency/name just warn
                if key in ["freq", "index_name"]:
                    ws = attribute_conflict_doc % (key, existing_value, value)
                    warnings.warn(ws, AttributeConflictWarning, stacklevel=6)

                    # reset
                    idx[key] = None
                    setattr(self, key, None)

                else:
                    raise ValueError(
                        f"invalid info for [{self.name}] for [{key}], "
                        f"existing_value [{existing_value}] conflicts with "
                        f"new value [{value}]"
                    )
            else:
                if value is not None or existing_value is not None:
                    idx[key] = value

    def set_info(self, info):
        """ set my state from the passed info """
        idx = info.get(self.name)
        if idx is not None:
            self.__dict__.update(idx)

    def get_attr(self):
        """ set the kind for this column """
        self.kind = getattr(self.attrs, self.kind_attr, None)

    def set_attr(self):
        """ set the kind for this column """
        setattr(self.attrs, self.kind_attr, self.kind)

    def read_metadata(self, handler):
        """ retrieve the metadata for this columns """
        self.metadata = handler.read_metadata(self.cname)

    def validate_metadata(self, handler: "AppendableTable"):
        """ validate that kind=category does not change the categories """
        if self.meta == "category":
            new_metadata = self.metadata
            cur_metadata = handler.read_metadata(self.cname)
            if (
                new_metadata is not None
                and cur_metadata is not None
                and not array_equivalent(new_metadata, cur_metadata)
            ):
                raise ValueError(
                    "cannot append a categorical with "
                    "different categories to the existing"
                )

    def write_metadata(self, handler: "AppendableTable"):
        """ set the meta data """
        if self.metadata is not None:
            handler.write_metadata(self.cname, self.metadata)


class GenericIndexCol(IndexCol):
    """ an index which is not represented in the data of the table """

    @property
    def is_indexed(self) -> bool:
        return False

    def convert(
        self,
        values,
        nan_rep,
        encoding,
        errors,
        start: Optional[int] = None,
        stop: Optional[int] = None,
    ):
        """ set the values from this selection: take = take ownership

        Parameters
        ----------

        values : np.ndarray
        nan_rep : str
        encoding : str
        errors : str
        start : int, optional
            Table row number: the start of the sub-selection.
        stop : int, optional
            Table row number: the end of the sub-selection. Values larger than
            the underlying table's row count are normalized to that.
        """
        assert self.table is not None  # for mypy

        _start = start if start is not None else 0
        _stop = min(stop, self.table.nrows) if stop is not None else self.table.nrows
        self.values = Int64Index(np.arange(_stop - _start))

    def get_attr(self):
        pass

    def set_attr(self):
        pass


class DataCol(IndexCol):
    """ a data holding column, by definition this is not indexable

        Parameters
        ----------

        data   : the actual data
        cname  : the column name in the table to hold the data (typically
                 values)
        meta   : a string description of the metadata
        metadata : the actual metadata
        """

    is_an_indexable = False
    is_data_indexable = False
    _info_fields = ["tz", "ordered"]

    @classmethod
    def create_for_block(
        cls, i: int, name=None, version=None, pos: Optional[int] = None
    ):
        """ return a new datacol with the block i """

        cname = name or f"values_block_{i}"
        if name is None:
            name = cname

        # prior to 0.10.1, we named values blocks like: values_block_0 an the
        # name values_0
        try:
            if version[0] == 0 and version[1] <= 10 and version[2] == 0:
                m = re.search(r"values_block_(\d+)", name)
                if m:
                    grp = m.groups()[0]
                    name = f"values_{grp}"
        except IndexError:
            pass

        return cls(name=name, cname=cname, pos=pos)

    def __init__(
        self, name: str, values=None, kind=None, typ=None, cname=None, pos=None,
    ):
        super().__init__(
            name=name, values=values, kind=kind, typ=typ, pos=pos, cname=cname
        )
        self.dtype = None
        self.data = None

    @property
    def dtype_attr(self) -> str:
        return f"{self.name}_dtype"

    @property
    def meta_attr(self) -> str:
        return f"{self.name}_meta"

    def __repr__(self) -> str:
        temp = tuple(
            map(
                pprint_thing, (self.name, self.cname, self.dtype, self.kind, self.shape)
            )
        )
        return ",".join(
            (
                f"{key}->{value}"
                for key, value in zip(["name", "cname", "dtype", "kind", "shape"], temp)
            )
        )

    def __eq__(self, other: Any) -> bool:
        """ compare 2 col items """
        return all(
            getattr(self, a, None) == getattr(other, a, None)
            for a in ["name", "cname", "dtype", "pos"]
        )

    def set_data(self, data, dtype=None):
        self.data = data
        if data is not None:
            if dtype is not None:
                self.dtype = dtype
                self.set_kind()
            elif self.dtype is None:
                self.dtype = data.dtype.name
                self.set_kind()

    def take_data(self):
        """ return the data & release the memory """
        self.data, data = None, self.data
        return data

    def set_metadata(self, metadata):
        """ record the metadata """
        if metadata is not None:
            metadata = np.array(metadata, copy=False).ravel()
        self.metadata = metadata

    def set_kind(self):
        # set my kind if we can

        if self.dtype is not None:
            dtype = _ensure_decoded(self.dtype)

            if dtype.startswith("string") or dtype.startswith("bytes"):
                self.kind = "string"
            elif dtype.startswith("float"):
                self.kind = "float"
            elif dtype.startswith("complex"):
                self.kind = "complex"
            elif dtype.startswith("int") or dtype.startswith("uint"):
                self.kind = "integer"
            elif dtype.startswith("date"):
                # in tests this is always "datetime64"
                self.kind = "datetime"
            elif dtype.startswith("timedelta"):
                self.kind = "timedelta"
            elif dtype.startswith("bool"):
                self.kind = "bool"
            else:
                raise AssertionError(f"cannot interpret dtype of [{dtype}] in [{self}]")

            # set my typ if we need
            if self.typ is None:
                self.typ = getattr(self.description, self.cname, None)

    def set_atom(
        self,
        block,
        block_items,
        existing_col,
        min_itemsize,
        nan_rep,
        info,
        encoding=None,
        errors="strict",
    ):
        """ create and setup my atom from the block b """

        self.values = list(block_items)

        # short-cut certain block types
        if block.is_categorical:
            return self.set_atom_categorical(block, items=block_items, info=info)
        elif block.is_datetimetz:
            return self.set_atom_datetime64tz(block, info=info)
        elif block.is_datetime:
            return self.set_atom_datetime64(block)
        elif block.is_timedelta:
            return self.set_atom_timedelta64(block)
        elif block.is_complex:
            return self.set_atom_complex(block)

        dtype = block.dtype.name
        inferred_type = lib.infer_dtype(block.values, skipna=False)

        if inferred_type == "date":
            raise TypeError("[date] is not implemented as a table column")
        elif inferred_type == "datetime":
            # after GH#8260
            # this only would be hit for a multi-timezone dtype
            # which is an error

            raise TypeError(
                "too many timezones in this block, create separate data columns"
            )
        elif inferred_type == "unicode":
            raise TypeError("[unicode] is not implemented as a table column")

        # this is basically a catchall; if say a datetime64 has nans then will
        # end up here ###
        elif inferred_type == "string" or dtype == "object":
            self.set_atom_string(
                block,
                block_items,
                existing_col,
                min_itemsize,
                nan_rep,
                encoding,
                errors,
            )

        # set as a data block
        else:
            self.set_atom_data(block)

    def get_atom_string(self, block, itemsize):
        return _tables().StringCol(itemsize=itemsize, shape=block.shape[0])

    def set_atom_string(
        self, block, block_items, existing_col, min_itemsize, nan_rep, encoding, errors
    ):
        # fill nan items with myself, don't disturb the blocks by
        # trying to downcast
        block = block.fillna(nan_rep, downcast=False)
        if isinstance(block, list):
            block = block[0]
        data = block.values

        # see if we have a valid string type
        inferred_type = lib.infer_dtype(data.ravel(), skipna=False)
        if inferred_type != "string":

            # we cannot serialize this data, so report an exception on a column
            # by column basis
            for i, item in enumerate(block_items):

                col = block.iget(i)
                inferred_type = lib.infer_dtype(col.ravel(), skipna=False)
                if inferred_type != "string":
                    raise TypeError(
                        f"Cannot serialize the column [{item}] because\n"
                        f"its data contents are [{inferred_type}] object dtype"
                    )

        # itemsize is the maximum length of a string (along any dimension)
        data_converted = _convert_string_array(data, encoding, errors)
        itemsize = data_converted.itemsize

        # specified min_itemsize?
        if isinstance(min_itemsize, dict):
            min_itemsize = int(
                min_itemsize.get(self.name) or min_itemsize.get("values") or 0
            )
        itemsize = max(min_itemsize or 0, itemsize)

        # check for column in the values conflicts
        if existing_col is not None:
            eci = existing_col.validate_col(itemsize)
            if eci > itemsize:
                itemsize = eci

        self.itemsize = itemsize
        self.kind = "string"
        self.typ = self.get_atom_string(block, itemsize)
        self.set_data(data_converted.astype(f"|S{itemsize}", copy=False))

    def get_atom_coltype(self, kind=None):
        """ return the PyTables column class for this column """
        if kind is None:
            kind = self.kind
        if self.kind.startswith("uint"):
            k4 = kind[4:]
            col_name = f"UInt{k4}Col"
        else:
            kcap = kind.capitalize()
            col_name = f"{kcap}Col"

        return getattr(_tables(), col_name)

    def get_atom_data(self, block, kind=None):
        return self.get_atom_coltype(kind=kind)(shape=block.shape[0])

    def set_atom_complex(self, block):
        self.kind = block.dtype.name
        itemsize = int(self.kind.split("complex")[-1]) // 8
        self.typ = _tables().ComplexCol(itemsize=itemsize, shape=block.shape[0])
        self.set_data(block.values.astype(self.typ.type, copy=False))

    def set_atom_data(self, block):
        self.kind = block.dtype.name
        self.typ = self.get_atom_data(block)
        self.set_data(block.values.astype(self.typ.type, copy=False))

    def set_atom_categorical(self, block, items, info=None):
        # currently only supports a 1-D categorical
        # in a 1-D block

        values = block.values
        codes = values.codes
        self.kind = "integer"
        self.dtype = codes.dtype.name
        if values.ndim > 1:
            raise NotImplementedError("only support 1-d categoricals")
        if len(items) > 1:
            raise NotImplementedError("only support single block categoricals")

        # write the codes; must be in a block shape
        self.ordered = values.ordered
        self.typ = self.get_atom_data(block, kind=codes.dtype.name)
        self.set_data(_block_shape(codes))

        # write the categories
        self.meta = "category"
        self.set_metadata(block.values.categories)

        # update the info
        self.update_info(info)

    def get_atom_datetime64(self, block):
        return _tables().Int64Col(shape=block.shape[0])

    def set_atom_datetime64(self, block):
        self.kind = "datetime64"
        self.typ = self.get_atom_datetime64(block)
        values = block.values.view("i8")
        self.set_data(values, "datetime64")

    def set_atom_datetime64tz(self, block, info):

        values = block.values

        # convert this column to i8 in UTC, and save the tz
        values = values.asi8.reshape(block.shape)

        # store a converted timezone
        self.tz = _get_tz(block.values.tz)
        self.update_info(info)

        self.kind = "datetime64"
        self.typ = self.get_atom_datetime64(block)
        self.set_data(values, "datetime64")

    def get_atom_timedelta64(self, block):
        return _tables().Int64Col(shape=block.shape[0])

    def set_atom_timedelta64(self, block):
        self.kind = "timedelta64"
        self.typ = self.get_atom_timedelta64(block)
        values = block.values.view("i8")
        self.set_data(values, "timedelta64")

    @property
    def shape(self):
        return getattr(self.data, "shape", None)

    @property
    def cvalues(self):
        """ return my cython values """
        return self.data

    def validate_attr(self, append):
        """validate that we have the same order as the existing & same dtype"""
        if append:
            existing_fields = getattr(self.attrs, self.kind_attr, None)
            if existing_fields is not None and existing_fields != list(self.values):
                raise ValueError("appended items do not match existing items in table!")

            existing_dtype = getattr(self.attrs, self.dtype_attr, None)
            if existing_dtype is not None and existing_dtype != self.dtype:
                raise ValueError(
                    "appended items dtype do not match existing "
                    "items dtype in table!"
                )

    def convert(self, values, nan_rep, encoding, errors, start=None, stop=None):
        """set the data from this selection (and convert to the correct dtype
        if we can)
        """

        # values is a recarray
        if values.dtype.fields is not None:
            values = values[self.cname]

        self.set_data(values)

        # use the meta if needed
        meta = _ensure_decoded(self.meta)

        # convert to the correct dtype
        if self.dtype is not None:
            dtype = _ensure_decoded(self.dtype)

            # reverse converts
            if dtype == "datetime64":

                # recreate with tz if indicated
                self.data = _set_tz(self.data, self.tz, coerce=True)

            elif dtype == "timedelta64":
                self.data = np.asarray(self.data, dtype="m8[ns]")
            elif dtype == "date":
                try:
                    self.data = np.asarray(
                        [date.fromordinal(v) for v in self.data], dtype=object
                    )
                except ValueError:
                    self.data = np.asarray(
                        [date.fromtimestamp(v) for v in self.data], dtype=object
                    )

            elif meta == "category":

                # we have a categorical
                categories = self.metadata
                codes = self.data.ravel()

                # if we have stored a NaN in the categories
                # then strip it; in theory we could have BOTH
                # -1s in the codes and nulls :<
                if categories is None:
                    # Handle case of NaN-only categorical columns in which case
                    # the categories are an empty array; when this is stored,
                    # pytables cannot write a zero-len array, so on readback
                    # the categories would be None and `read_hdf()` would fail.
                    categories = Index([], dtype=np.float64)
                else:
                    mask = isna(categories)
                    if mask.any():
                        categories = categories[~mask]
                        codes[codes != -1] -= mask.astype(int).cumsum().values

                self.data = Categorical.from_codes(
                    codes, categories=categories, ordered=self.ordered
                )

            else:

                try:
                    self.data = self.data.astype(dtype, copy=False)
                except TypeError:
                    self.data = self.data.astype("O", copy=False)

        # convert nans / decode
        if _ensure_decoded(self.kind) == "string":
            self.data = _unconvert_string_array(
                self.data, nan_rep=nan_rep, encoding=encoding, errors=errors
            )

    def get_attr(self):
        """ get the data for this column """
        self.values = getattr(self.attrs, self.kind_attr, None)
        self.dtype = getattr(self.attrs, self.dtype_attr, None)
        self.meta = getattr(self.attrs, self.meta_attr, None)
        self.set_kind()

    def set_attr(self):
        """ set the data for this column """
        setattr(self.attrs, self.kind_attr, self.values)
        setattr(self.attrs, self.meta_attr, self.meta)
        if self.dtype is not None:
            setattr(self.attrs, self.dtype_attr, self.dtype)


class DataIndexableCol(DataCol):
    """ represent a data column that can be indexed """

    is_data_indexable = True

    def validate_names(self):
        if not Index(self.values).is_object():
            # TODO: should the message here be more specifically non-str?
            raise ValueError("cannot have non-object label DataIndexableCol")

    def get_atom_string(self, block, itemsize):
        return _tables().StringCol(itemsize=itemsize)

    def get_atom_data(self, block, kind=None):
        return self.get_atom_coltype(kind=kind)()

    def get_atom_datetime64(self, block):
        return _tables().Int64Col()

    def get_atom_timedelta64(self, block):
        return _tables().Int64Col()


class GenericDataIndexableCol(DataIndexableCol):
    """ represent a generic pytables data column """

    def get_attr(self):
        pass


class Fixed:
    """ represent an object in my store
        facilitate read/write of various types of objects
        this is an abstract base class

        Parameters
        ----------

        parent : my parent HDFStore
        group  : the group node where the table resides
        """

    pandas_kind: str
    obj_type: Type[Union[DataFrame, Series]]
    ndim: int
    parent: HDFStore
    group: "Node"
    errors: str
    is_table = False

    def __init__(
        self, parent: HDFStore, group: "Node", encoding=None, errors: str = "strict"
    ):
        assert isinstance(parent, HDFStore), type(parent)
        assert _table_mod is not None  # needed for mypy
        assert isinstance(group, _table_mod.Node), type(group)
        self.parent = parent
        self.group = group
        self.encoding = _ensure_encoding(encoding)
        self.errors = errors

    @property
    def is_old_version(self) -> bool:
        return self.version[0] <= 0 and self.version[1] <= 10 and self.version[2] < 1

    @property
    def version(self) -> Tuple[int, int, int]:
        """ compute and set our version """
        version = _ensure_decoded(getattr(self.group._v_attrs, "pandas_version", None))
        try:
            version = tuple(int(x) for x in version.split("."))
            if len(version) == 2:
                version = version + (0,)
        except AttributeError:
            version = (0, 0, 0)
        return version

    @property
    def pandas_type(self):
        return _ensure_decoded(getattr(self.group._v_attrs, "pandas_type", None))

    def __repr__(self) -> str:
        """ return a pretty representation of myself """
        self.infer_axes()
        s = self.shape
        if s is not None:
            if isinstance(s, (list, tuple)):
                jshape = ",".join(pprint_thing(x) for x in s)
                s = f"[{jshape}]"
            return f"{self.pandas_type:12.12} (shape->{s})"
        return self.pandas_type

    def set_object_info(self):
        """ set my pandas type & version """
        self.attrs.pandas_type = str(self.pandas_kind)
        self.attrs.pandas_version = str(_version)

    def copy(self):
        new_self = copy.copy(self)
        return new_self

    @property
    def storage_obj_type(self):
        return self.obj_type

    @property
    def shape(self):
        return self.nrows

    @property
    def pathname(self):
        return self.group._v_pathname

    @property
    def _handle(self):
        return self.parent._handle

    @property
    def _filters(self):
        return self.parent._filters

    @property
    def _complevel(self) -> int:
        return self.parent._complevel

    @property
    def _fletcher32(self) -> bool:
        return self.parent._fletcher32

    @property
    def _complib(self):
        return self.parent._complib

    @property
    def attrs(self):
        return self.group._v_attrs

    def set_attrs(self):
        """ set our object attributes """
        pass

    def get_attrs(self):
        """ get our object attributes """
        pass

    @property
    def storable(self):
        """ return my storable """
        return self.group

    @property
    def is_exists(self) -> bool:
        return False

    @property
    def nrows(self):
        return getattr(self.storable, "nrows", None)

    def validate(self, other):
        """ validate against an existing storable """
        if other is None:
            return
        return True

    def validate_version(self, where=None):
        """ are we trying to operate on an old version? """
        return True

    def infer_axes(self):
        """ infer the axes of my storer
              return a boolean indicating if we have a valid storer or not """

        s = self.storable
        if s is None:
            return False
        self.get_attrs()
        return True

    def read(
        self,
        where=None,
        columns=None,
        start: Optional[int] = None,
        stop: Optional[int] = None,
    ):
        raise NotImplementedError(
            "cannot read on an abstract storer: subclasses should implement"
        )

    def write(self, **kwargs):
        raise NotImplementedError(
            "cannot write on an abstract storer: subclasses should implement"
        )

    def delete(
        self, where=None, start: Optional[int] = None, stop: Optional[int] = None
    ):
        """
        support fully deleting the node in its entirety (only) - where
        specification must be None
        """
        if com.all_none(where, start, stop):
            self._handle.remove_node(self.group, recursive=True)
            return None

        raise TypeError("cannot delete on an abstract storer")


class GenericFixed(Fixed):
    """ a generified fixed version """

    _index_type_map = {DatetimeIndex: "datetime", PeriodIndex: "period"}
    _reverse_index_map = {v: k for k, v in _index_type_map.items()}
    attributes: List[str] = []

    # indexer helpders
    def _class_to_alias(self, cls) -> str:
        return self._index_type_map.get(cls, "")

    def _alias_to_class(self, alias):
        if isinstance(alias, type):  # pragma: no cover
            # compat: for a short period of time master stored types
            return alias
        return self._reverse_index_map.get(alias, Index)

    def _get_index_factory(self, klass):
        if klass == DatetimeIndex:

            def f(values, freq=None, tz=None):
                # data are already in UTC, localize and convert if tz present
                result = DatetimeIndex._simple_new(values.values, name=None, freq=freq)
                if tz is not None:
                    result = result.tz_localize("UTC").tz_convert(tz)
                return result

            return f
        elif klass == PeriodIndex:

            def f(values, freq=None, tz=None):
                return PeriodIndex._simple_new(values, name=None, freq=freq)

            return f

        return klass

    def validate_read(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """
        remove table keywords from kwargs and return
        raise if any keywords are passed which are not-None
        """
        kwargs = copy.copy(kwargs)

        columns = kwargs.pop("columns", None)
        if columns is not None:
            raise TypeError(
                "cannot pass a column specification when reading "
                "a Fixed format store. this store must be "
                "selected in its entirety"
            )
        where = kwargs.pop("where", None)
        if where is not None:
            raise TypeError(
                "cannot pass a where specification when reading "
                "from a Fixed format store. this store must be "
                "selected in its entirety"
            )
        return kwargs

    @property
    def is_exists(self) -> bool:
        return True

    def set_attrs(self):
        """ set our object attributes """
        self.attrs.encoding = self.encoding
        self.attrs.errors = self.errors

    def get_attrs(self):
        """ retrieve our attributes """
        self.encoding = _ensure_encoding(getattr(self.attrs, "encoding", None))
        self.errors = _ensure_decoded(getattr(self.attrs, "errors", "strict"))
        for n in self.attributes:
            setattr(self, n, _ensure_decoded(getattr(self.attrs, n, None)))

    def write(self, obj, **kwargs):
        self.set_attrs()

    def read_array(
        self, key: str, start: Optional[int] = None, stop: Optional[int] = None
    ):
        """ read an array for the specified node (off of group """
        import tables

        node = getattr(self.group, key)
        attrs = node._v_attrs

        transposed = getattr(attrs, "transposed", False)

        if isinstance(node, tables.VLArray):
            ret = node[0][start:stop]
        else:
            dtype = getattr(attrs, "value_type", None)
            shape = getattr(attrs, "shape", None)

            if shape is not None:
                # length 0 axis
                ret = np.empty(shape, dtype=dtype)
            else:
                ret = node[start:stop]

            if dtype == "datetime64":

                # reconstruct a timezone if indicated
                ret = _set_tz(ret, getattr(attrs, "tz", None), coerce=True)

            elif dtype == "timedelta64":
                ret = np.asarray(ret, dtype="m8[ns]")

        if transposed:
            return ret.T
        else:
            return ret

    def read_index(
        self, key: str, start: Optional[int] = None, stop: Optional[int] = None
    ) -> Index:
        variety = _ensure_decoded(getattr(self.attrs, f"{key}_variety"))

        if variety == "multi":
            return self.read_multi_index(key, start=start, stop=stop)
        elif variety == "regular":
            node = getattr(self.group, key)
            _, index = self.read_index_node(node, start=start, stop=stop)
            return index
        else:  # pragma: no cover
            raise TypeError(f"unrecognized index variety: {variety}")

    def write_index(self, key: str, index: Index):
        if isinstance(index, MultiIndex):
            setattr(self.attrs, f"{key}_variety", "multi")
            self.write_multi_index(key, index)
        else:
            setattr(self.attrs, f"{key}_variety", "regular")
            converted = _convert_index("index", index, self.encoding, self.errors)

            self.write_array(key, converted.values)

            node = getattr(self.group, key)
            node._v_attrs.kind = converted.kind
            node._v_attrs.name = index.name

            if isinstance(index, (DatetimeIndex, PeriodIndex)):
                node._v_attrs.index_class = self._class_to_alias(type(index))

            if isinstance(index, (DatetimeIndex, PeriodIndex, TimedeltaIndex)):
                node._v_attrs.freq = index.freq

            if isinstance(index, DatetimeIndex) and index.tz is not None:
                node._v_attrs.tz = _get_tz(index.tz)

    def write_multi_index(self, key: str, index: MultiIndex):
        setattr(self.attrs, f"{key}_nlevels", index.nlevels)

        for i, (lev, level_codes, name) in enumerate(
            zip(index.levels, index.codes, index.names)
        ):
            # write the level
            if is_extension_array_dtype(lev):
                raise NotImplementedError(
                    "Saving a MultiIndex with an extension dtype is not supported."
                )
            level_key = f"{key}_level{i}"
            conv_level = _convert_index(level_key, lev, self.encoding, self.errors)
            self.write_array(level_key, conv_level.values)
            node = getattr(self.group, level_key)
            node._v_attrs.kind = conv_level.kind
            node._v_attrs.name = name

            # write the name
            setattr(node._v_attrs, f"{key}_name{name}", name)

            # write the labels
            label_key = f"{key}_label{i}"
            self.write_array(label_key, level_codes)

    def read_multi_index(
        self, key: str, start: Optional[int] = None, stop: Optional[int] = None
    ) -> MultiIndex:
        nlevels = getattr(self.attrs, f"{key}_nlevels")

        levels = []
        codes = []
        names = []
        for i in range(nlevels):
            level_key = f"{key}_level{i}"
            node = getattr(self.group, level_key)
            name, lev = self.read_index_node(node, start=start, stop=stop)
            levels.append(lev)
            names.append(name)

            label_key = f"{key}_label{i}"
            level_codes = self.read_array(label_key, start=start, stop=stop)
            codes.append(level_codes)

        return MultiIndex(
            levels=levels, codes=codes, names=names, verify_integrity=True
        )

    def read_index_node(
        self, node: "Node", start: Optional[int] = None, stop: Optional[int] = None
    ):
        data = node[start:stop]
        # If the index was an empty array write_array_empty() will
        # have written a sentinel. Here we relace it with the original.
        if "shape" in node._v_attrs and self._is_empty_array(node._v_attrs.shape):
            data = np.empty(node._v_attrs.shape, dtype=node._v_attrs.value_type,)
        kind = _ensure_decoded(node._v_attrs.kind)
        name = None

        if "name" in node._v_attrs:
            name = _ensure_str(node._v_attrs.name)
            name = _ensure_decoded(name)

        index_class = self._alias_to_class(
            _ensure_decoded(getattr(node._v_attrs, "index_class", ""))
        )
        factory = self._get_index_factory(index_class)

        kwargs = {}
        if "freq" in node._v_attrs:
            kwargs["freq"] = node._v_attrs["freq"]

        if "tz" in node._v_attrs:
            if isinstance(node._v_attrs["tz"], bytes):
                # created by python2
                kwargs["tz"] = node._v_attrs["tz"].decode("utf-8")
            else:
                # created by python3
                kwargs["tz"] = node._v_attrs["tz"]

        if kind == "date":
            index = factory(
                _unconvert_index(
                    data, kind, encoding=self.encoding, errors=self.errors
                ),
                dtype=object,
                **kwargs,
            )
        else:
            index = factory(
                _unconvert_index(
                    data, kind, encoding=self.encoding, errors=self.errors
                ),
                **kwargs,
            )

        index.name = name

        return name, index

    def write_array_empty(self, key: str, value):
        """ write a 0-len array """

        # ugly hack for length 0 axes
        arr = np.empty((1,) * value.ndim)
        self._handle.create_array(self.group, key, arr)
        getattr(self.group, key)._v_attrs.value_type = str(value.dtype)
        getattr(self.group, key)._v_attrs.shape = value.shape

    def _is_empty_array(self, shape) -> bool:
        """Returns true if any axis is zero length."""
        return any(x == 0 for x in shape)

    def write_array(self, key: str, value, items=None):
        if key in self.group:
            self._handle.remove_node(self.group, key)

        # Transform needed to interface with pytables row/col notation
        empty_array = self._is_empty_array(value.shape)
        transposed = False

        if is_categorical_dtype(value):
            raise NotImplementedError(
                "Cannot store a category dtype in "
                "a HDF5 dataset that uses format="
                '"fixed". Use format="table".'
            )
        if not empty_array:
            if hasattr(value, "T"):
                # ExtensionArrays (1d) may not have transpose.
                value = value.T
                transposed = True

        if self._filters is not None:
            atom = None
            try:
                # get the atom for this datatype
                atom = _tables().Atom.from_dtype(value.dtype)
            except ValueError:
                pass

            if atom is not None:
                # create an empty chunked array and fill it from value
                if not empty_array:
                    ca = self._handle.create_carray(
                        self.group, key, atom, value.shape, filters=self._filters
                    )
                    ca[:] = value
                    getattr(self.group, key)._v_attrs.transposed = transposed

                else:
                    self.write_array_empty(key, value)

                return

        if value.dtype.type == np.object_:

            # infer the type, warn if we have a non-string type here (for
            # performance)
            inferred_type = lib.infer_dtype(value.ravel(), skipna=False)
            if empty_array:
                pass
            elif inferred_type == "string":
                pass
            else:
                try:
                    items = list(items)
                except TypeError:
                    pass
                ws = performance_doc % (inferred_type, key, items)
                warnings.warn(ws, PerformanceWarning, stacklevel=7)

            vlarr = self._handle.create_vlarray(self.group, key, _tables().ObjectAtom())
            vlarr.append(value)
        else:
            if empty_array:
                self.write_array_empty(key, value)
            else:
                if is_datetime64_dtype(value.dtype):
                    self._handle.create_array(self.group, key, value.view("i8"))
                    getattr(self.group, key)._v_attrs.value_type = "datetime64"
                elif is_datetime64tz_dtype(value.dtype):
                    # store as UTC
                    # with a zone
                    self._handle.create_array(self.group, key, value.asi8)

                    node = getattr(self.group, key)
                    node._v_attrs.tz = _get_tz(value.tz)
                    node._v_attrs.value_type = "datetime64"
                elif is_timedelta64_dtype(value.dtype):
                    self._handle.create_array(self.group, key, value.view("i8"))
                    getattr(self.group, key)._v_attrs.value_type = "timedelta64"
                else:
                    self._handle.create_array(self.group, key, value)

        getattr(self.group, key)._v_attrs.transposed = transposed


class SeriesFixed(GenericFixed):
    pandas_kind = "series"
    attributes = ["name"]

    name: Optional[Hashable]

    @property
    def shape(self):
        try:
            return (len(self.group.values),)
        except (TypeError, AttributeError):
            return None

    def read(
        self,
        where=None,
        columns=None,
        start: Optional[int] = None,
        stop: Optional[int] = None,
    ):
        self.validate_read({"where": where, "columns": columns})
        index = self.read_index("index", start=start, stop=stop)
        values = self.read_array("values", start=start, stop=stop)
        return Series(values, index=index, name=self.name)

    def write(self, obj, **kwargs):
        super().write(obj, **kwargs)
        self.write_index("index", obj.index)
        self.write_array("values", obj.values)
        self.attrs.name = obj.name


class BlockManagerFixed(GenericFixed):
    attributes = ["ndim", "nblocks"]
    is_shape_reversed = False

    nblocks: int

    @property
    def shape(self):
        try:
            ndim = self.ndim

            # items
            items = 0
            for i in range(self.nblocks):
                node = getattr(self.group, f"block{i}_items")
                shape = getattr(node, "shape", None)
                if shape is not None:
                    items += shape[0]

            # data shape
            node = self.group.block0_values
            shape = getattr(node, "shape", None)
            if shape is not None:
                shape = list(shape[0 : (ndim - 1)])
            else:
                shape = []

            shape.append(items)

            # hacky - this works for frames, but is reversed for panels
            if self.is_shape_reversed:
                shape = shape[::-1]

            return shape
        except AttributeError:
            return None

    def read(
        self,
        where=None,
        columns=None,
        start: Optional[int] = None,
        stop: Optional[int] = None,
    ):
        # start, stop applied to rows, so 0th axis only
        self.validate_read({"columns": columns, "where": where})
        select_axis = self.obj_type()._get_block_manager_axis(0)

        axes = []
        for i in range(self.ndim):

            _start, _stop = (start, stop) if i == select_axis else (None, None)
            ax = self.read_index(f"axis{i}", start=_start, stop=_stop)
            axes.append(ax)

        items = axes[0]
        blocks = []
        for i in range(self.nblocks):

            blk_items = self.read_index(f"block{i}_items")
            values = self.read_array(f"block{i}_values", start=_start, stop=_stop)
            blk = make_block(
                values, placement=items.get_indexer(blk_items), ndim=len(axes)
            )
            blocks.append(blk)

        return self.obj_type(BlockManager(blocks, axes))

    def write(self, obj, **kwargs):
        super().write(obj, **kwargs)
        data = obj._data
        if not data.is_consolidated():
            data = data.consolidate()

        self.attrs.ndim = data.ndim
        for i, ax in enumerate(data.axes):
            if i == 0:
                if not ax.is_unique:
                    raise ValueError("Columns index has to be unique for fixed format")
            self.write_index(f"axis{i}", ax)

        # Supporting mixed-type DataFrame objects...nontrivial
        self.attrs.nblocks = len(data.blocks)
        for i, blk in enumerate(data.blocks):
            # I have no idea why, but writing values before items fixed #2299
            blk_items = data.items.take(blk.mgr_locs)
            self.write_array(f"block{i}_values", blk.values, items=blk_items)
            self.write_index(f"block{i}_items", blk_items)


class FrameFixed(BlockManagerFixed):
    pandas_kind = "frame"
    obj_type = DataFrame


class Table(Fixed):
    """ represent a table:
          facilitate read/write of various types of tables

        Attrs in Table Node
        -------------------
        These are attributes that are store in the main table node, they are
        necessary to recreate these tables when read back in.

        index_axes    : a list of tuples of the (original indexing axis and
            index column)
        non_index_axes: a list of tuples of the (original index axis and
            columns on a non-indexing axis)
        values_axes   : a list of the columns which comprise the data of this
            table
        data_columns  : a list of the columns that we are allowing indexing
            (these become single columns in values_axes), or True to force all
            columns
        nan_rep       : the string to use for nan representations for string
            objects
        levels        : the names of levels
        metadata      : the names of the metadata columns

        """

    pandas_kind = "wide_table"
    table_type: str
    levels = 1
    is_table = True
    is_shape_reversed = False

    index_axes: List[IndexCol]
    non_index_axes: List[Tuple[int, Any]]
    values_axes: List[DataCol]
    data_columns: List
    metadata: List
    info: Dict

    def __init__(
        self, parent: HDFStore, group: "Node", encoding=None, errors: str = "strict"
    ):
        super().__init__(parent, group, encoding=encoding, errors=errors)
        self.index_axes = []
        self.non_index_axes = []
        self.values_axes = []
        self.data_columns = []
        self.metadata = []
        self.info = dict()
        self.nan_rep = None

    @property
    def table_type_short(self) -> str:
        return self.table_type.split("_")[0]

    def __repr__(self) -> str:
        """ return a pretty representation of myself """
        self.infer_axes()
        jdc = ",".join(self.data_columns) if len(self.data_columns) else ""
        dc = f",dc->[{jdc}]"

        ver = ""
        if self.is_old_version:
            jver = ".".join(str(x) for x in self.version)
            ver = f"[{jver}]"

        jindex_axes = ",".join(a.name for a in self.index_axes)
        return (
            f"{self.pandas_type:12.12}{ver} "
            f"(typ->{self.table_type_short},nrows->{self.nrows},"
            f"ncols->{self.ncols},indexers->[{jindex_axes}]{dc})"
        )

    def __getitem__(self, c):
        """ return the axis for c """
        for a in self.axes:
            if c == a.name:
                return a
        return None

    def validate(self, other):
        """ validate against an existing table """
        if other is None:
            return

        if other.table_type != self.table_type:
            raise TypeError(
                "incompatible table_type with existing "
                f"[{other.table_type} - {self.table_type}]"
            )

        for c in ["index_axes", "non_index_axes", "values_axes"]:
            sv = getattr(self, c, None)
            ov = getattr(other, c, None)
            if sv != ov:

                # show the error for the specific axes
                for i, sax in enumerate(sv):
                    oax = ov[i]
                    if sax != oax:
                        raise ValueError(
                            f"invalid combinate of [{c}] on appending data "
                            f"[{sax}] vs current table [{oax}]"
                        )

                # should never get here
                raise Exception(
                    f"invalid combinate of [{c}] on appending data [{sv}] vs "
                    f"current table [{ov}]"
                )

    @property
    def is_multi_index(self) -> bool:
        """the levels attribute is 1 or a list in the case of a multi-index"""
        return isinstance(self.levels, list)

    def validate_metadata(self, existing):
        """ create / validate metadata """
        self.metadata = [c.name for c in self.values_axes if c.metadata is not None]

    def validate_multiindex(self, obj):
        """validate that we can store the multi-index; reset and return the
        new object
        """
        levels = [
            l if l is not None else f"level_{i}" for i, l in enumerate(obj.index.names)
        ]
        try:
            return obj.reset_index(), levels
        except ValueError:
            raise ValueError(
                "duplicate names/columns in the multi-index when storing as a table"
            )

    @property
    def nrows_expected(self) -> int:
        """ based on our axes, compute the expected nrows """
        return np.prod([i.cvalues.shape[0] for i in self.index_axes])

    @property
    def is_exists(self) -> bool:
        """ has this table been created """
        return "table" in self.group

    @property
    def storable(self):
        return getattr(self.group, "table", None)

    @property
    def table(self):
        """ return the table group (this is my storable) """
        return self.storable

    @property
    def dtype(self):
        return self.table.dtype

    @property
    def description(self):
        return self.table.description

    @property
    def axes(self):
        return itertools.chain(self.index_axes, self.values_axes)

    @property
    def ncols(self) -> int:
        """ the number of total columns in the values axes """
        return sum(len(a.values) for a in self.values_axes)

    @property
    def is_transposed(self) -> bool:
        return False

    @property
    def data_orientation(self):
        """return a tuple of my permutated axes, non_indexable at the front"""
        return tuple(
            itertools.chain(
                [int(a[0]) for a in self.non_index_axes],
                [int(a.axis) for a in self.index_axes],
            )
        )

    def queryables(self) -> Dict[str, Any]:
        """ return a dict of the kinds allowable columns for this object """

        # compute the values_axes queryables
        d1 = [(a.cname, a) for a in self.index_axes]
        d2 = [
            (self.storage_obj_type._AXIS_NAMES[axis], None)
            for axis, values in self.non_index_axes
        ]
        d3 = [
            (v.cname, v) for v in self.values_axes if v.name in set(self.data_columns)
        ]

        return dict(d1 + d2 + d3)  # type: ignore
        # error: List comprehension has incompatible type
        #  List[Tuple[Any, None]]; expected List[Tuple[str, IndexCol]]

    def index_cols(self):
        """ return a list of my index cols """
        # Note: each `i.cname` below is assured to be a str.
        return [(i.axis, i.cname) for i in self.index_axes]

    def values_cols(self) -> List[str]:
        """ return a list of my values cols """
        return [i.cname for i in self.values_axes]

    def _get_metadata_path(self, key: str) -> str:
        """ return the metadata pathname for this key """
        group = self.group._v_pathname
        return f"{group}/meta/{key}/meta"

    def write_metadata(self, key: str, values):
        """
        write out a meta data array to the key as a fixed-format Series

        Parameters
        ----------
        key : str
        values : ndarray
        """
        values = Series(values)
        self.parent.put(
            self._get_metadata_path(key),
            values,
            format="table",
            encoding=self.encoding,
            errors=self.errors,
            nan_rep=self.nan_rep,
        )

    def read_metadata(self, key: str):
        """ return the meta data array for this key """
        if getattr(getattr(self.group, "meta", None), key, None) is not None:
            return self.parent.select(self._get_metadata_path(key))
        return None

    def set_attrs(self):
        """ set our table type & indexables """
        self.attrs.table_type = str(self.table_type)
        self.attrs.index_cols = self.index_cols()
        self.attrs.values_cols = self.values_cols()
        self.attrs.non_index_axes = self.non_index_axes
        self.attrs.data_columns = self.data_columns
        self.attrs.nan_rep = self.nan_rep
        self.attrs.encoding = self.encoding
        self.attrs.errors = self.errors
        self.attrs.levels = self.levels
        self.attrs.metadata = self.metadata
        self.attrs.info = self.info

    def get_attrs(self):
        """ retrieve our attributes """
        self.non_index_axes = getattr(self.attrs, "non_index_axes", None) or []
        self.data_columns = getattr(self.attrs, "data_columns", None) or []
        self.info = getattr(self.attrs, "info", None) or dict()
        self.nan_rep = getattr(self.attrs, "nan_rep", None)
        self.encoding = _ensure_encoding(getattr(self.attrs, "encoding", None))
        self.errors = _ensure_decoded(getattr(self.attrs, "errors", "strict"))
        self.levels = getattr(self.attrs, "levels", None) or []
        self.index_axes = [a.infer(self) for a in self.indexables if a.is_an_indexable]
        self.values_axes = [
            a.infer(self) for a in self.indexables if not a.is_an_indexable
        ]
        self.metadata = getattr(self.attrs, "metadata", None) or []

    def validate_version(self, where=None):
        """ are we trying to operate on an old version? """
        if where is not None:
            if self.version[0] <= 0 and self.version[1] <= 10 and self.version[2] < 1:
                ws = incompatibility_doc % ".".join([str(x) for x in self.version])
                warnings.warn(ws, IncompatibilityWarning)

    def validate_min_itemsize(self, min_itemsize):
        """validate the min_itemsize doesn't contain items that are not in the
        axes this needs data_columns to be defined
        """
        if min_itemsize is None:
            return
        if not isinstance(min_itemsize, dict):
            return

        q = self.queryables()
        for k, v in min_itemsize.items():

            # ok, apply generally
            if k == "values":
                continue
            if k not in q:
                raise ValueError(
                    f"min_itemsize has the key [{k}] which is not an axis or "
                    "data_column"
                )

    @property
    def indexables(self):
        """ create/cache the indexables if they don't exist """
        if self._indexables is None:

            self._indexables = []

            # Note: each of the `name` kwargs below are str, ensured
            #  by the definition in index_cols.
            # index columns
            self._indexables.extend(
                [
                    IndexCol(name=name, axis=axis, pos=i)
                    for i, (axis, name) in enumerate(self.attrs.index_cols)
                ]
            )

            # values columns
            dc = set(self.data_columns)
            base_pos = len(self._indexables)

            def f(i, c):
                assert isinstance(c, str)
                klass = DataCol
                if c in dc:
                    klass = DataIndexableCol
                return klass.create_for_block(
                    i=i, name=c, pos=base_pos + i, version=self.version
                )

            # Note: the definition of `values_cols` ensures that each
            #  `c` below is a str.
            self._indexables.extend(
                [f(i, c) for i, c in enumerate(self.attrs.values_cols)]
            )

        return self._indexables

    def create_index(self, columns=None, optlevel=None, kind=None):
        """
        Create a pytables index on the specified columns
          note: cannot index Time64Col() or ComplexCol currently;
          PyTables must be >= 3.0

        Parameters
        ----------
        columns : False (don't create an index), True (create all columns
            index), None or list_like (the indexers to index)
        optlevel: optimization level (defaults to 6)
        kind    : kind of index (defaults to 'medium')

        Raises
        ------
        raises if the node is not a table

        """

        if not self.infer_axes():
            return
        if columns is False:
            return

        # index all indexables and data_columns
        if columns is None or columns is True:
            columns = [a.cname for a in self.axes if a.is_data_indexable]
        if not isinstance(columns, (tuple, list)):
            columns = [columns]

        kw = dict()
        if optlevel is not None:
            kw["optlevel"] = optlevel
        if kind is not None:
            kw["kind"] = kind

        table = self.table
        for c in columns:
            v = getattr(table.cols, c, None)
            if v is not None:

                # remove the index if the kind/optlevel have changed
                if v.is_indexed:
                    index = v.index
                    cur_optlevel = index.optlevel
                    cur_kind = index.kind

                    if kind is not None and cur_kind != kind:
                        v.remove_index()
                    else:
                        kw["kind"] = cur_kind

                    if optlevel is not None and cur_optlevel != optlevel:
                        v.remove_index()
                    else:
                        kw["optlevel"] = cur_optlevel

                # create the index
                if not v.is_indexed:
                    if v.type.startswith("complex"):
                        raise TypeError(
                            "Columns containing complex values can be stored "
                            "but cannot"
                            " be indexed when using table format. Either use "
                            "fixed format, set index=False, or do not include "
                            "the columns containing complex values to "
                            "data_columns when initializing the table."
                        )
                    v.create_index(**kw)

    def read_axes(
        self, where, start: Optional[int] = None, stop: Optional[int] = None
    ) -> bool:
        """
        Create the axes sniffed from the table.

        Parameters
        ----------
        where : ???
        start: int or None, default None
        stop: int or None, default None

        Returns
        -------
        bool
            Indicates success.
        """

        # validate the version
        self.validate_version(where)

        # infer the data kind
        if not self.infer_axes():
            return False

        # create the selection
        selection = Selection(self, where=where, start=start, stop=stop)
        values = selection.select()

        # convert the data
        for a in self.axes:
            a.set_info(self.info)
            a.convert(
                values,
                nan_rep=self.nan_rep,
                encoding=self.encoding,
                errors=self.errors,
                start=start,
                stop=stop,
            )

        return True

    def get_object(self, obj):
        """ return the data for this obj """
        return obj

    def validate_data_columns(self, data_columns, min_itemsize):
        """take the input data_columns and min_itemize and create a data
        columns spec
        """

        if not len(self.non_index_axes):
            return []

        axis, axis_labels = self.non_index_axes[0]
        info = self.info.get(axis, dict())
        if info.get("type") == "MultiIndex" and data_columns:
            raise ValueError(
                f"cannot use a multi-index on axis [{axis}] with "
                f"data_columns {data_columns}"
            )

        # evaluate the passed data_columns, True == use all columns
        # take only valide axis labels
        if data_columns is True:
            data_columns = list(axis_labels)
        elif data_columns is None:
            data_columns = []

        # if min_itemsize is a dict, add the keys (exclude 'values')
        if isinstance(min_itemsize, dict):

            existing_data_columns = set(data_columns)
            data_columns.extend(
                [
                    k
                    for k in min_itemsize.keys()
                    if k != "values" and k not in existing_data_columns
                ]
            )

        # return valid columns in the order of our axis
        return [c for c in data_columns if c in axis_labels]

    def create_axes(
        self,
        axes,
        obj,
        validate: bool = True,
        nan_rep=None,
        data_columns=None,
        min_itemsize=None,
    ):
        """ create and return the axes
        legacy tables create an indexable column, indexable index,
        non-indexable fields

            Parameters
            ----------
            axes: a list of the axes in order to create (names or numbers of
                the axes)
            obj : the object to create axes on
            validate: validate the obj against an existing object already
                written
            min_itemsize: a dict of the min size for a column in bytes
            nan_rep : a values to use for string column nan_rep
            encoding : the encoding for string values
            data_columns : a list of columns that we want to create separate to
                allow indexing (or True will force all columns)

        """

        # set the default axes if needed
        if axes is None:
            try:
                axes = _AXES_MAP[type(obj)]
            except KeyError:
                group = self.group._v_name
                raise TypeError(
                    f"cannot properly create the storer for: [group->{group},"
                    f"value->{type(obj)}]"
                )

        # map axes to numbers
        axes = [obj._get_axis_number(a) for a in axes]

        # do we have an existing table (if so, use its axes & data_columns)
        if self.infer_axes():
            existing_table = self.copy()
            existing_table.infer_axes()
            axes = [a.axis for a in existing_table.index_axes]
            data_columns = existing_table.data_columns
            nan_rep = existing_table.nan_rep
            self.encoding = existing_table.encoding
            self.errors = existing_table.errors
            self.info = copy.copy(existing_table.info)
        else:
            existing_table = None

        # currently support on ndim-1 axes
        if len(axes) != self.ndim - 1:
            raise ValueError(
                "currently only support ndim-1 indexers in an AppendableTable"
            )

        # create according to the new data
        self.non_index_axes = []
        self.data_columns = []

        # nan_representation
        if nan_rep is None:
            nan_rep = "nan"

        self.nan_rep = nan_rep

        # create axes to index and non_index
        index_axes_map = dict()
        for i, a in enumerate(obj.axes):

            if i in axes:
                name = obj._AXIS_NAMES[i]
                new_index = _convert_index(name, a, self.encoding, self.errors)
                new_index.axis = i
                index_axes_map[i] = new_index

            else:

                # we might be able to change the axes on the appending data if
                # necessary
                append_axis = list(a)
                if existing_table is not None:
                    indexer = len(self.non_index_axes)
                    exist_axis = existing_table.non_index_axes[indexer][1]
                    if not array_equivalent(
                        np.array(append_axis), np.array(exist_axis)
                    ):

                        # ahah! -> reindex
                        if array_equivalent(
                            np.array(sorted(append_axis)), np.array(sorted(exist_axis))
                        ):
                            append_axis = exist_axis

                # the non_index_axes info
                info = _get_info(self.info, i)
                info["names"] = list(a.names)
                info["type"] = type(a).__name__

                self.non_index_axes.append((i, append_axis))

        # set axis positions (based on the axes)
        new_index_axes = [index_axes_map[a] for a in axes]
        for j, iax in enumerate(new_index_axes):
            iax.set_pos(j)
            iax.update_info(self.info)
        self.index_axes = new_index_axes

        j = len(self.index_axes)

        # check for column conflicts
        for a in self.axes:
            a.maybe_set_size(min_itemsize=min_itemsize)

        # reindex by our non_index_axes & compute data_columns
        for a in self.non_index_axes:
            obj = _reindex_axis(obj, a[0], a[1])

        def get_blk_items(mgr, blocks):
            return [mgr.items.take(blk.mgr_locs) for blk in blocks]

        # figure out data_columns and get out blocks
        block_obj = self.get_object(obj)._consolidate()
        blocks = block_obj._data.blocks
        blk_items = get_blk_items(block_obj._data, blocks)
        if len(self.non_index_axes):
            axis, axis_labels = self.non_index_axes[0]
            data_columns = self.validate_data_columns(data_columns, min_itemsize)
            if len(data_columns):
                mgr = block_obj.reindex(
                    Index(axis_labels).difference(Index(data_columns)), axis=axis
                )._data

                blocks = list(mgr.blocks)
                blk_items = get_blk_items(mgr, blocks)
                for c in data_columns:
                    mgr = block_obj.reindex([c], axis=axis)._data
                    blocks.extend(mgr.blocks)
                    blk_items.extend(get_blk_items(mgr, mgr.blocks))

        # reorder the blocks in the same order as the existing_table if we can
        if existing_table is not None:
            by_items = {
                tuple(b_items.tolist()): (b, b_items)
                for b, b_items in zip(blocks, blk_items)
            }
            new_blocks = []
            new_blk_items = []
            for ea in existing_table.values_axes:
                items = tuple(ea.values)
                try:
                    b, b_items = by_items.pop(items)
                    new_blocks.append(b)
                    new_blk_items.append(b_items)
                except (IndexError, KeyError):
                    jitems = ",".join(pprint_thing(item) for item in items)
                    raise ValueError(
                        f"cannot match existing table structure for [{jitems}] "
                        "on appending data"
                    )
            blocks = new_blocks
            blk_items = new_blk_items

        # add my values
        self.values_axes = []
        for i, (b, b_items) in enumerate(zip(blocks, blk_items)):

            # shape of the data column are the indexable axes
            klass = DataCol
            name = None

            # we have a data_column
            if data_columns and len(b_items) == 1 and b_items[0] in data_columns:
                klass = DataIndexableCol
                name = b_items[0]
                if not (name is None or isinstance(name, str)):
                    # TODO: should the message here be more specifically non-str?
                    raise ValueError("cannot have non-object label DataIndexableCol")
                self.data_columns.append(name)

            # make sure that we match up the existing columns
            # if we have an existing table
            if existing_table is not None and validate:
                try:
                    existing_col = existing_table.values_axes[i]
                except (IndexError, KeyError):
                    raise ValueError(
                        f"Incompatible appended table [{blocks}]"
                        f"with existing table [{existing_table.values_axes}]"
                    )
            else:
                existing_col = None

            col = klass.create_for_block(i=i, name=name, version=self.version)
            col.set_atom(
                block=b,
                block_items=b_items,
                existing_col=existing_col,
                min_itemsize=min_itemsize,
                nan_rep=nan_rep,
                encoding=self.encoding,
                errors=self.errors,
                info=self.info,
            )
            col.set_pos(j)

            self.values_axes.append(col)

            j += 1

        # validate our min_itemsize
        self.validate_min_itemsize(min_itemsize)

        # validate our metadata
        self.validate_metadata(existing_table)

        # validate the axes if we have an existing table
        if validate:
            self.validate(existing_table)

    def process_axes(self, obj, selection: "Selection", columns=None):
        """ process axes filters """

        # make a copy to avoid side effects
        if columns is not None:
            columns = list(columns)

        # make sure to include levels if we have them
        if columns is not None and self.is_multi_index:
            assert isinstance(self.levels, list)  # assured by is_multi_index
            for n in self.levels:
                if n not in columns:
                    columns.insert(0, n)

        # reorder by any non_index_axes & limit to the select columns
        for axis, labels in self.non_index_axes:
            obj = _reindex_axis(obj, axis, labels, columns)

        # apply the selection filters (but keep in the same order)
        if selection.filter is not None:
            for field, op, filt in selection.filter.format():

                def process_filter(field, filt):

                    for axis_name in obj._AXIS_NAMES.values():
                        axis_number = obj._get_axis_number(axis_name)
                        axis_values = obj._get_axis(axis_name)
                        assert axis_number is not None

                        # see if the field is the name of an axis
                        if field == axis_name:

                            # if we have a multi-index, then need to include
                            # the levels
                            if self.is_multi_index:
                                filt = filt.union(Index(self.levels))

                            takers = op(axis_values, filt)
                            return obj.loc(axis=axis_number)[takers]

                        # this might be the name of a file IN an axis
                        elif field in axis_values:

                            # we need to filter on this dimension
                            values = ensure_index(getattr(obj, field).values)
                            filt = ensure_index(filt)

                            # hack until we support reversed dim flags
                            if isinstance(obj, DataFrame):
                                axis_number = 1 - axis_number
                            takers = op(values, filt)
                            return obj.loc(axis=axis_number)[takers]

                    raise ValueError(f"cannot find the field [{field}] for filtering!")

                obj = process_filter(field, filt)

        return obj

    def create_description(
        self,
        complib=None,
        complevel: Optional[int] = None,
        fletcher32: bool = False,
        expectedrows: Optional[int] = None,
    ) -> Dict[str, Any]:
        """ create the description of the table from the axes & values """

        # provided expected rows if its passed
        if expectedrows is None:
            expectedrows = max(self.nrows_expected, 10000)

        d = dict(name="table", expectedrows=expectedrows)

        # description from the axes & values
        d["description"] = {a.cname: a.typ for a in self.axes}

        if complib:
            if complevel is None:
                complevel = self._complevel or 9
            filters = _tables().Filters(
                complevel=complevel,
                complib=complib,
                fletcher32=fletcher32 or self._fletcher32,
            )
            d["filters"] = filters
        elif self._filters is not None:
            d["filters"] = self._filters

        return d

    def read_coordinates(
        self, where=None, start: Optional[int] = None, stop: Optional[int] = None,
    ):
        """select coordinates (row numbers) from a table; return the
        coordinates object
        """

        # validate the version
        self.validate_version(where)

        # infer the data kind
        if not self.infer_axes():
            return False

        # create the selection
        selection = Selection(self, where=where, start=start, stop=stop)
        coords = selection.select_coords()
        if selection.filter is not None:
            for field, op, filt in selection.filter.format():
                data = self.read_column(
                    field, start=coords.min(), stop=coords.max() + 1
                )
                coords = coords[op(data.iloc[coords - coords.min()], filt).values]

        return Index(coords)

    def read_column(
        self,
        column: str,
        where=None,
        start: Optional[int] = None,
        stop: Optional[int] = None,
    ):
        """return a single column from the table, generally only indexables
        are interesting
        """

        # validate the version
        self.validate_version()

        # infer the data kind
        if not self.infer_axes():
            return False

        if where is not None:
            raise TypeError("read_column does not currently accept a where clause")

        # find the axes
        for a in self.axes:
            if column == a.name:

                if not a.is_data_indexable:
                    raise ValueError(
                        f"column [{column}] can not be extracted individually; "
                        "it is not data indexable"
                    )

                # column must be an indexable or a data column
                c = getattr(self.table.cols, column)
                a.set_info(self.info)
                a.convert(
                    c[start:stop],
                    nan_rep=self.nan_rep,
                    encoding=self.encoding,
                    errors=self.errors,
                )
                return Series(_set_tz(a.take_data(), a.tz, True), name=column)

        raise KeyError(f"column [{column}] not found in the table")


class WORMTable(Table):
    """ a write-once read-many table: this format DOES NOT ALLOW appending to a
         table. writing is a one-time operation the data are stored in a format
         that allows for searching the data on disk
         """

    table_type = "worm"

    def read(self, **kwargs):
        """ read the indices and the indexing array, calculate offset rows and
        return """
        raise NotImplementedError("WORMTable needs to implement read")

    def write(self, **kwargs):
        """ write in a format that we can search later on (but cannot append
               to): write out the indices and the values using _write_array
               (e.g. a CArray) create an indexing table so that we can search
        """
        raise NotImplementedError("WORMTable needs to implement write")


class AppendableTable(Table):
    """ support the new appendable table formats """

    _indexables = None
    table_type = "appendable"

    def write(
        self,
        obj,
        axes=None,
        append=False,
        complib=None,
        complevel=None,
        fletcher32=None,
        min_itemsize=None,
        chunksize=None,
        expectedrows=None,
        dropna=False,
        nan_rep=None,
        data_columns=None,
    ):

        if not append and self.is_exists:
            self._handle.remove_node(self.group, "table")

        # create the axes
        self.create_axes(
            axes=axes,
            obj=obj,
            validate=append,
            min_itemsize=min_itemsize,
            nan_rep=nan_rep,
            data_columns=data_columns,
        )

        for a in self.axes:
            a.validate(self, append)

        if not self.is_exists:

            # create the table
            options = self.create_description(
                complib=complib,
                complevel=complevel,
                fletcher32=fletcher32,
                expectedrows=expectedrows,
            )

            # set the table attributes
            self.set_attrs()

            # create the table
            self._handle.create_table(self.group, **options)

        # update my info
        self.attrs.info = self.info

        # validate the axes and set the kinds
        for a in self.axes:
            a.validate_and_set(self, append)

        # add the rows
        self.write_data(chunksize, dropna=dropna)

    def write_data(self, chunksize: Optional[int], dropna: bool = False):
        """ we form the data into a 2-d including indexes,values,mask
            write chunk-by-chunk """

        names = self.dtype.names
        nrows = self.nrows_expected

        # if dropna==True, then drop ALL nan rows
        masks = []
        if dropna:

            for a in self.values_axes:

                # figure the mask: only do if we can successfully process this
                # column, otherwise ignore the mask
                mask = isna(a.data).all(axis=0)
                if isinstance(mask, np.ndarray):
                    masks.append(mask.astype("u1", copy=False))

        # consolidate masks
        if len(masks):
            mask = masks[0]
            for m in masks[1:]:
                mask = mask & m
            mask = mask.ravel()
        else:
            mask = None

        # broadcast the indexes if needed
        indexes = [a.cvalues for a in self.index_axes]
        nindexes = len(indexes)
        bindexes = []
        for i, idx in enumerate(indexes):

            # broadcast to all other indexes except myself
            if i > 0 and i < nindexes:
                repeater = np.prod([indexes[bi].shape[0] for bi in range(0, i)])
                idx = np.tile(idx, repeater)

            if i < nindexes - 1:
                repeater = np.prod(
                    [indexes[bi].shape[0] for bi in range(i + 1, nindexes)]
                )
                idx = np.repeat(idx, repeater)

            bindexes.append(idx)

        # transpose the values so first dimension is last
        # reshape the values if needed
        values = [a.take_data() for a in self.values_axes]
        values = [v.transpose(np.roll(np.arange(v.ndim), v.ndim - 1)) for v in values]
        bvalues = []
        for i, v in enumerate(values):
            new_shape = (nrows,) + self.dtype[names[nindexes + i]].shape
            bvalues.append(values[i].reshape(new_shape))

        # write the chunks
        if chunksize is None:
            chunksize = 100000

        rows = np.empty(min(chunksize, nrows), dtype=self.dtype)
        chunks = int(nrows / chunksize) + 1
        for i in range(chunks):
            start_i = i * chunksize
            end_i = min((i + 1) * chunksize, nrows)
            if start_i >= end_i:
                break

            self.write_data_chunk(
                rows,
                indexes=[a[start_i:end_i] for a in bindexes],
                mask=mask[start_i:end_i] if mask is not None else None,
                values=[v[start_i:end_i] for v in bvalues],
            )

    def write_data_chunk(self, rows, indexes, mask, values):
        """
        Parameters
        ----------
        rows : an empty memory space where we are putting the chunk
        indexes : an array of the indexes
        mask : an array of the masks
        values : an array of the values
        """

        # 0 len
        for v in values:
            if not np.prod(v.shape):
                return

        nrows = indexes[0].shape[0]
        if nrows != len(rows):
            rows = np.empty(nrows, dtype=self.dtype)
        names = self.dtype.names
        nindexes = len(indexes)

        # indexes
        for i, idx in enumerate(indexes):
            rows[names[i]] = idx

        # values
        for i, v in enumerate(values):
            rows[names[i + nindexes]] = v

        # mask
        if mask is not None:
            m = ~mask.ravel().astype(bool, copy=False)
            if not m.all():
                rows = rows[m]

        if len(rows):
            self.table.append(rows)
            self.table.flush()

    def delete(
        self, where=None, start: Optional[int] = None, stop: Optional[int] = None,
    ):

        # delete all rows (and return the nrows)
        if where is None or not len(where):
            if start is None and stop is None:
                nrows = self.nrows
                self._handle.remove_node(self.group, recursive=True)
            else:
                # pytables<3.0 would remove a single row with stop=None
                if stop is None:
                    stop = self.nrows
                nrows = self.table.remove_rows(start=start, stop=stop)
                self.table.flush()
            return nrows

        # infer the data kind
        if not self.infer_axes():
            return None

        # create the selection
        table = self.table
        selection = Selection(self, where, start=start, stop=stop)
        values = selection.select_coords()

        # delete the rows in reverse order
        sorted_series = Series(values).sort_values()
        ln = len(sorted_series)

        if ln:

            # construct groups of consecutive rows
            diff = sorted_series.diff()
            groups = list(diff[diff > 1].index)

            # 1 group
            if not len(groups):
                groups = [0]

            # final element
            if groups[-1] != ln:
                groups.append(ln)

            # initial element
            if groups[0] != 0:
                groups.insert(0, 0)

            # we must remove in reverse order!
            pg = groups.pop()
            for g in reversed(groups):
                rows = sorted_series.take(range(g, pg))
                table.remove_rows(
                    start=rows[rows.index[0]], stop=rows[rows.index[-1]] + 1
                )
                pg = g

            self.table.flush()

        # return the number of rows removed
        return ln


class AppendableFrameTable(AppendableTable):
    """ support the new appendable table formats """

    pandas_kind = "frame_table"
    table_type = "appendable_frame"
    ndim = 2
    obj_type: Type[Union[DataFrame, Series]] = DataFrame

    @property
    def is_transposed(self) -> bool:
        return self.index_axes[0].axis == 1

    def get_object(self, obj):
        """ these are written transposed """
        if self.is_transposed:
            obj = obj.T
        return obj

    def read(
        self,
        where=None,
        columns=None,
        start: Optional[int] = None,
        stop: Optional[int] = None,
    ):

        if not self.read_axes(where=where, start=start, stop=stop):
            return None

        info = (
            self.info.get(self.non_index_axes[0][0], dict())
            if len(self.non_index_axes)
            else dict()
        )
        index = self.index_axes[0].values
        frames = []
        for a in self.values_axes:

            # we could have a multi-index constructor here
            # ensure_index doesn't recognized our list-of-tuples here
            if info.get("type") == "MultiIndex":
                cols = MultiIndex.from_tuples(a.values)
            else:
                cols = Index(a.values)
            names = info.get("names")
            if names is not None:
                cols.set_names(names, inplace=True)

            if self.is_transposed:
                values = a.cvalues
                index_ = cols
                cols_ = Index(index, name=getattr(index, "name", None))
            else:
                values = a.cvalues.T
                index_ = Index(index, name=getattr(index, "name", None))
                cols_ = cols

            # if we have a DataIndexableCol, its shape will only be 1 dim
            if values.ndim == 1 and isinstance(values, np.ndarray):
                values = values.reshape((1, values.shape[0]))

            block = make_block(values, placement=np.arange(len(cols_)), ndim=2)
            mgr = BlockManager([block], [cols_, index_])
            frames.append(DataFrame(mgr))

        if len(frames) == 1:
            df = frames[0]
        else:
            df = concat(frames, axis=1)

        selection = Selection(self, where=where, start=start, stop=stop)
        # apply the selection filters & axis orderings
        df = self.process_axes(df, selection=selection, columns=columns)

        return df


class AppendableSeriesTable(AppendableFrameTable):
    """ support the new appendable table formats """

    pandas_kind = "series_table"
    table_type = "appendable_series"
    ndim = 2
    obj_type = Series
    storage_obj_type = DataFrame

    @property
    def is_transposed(self) -> bool:
        return False

    def get_object(self, obj):
        return obj

    def write(self, obj, data_columns=None, **kwargs):
        """ we are going to write this as a frame table """
        if not isinstance(obj, DataFrame):
            name = obj.name or "values"
            obj = DataFrame({name: obj}, index=obj.index)
            obj.columns = [name]
        return super().write(obj=obj, data_columns=obj.columns.tolist(), **kwargs)

    def read(
        self,
        where=None,
        columns=None,
        start: Optional[int] = None,
        stop: Optional[int] = None,
    ):

        is_multi_index = self.is_multi_index
        if columns is not None and is_multi_index:
            assert isinstance(self.levels, list)  # needed for mypy
            for n in self.levels:
                if n not in columns:
                    columns.insert(0, n)
        s = super().read(where=where, columns=columns, start=start, stop=stop)
        if is_multi_index:
            s.set_index(self.levels, inplace=True)

        s = s.iloc[:, 0]

        # remove the default name
        if s.name == "values":
            s.name = None
        return s


class AppendableMultiSeriesTable(AppendableSeriesTable):
    """ support the new appendable table formats """

    pandas_kind = "series_table"
    table_type = "appendable_multiseries"

    def write(self, obj, **kwargs):
        """ we are going to write this as a frame table """
        name = obj.name or "values"
        obj, self.levels = self.validate_multiindex(obj)
        cols = list(self.levels)
        cols.append(name)
        obj.columns = cols
        return super().write(obj=obj, **kwargs)


class GenericTable(AppendableFrameTable):
    """ a table that read/writes the generic pytables table format """

    pandas_kind = "frame_table"
    table_type = "generic_table"
    ndim = 2
    obj_type = DataFrame

    @property
    def pandas_type(self) -> str:
        return self.pandas_kind

    @property
    def storable(self):
        return getattr(self.group, "table", None) or self.group

    def get_attrs(self):
        """ retrieve our attributes """
        self.non_index_axes = []
        self.nan_rep = None
        self.levels = []

        self.index_axes = [a.infer(self) for a in self.indexables if a.is_an_indexable]
        self.values_axes = [
            a.infer(self) for a in self.indexables if not a.is_an_indexable
        ]
        self.data_columns = [a.name for a in self.values_axes]

    @property
    def indexables(self):
        """ create the indexables from the table description """
        if self._indexables is None:

            d = self.description

            # the index columns is just a simple index
            self._indexables = [GenericIndexCol(name="index", axis=0)]

            for i, n in enumerate(d._v_names):
                assert isinstance(n, str)

                dc = GenericDataIndexableCol(name=n, pos=i, values=[n])
                self._indexables.append(dc)

        return self._indexables

    def write(self, **kwargs):
        raise NotImplementedError("cannot write on an generic table")


class AppendableMultiFrameTable(AppendableFrameTable):
    """ a frame with a multi-index """

    table_type = "appendable_multiframe"
    obj_type = DataFrame
    ndim = 2
    _re_levels = re.compile(r"^level_\d+$")

    @property
    def table_type_short(self) -> str:
        return "appendable_multi"

    def write(self, obj, data_columns=None, **kwargs):
        if data_columns is None:
            data_columns = []
        elif data_columns is True:
            data_columns = obj.columns.tolist()
        obj, self.levels = self.validate_multiindex(obj)
        for n in self.levels:
            if n not in data_columns:
                data_columns.insert(0, n)
        return super().write(obj=obj, data_columns=data_columns, **kwargs)

    def read(
        self,
        where=None,
        columns=None,
        start: Optional[int] = None,
        stop: Optional[int] = None,
    ):

        df = super().read(where=where, columns=columns, start=start, stop=stop)
        df = df.set_index(self.levels)

        # remove names for 'level_%d'
        df.index = df.index.set_names(
            [None if self._re_levels.search(l) else l for l in df.index.names]
        )

        return df


def _reindex_axis(obj, axis: int, labels: Index, other=None):
    ax = obj._get_axis(axis)
    labels = ensure_index(labels)

    # try not to reindex even if other is provided
    # if it equals our current index
    if other is not None:
        other = ensure_index(other)
    if (other is None or labels.equals(other)) and labels.equals(ax):
        return obj

    labels = ensure_index(labels.unique())
    if other is not None:
        labels = ensure_index(other.unique()).intersection(labels, sort=False)
    if not labels.equals(ax):
        slicer: List[Union[slice, Index]] = [slice(None, None)] * obj.ndim
        slicer[axis] = labels
        obj = obj.loc[tuple(slicer)]
    return obj


def _get_info(info, name):
    """ get/create the info for this name """
    try:
        idx = info[name]
    except KeyError:
        idx = info[name] = dict()
    return idx


# tz to/from coercion


def _get_tz(tz):
    """ for a tz-aware type, return an encoded zone """
    zone = timezones.get_timezone(tz)
    if zone is None:
        zone = tz.utcoffset().total_seconds()
    return zone


def _set_tz(values, tz, preserve_UTC: bool = False, coerce: bool = False):
    """
    coerce the values to a DatetimeIndex if tz is set
    preserve the input shape if possible

    Parameters
    ----------
    values : ndarray
    tz : string/pickled tz object
    preserve_UTC : bool,
        preserve the UTC of the result
    coerce : if we do not have a passed timezone, coerce to M8[ns] ndarray
    """
    if tz is not None:
        name = getattr(values, "name", None)
        values = values.ravel()
        tz = timezones.get_timezone(_ensure_decoded(tz))
        values = DatetimeIndex(values, name=name)
        if values.tz is None:
            values = values.tz_localize("UTC").tz_convert(tz)
        if preserve_UTC:
            if tz == "UTC":
                values = list(values)
    elif coerce:
        values = np.asarray(values, dtype="M8[ns]")

    return values


def _convert_index(name: str, index: Index, encoding=None, errors="strict"):
    assert isinstance(name, str)

    index_name = index.name

    if isinstance(index, DatetimeIndex):
        converted = index.asi8
        return IndexCol(
            name,
            converted,
            "datetime64",
            _tables().Int64Col(),
            freq=index.freq,
            tz=index.tz,
            index_name=index_name,
        )
    elif isinstance(index, TimedeltaIndex):
        converted = index.asi8
        return IndexCol(
            name,
            converted,
            "timedelta64",
            _tables().Int64Col(),
            freq=index.freq,
            index_name=index_name,
        )
    elif isinstance(index, (Int64Index, PeriodIndex)):
        atom = _tables().Int64Col()
        # avoid to store ndarray of Period objects
        return IndexCol(
            name,
            index._ndarray_values,
            "integer",
            atom,
            freq=getattr(index, "freq", None),
            index_name=index_name,
        )

    if isinstance(index, MultiIndex):
        raise TypeError("MultiIndex not supported here!")

    inferred_type = lib.infer_dtype(index, skipna=False)
    # we wont get inferred_type of "datetime64" or "timedelta64" as these
    #  would go through the DatetimeIndex/TimedeltaIndex paths above

    values = np.asarray(index)

    if inferred_type == "date":
        converted = np.asarray([v.toordinal() for v in values], dtype=np.int32)
        return IndexCol(
            name, converted, "date", _tables().Time32Col(), index_name=index_name,
        )
    elif inferred_type == "string":
        # atom = _tables().ObjectAtom()
        # return np.asarray(values, dtype='O'), 'object', atom

        converted = _convert_string_array(values, encoding, errors)
        itemsize = converted.dtype.itemsize
        return IndexCol(
            name,
            converted,
            "string",
            _tables().StringCol(itemsize),
            itemsize=itemsize,
            index_name=index_name,
        )

    elif inferred_type == "integer":
        # take a guess for now, hope the values fit
        atom = _tables().Int64Col()
        return IndexCol(
            name,
            np.asarray(values, dtype=np.int64),
            "integer",
            atom,
            index_name=index_name,
        )
    elif inferred_type == "floating":
        atom = _tables().Float64Col()
        return IndexCol(
            name,
            np.asarray(values, dtype=np.float64),
            "float",
            atom,
            index_name=index_name,
        )
    else:
        atom = _tables().ObjectAtom()
        return IndexCol(
            name, np.asarray(values, dtype="O"), "object", atom, index_name=index_name,
        )


def _unconvert_index(data, kind: str, encoding=None, errors="strict"):
    index: Union[Index, np.ndarray]

    if kind == "datetime64":
        index = DatetimeIndex(data)
    elif kind == "timedelta64":
        index = TimedeltaIndex(data)
    elif kind == "date":
        try:
            index = np.asarray([date.fromordinal(v) for v in data], dtype=object)
        except (ValueError):
            index = np.asarray([date.fromtimestamp(v) for v in data], dtype=object)
    elif kind in ("integer", "float"):
        index = np.asarray(data)
    elif kind in ("string"):
        index = _unconvert_string_array(
            data, nan_rep=None, encoding=encoding, errors=errors
        )
    elif kind == "object":
        index = np.asarray(data[0])
    else:  # pragma: no cover
        raise ValueError(f"unrecognized index type {kind}")
    return index


def _convert_string_array(data, encoding, errors, itemsize=None):
    """
    we take a string-like that is object dtype and coerce to a fixed size
    string type

    Parameters
    ----------
    data : a numpy array of object dtype
    encoding : None or string-encoding
    errors : handler for encoding errors
    itemsize : integer, optional, defaults to the max length of the strings

    Returns
    -------
    data in a fixed-length string dtype, encoded to bytes if needed
    """

    # encode if needed
    if encoding is not None and len(data):
        data = (
            Series(data.ravel()).str.encode(encoding, errors).values.reshape(data.shape)
        )

    # create the sized dtype
    if itemsize is None:
        ensured = ensure_object(data.ravel())
        itemsize = max(1, libwriters.max_len_string_array(ensured))

    data = np.asarray(data, dtype=f"S{itemsize}")
    return data


def _unconvert_string_array(data, nan_rep=None, encoding=None, errors="strict"):
    """
    inverse of _convert_string_array

    Parameters
    ----------
    data : fixed length string dtyped array
    nan_rep : the storage repr of NaN, optional
    encoding : the encoding of the data, optional
    errors : handler for encoding errors, default 'strict'

    Returns
    -------
    an object array of the decoded data

    """
    shape = data.shape
    data = np.asarray(data.ravel(), dtype=object)

    # guard against a None encoding (because of a legacy
    # where the passed encoding is actually None)
    encoding = _ensure_encoding(encoding)
    if encoding is not None and len(data):

        itemsize = libwriters.max_len_string_array(ensure_object(data))
        dtype = f"U{itemsize}"

        if isinstance(data[0], bytes):
            data = Series(data).str.decode(encoding, errors=errors).values
        else:
            data = data.astype(dtype, copy=False).astype(object, copy=False)

    if nan_rep is None:
        nan_rep = "nan"

    data = libwriters.string_array_replace_from_nan_rep(data, nan_rep)
    return data.reshape(shape)


def _maybe_convert(values: np.ndarray, val_kind, encoding, errors):
    val_kind = _ensure_decoded(val_kind)
    if _need_convert(val_kind):
        conv = _get_converter(val_kind, encoding, errors)
        # conv = np.frompyfunc(conv, 1, 1)
        values = conv(values)
    return values


def _get_converter(kind: str, encoding, errors):
    if kind == "datetime64":
        return lambda x: np.asarray(x, dtype="M8[ns]")
    elif kind == "string":
        return lambda x: _unconvert_string_array(x, encoding=encoding, errors=errors)
    else:  # pragma: no cover
        raise ValueError(f"invalid kind {kind}")


def _need_convert(kind) -> bool:
    if kind in ("datetime64", "string"):
        return True
    return False


class Selection:
    """
    Carries out a selection operation on a tables.Table object.

    Parameters
    ----------
    table : a Table object
    where : list of Terms (or convertible to)
    start, stop: indices to start and/or stop selection

    """

    def __init__(
        self,
        table: Table,
        where=None,
        start: Optional[int] = None,
        stop: Optional[int] = None,
    ):
        self.table = table
        self.where = where
        self.start = start
        self.stop = stop
        self.condition = None
        self.filter = None
        self.terms = None
        self.coordinates = None

        if is_list_like(where):

            # see if we have a passed coordinate like
            try:
                inferred = lib.infer_dtype(where, skipna=False)
                if inferred == "integer" or inferred == "boolean":
                    where = np.asarray(where)
                    if where.dtype == np.bool_:
                        start, stop = self.start, self.stop
                        if start is None:
                            start = 0
                        if stop is None:
                            stop = self.table.nrows
                        self.coordinates = np.arange(start, stop)[where]
                    elif issubclass(where.dtype.type, np.integer):
                        if (self.start is not None and (where < self.start).any()) or (
                            self.stop is not None and (where >= self.stop).any()
                        ):
                            raise ValueError(
                                "where must have index locations >= start and < stop"
                            )
                        self.coordinates = where

            except ValueError:
                pass

        if self.coordinates is None:

            self.terms = self.generate(where)

            # create the numexpr & the filter
            if self.terms is not None:
                self.condition, self.filter = self.terms.evaluate()

    def generate(self, where):
        """ where can be a : dict,list,tuple,string """
        if where is None:
            return None

        q = self.table.queryables()
        try:
            return PyTablesExpr(where, queryables=q, encoding=self.table.encoding)
        except NameError:
            # raise a nice message, suggesting that the user should use
            # data_columns
            qkeys = ",".join(q.keys())
            raise ValueError(
                f"The passed where expression: {where}\n"
                "            contains an invalid variable reference\n"
                "            all of the variable references must be a "
                "reference to\n"
                "            an axis (e.g. 'index' or 'columns'), or a "
                "data_column\n"
                f"            The currently defined references are: {qkeys}\n"
            )

    def select(self):
        """
        generate the selection
        """
        if self.condition is not None:
            return self.table.table.read_where(
                self.condition.format(), start=self.start, stop=self.stop
            )
        elif self.coordinates is not None:
            return self.table.table.read_coordinates(self.coordinates)
        return self.table.table.read(start=self.start, stop=self.stop)

    def select_coords(self):
        """
        generate the selection
        """
        start, stop = self.start, self.stop
        nrows = self.table.nrows
        if start is None:
            start = 0
        elif start < 0:
            start += nrows
        if self.stop is None:
            stop = nrows
        elif stop < 0:
            stop += nrows

        if self.condition is not None:
            return self.table.table.get_where_list(
                self.condition.format(), start=start, stop=stop, sort=True
            )
        elif self.coordinates is not None:
            return self.coordinates

        return np.arange(start, stop)
