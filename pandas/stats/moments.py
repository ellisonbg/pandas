"""
Provides rolling statistical moments and related descriptive
statistics implemented in Cython
"""
from __future__ import division

from functools import wraps

from numpy import NaN
import numpy as np

from pandas.core.api import DataFrame, Series, notnull
import pandas._tseries as _tseries

from pandas.util.decorators import Substitution, Appender

__all__ = ['rolling_count', 'rolling_max', 'rolling_min',
           'rolling_sum', 'rolling_mean', 'rolling_std', 'rolling_cov',
           'rolling_corr', 'rolling_var', 'rolling_skew', 'rolling_kurt',
           'rolling_quantile', 'rolling_median', 'rolling_apply',
           'rolling_corr_pairwise',
           'ewma', 'ewmvar', 'ewmstd', 'ewmvol', 'ewmcorr', 'ewmcov']

#-------------------------------------------------------------------------------
# Docs

_doc_template = """
%s

Parameters
----------
%s
window : Number of observations used for calculating statistic
min_periods : int
    Minimum number of observations in window required to have a value
time_rule : {None, 'WEEKDAY', 'EOM', 'W@MON', ...}, default=None
    Name of time rule to conform to before computing statistic

Returns
-------
%s
"""


_ewm_doc = r"""%s

Parameters
----------
%s
com : float. optional
    Center of mass: \alpha = com / (1 + com),
span : float, optional
    Specify decay in terms of span, \alpha = 2 / (span + 1)
min_periods : int, default 0
    Number of observations in sample to require (only affects
    beginning)
time_rule : {None, 'WEEKDAY', 'EOM', 'W@MON', ...}, default None
    Name of time rule to conform to before computing statistic
%s
Notes
-----
Either center of mass or span must be specified

EWMA is sometimes specified using a "span" parameter s, we have have that the
decay parameter \alpha is related to the span as :math:`\alpha = 1 - 2 / (s + 1)
= c / (1 + c)`

where c is the center of mass. Given a span, the associated center of mass is
:math:`c = (s - 1) / 2`

So a "20-day EWMA" would have center 9.5.

Returns
-------
y : type of input argument
"""

_type_of_input = "y : type of input argument"

_flex_retval = """y : type depends on inputs
    DataFrame / DataFrame -> DataFrame (matches on columns)
    DataFrame / Series -> Computes result for each column
    Series / Series -> Series"""

_unary_arg = "arg : Series, DataFrame"

_binary_arg_flex = """arg1 : Series, DataFrame, or ndarray
arg2 : Series, DataFrame, or ndarray"""

_binary_arg = """arg1 : Series, DataFrame, or ndarray
arg2 : Series, DataFrame, or ndarray"""

_bias_doc = r"""bias : boolean, default False
    Use a standard estimation bias correction
"""
def rolling_count(arg, window, time_rule=None):
    """
    Rolling count of number of non-NaN observations inside provided window.

    Parameters
    ----------
    arg :  DataFrame or numpy ndarray-like
    window : Number of observations used for calculating statistic

    Returns
    -------
    rolling_count : type of caller
    """
    arg = _conv_timerule(arg, time_rule)
    window = min(window, len(arg))

    return_hook, values = _process_data_structure(arg, kill_inf=False)

    converted = np.isfinite(values).astype(float)
    result = rolling_sum(converted, window, min_periods=1,
                         time_rule=time_rule)

    # putmask here?
    result[np.isnan(result)] = 0

    return return_hook(result)

@Substitution("Unbiased moving covariance", _binary_arg_flex, _flex_retval)
@Appender(_doc_template)
def rolling_cov(arg1, arg2, window, min_periods=None, time_rule=None):
    def _get_cov(X, Y):
        mean = lambda x: rolling_mean(x, window, min_periods, time_rule)
        count = rolling_count(X + Y, window, time_rule)
        bias_adj = count / (count - 1)
        return (mean(X * Y) - mean(X) * mean(Y)) * bias_adj
    return _flex_binary_moment(arg1, arg2, _get_cov)

@Substitution("Moving sample correlation", _binary_arg_flex, _flex_retval)
@Appender(_doc_template)
def rolling_corr(arg1, arg2, window, min_periods=None, time_rule=None):
    def _get_corr(a, b):
        num = rolling_cov(a, b, window, min_periods, time_rule)
        den  = (rolling_std(a, window, min_periods, time_rule) *
                rolling_std(b, window, min_periods, time_rule))
        return num / den
    return _flex_binary_moment(arg1, arg2, _get_corr)

def _flex_binary_moment(arg1, arg2, f):
    if isinstance(arg1, np.ndarray) and isinstance(arg2, np.ndarray):
        X, Y = _prep_binary(arg1, arg2)
        return f(X, Y)
    elif isinstance(arg1, DataFrame):
        results = {}
        if isinstance(arg2, DataFrame):
            X, Y = arg1.align(arg2, join='outer')
            X = X + 0 * Y
            Y = Y + 0 * X
            res_columns = arg1.columns.union(arg2.columns)
            for col in res_columns:
                if col in X and col in Y:
                    results[col] = f(X[col], Y[col])
        else:
            res_columns = arg1.columns
            X, Y = arg1.align(arg2, axis=0, join='outer')
            results = {}

            for col in res_columns:
                results[col] = f(X[col], Y)

        return DataFrame(results, index=X.index, columns=res_columns)
    else:
        return _flex_binary_moment(arg2, arg1, f)

def rolling_corr_pairwise(df, window, min_periods=None):
    """
    Computes pairwise rolling correlation matrices as Panel whose items are
    dates

    Parameters
    ----------
    df : DataFrame
    window : int
    min_periods : int, default None

    Returns
    -------
    correls : Panel
    """
    from pandas import Panel
    from collections import defaultdict

    all_results = defaultdict(dict)

    for i, k1 in enumerate(df.columns):
        for k2 in df.columns[i:]:
            corr = rolling_corr(df[k1], df[k2], window,
                                min_periods=min_periods)
            all_results[k1][k2] = corr
            all_results[k2][k1] = corr

    return Panel.from_dict(all_results).swapaxes('items', 'major')

def _rolling_moment(arg, window, func, minp, axis=0, time_rule=None):
    """
    Rolling statistical measure using supplied function. Designed to be
    used with passed-in Cython array-based functions.

    Parameters
    ----------
    arg :  DataFrame or numpy ndarray-like
    window : Number of observations used for calculating statistic
    func : Cython function to compute rolling statistic on raw series
    minp : int
        Minimum number of observations required to have a value
    axis : int, default 0
    time_rule : string or DateOffset
        Time rule to conform to before computing result

    Returns
    -------
    y : type of input
    """
    arg = _conv_timerule(arg, time_rule)
    calc = lambda x: func(x, window, minp=minp)
    return_hook, values = _process_data_structure(arg)
    # actually calculate the moment. Faster way to do this?
    result = np.apply_along_axis(calc, axis, values)

    return return_hook(result)

def _process_data_structure(arg, kill_inf=True):
    if isinstance(arg, DataFrame):
        return_hook = lambda v: type(arg)(v, index=arg.index,
                                          columns=arg.columns)
        values = arg.values
    elif isinstance(arg, Series):
        values = arg.values
        return_hook = lambda v: Series(v, arg.index)
    else:
        return_hook = lambda v: v
        values = arg

    if not issubclass(values.dtype.type, float):
        values = values.astype(float)

    if kill_inf:
        values = values.copy()
        values[np.isinf(values)] = np.NaN

    return return_hook, values

#-------------------------------------------------------------------------------
# Exponential moving moments

def _get_center_of_mass(com, span):
    if span is not None:
        if com is not None:
            raise Exception("com and span are mutually exclusive")

        # convert span to center of mass
        com = (span - 1) / 2.

    elif com is None:
        raise Exception("Must pass either com or span")

    return float(com)

@Substitution("Exponentially-weighted moving average", _unary_arg, "")
@Appender(_ewm_doc)
def ewma(arg, com=None, span=None, min_periods=0, time_rule=None):
    com = _get_center_of_mass(com, span)
    arg = _conv_timerule(arg, time_rule)

    def _ewma(v):
        result = _tseries.ewma(v, com)
        first_index = _first_valid_index(v)
        result[first_index : first_index + min_periods] = NaN
        return result

    return_hook, values = _process_data_structure(arg)
    output = np.apply_along_axis(_ewma, 0, values)
    return return_hook(output)

def _first_valid_index(arr):
    # argmax scans from left
    return notnull(arr).argmax()

@Substitution("Exponentially-weighted moving variance", _unary_arg, _bias_doc)
@Appender(_ewm_doc)
def ewmvar(arg, com=None, span=None, min_periods=0, bias=False,
           time_rule=None):
    com = _get_center_of_mass(com, span)
    arg = _conv_timerule(arg, time_rule)
    moment2nd = ewma(arg * arg, com=com, min_periods=min_periods)
    moment1st = ewma(arg, com=com, min_periods=min_periods)

    result = moment2nd - moment1st ** 2
    if not bias:
        result *= (1.0 + 2.0 * com) / (2.0 * com)

    return result

@Substitution("Exponentially-weighted moving std", _unary_arg, _bias_doc)
@Appender(_ewm_doc)
def ewmstd(arg, com=None, span=None, min_periods=0, bias=False,
           time_rule=None):
    result = ewmvar(arg, com=com, span=span, time_rule=time_rule,
                    min_periods=min_periods, bias=bias)
    return np.sqrt(result)

ewmvol = ewmstd

@Substitution("Exponentially-weighted moving covariance", _binary_arg, "")
@Appender(_ewm_doc)
def ewmcov(arg1, arg2, com=None, span=None, min_periods=0, bias=False,
           time_rule=None):
    X, Y = _prep_binary(arg1, arg2)

    X = _conv_timerule(X, time_rule)
    Y = _conv_timerule(Y, time_rule)

    mean = lambda x: ewma(x, com=com, span=span, min_periods=min_periods)

    result = (mean(X*Y) - mean(X) * mean(Y))

    if not bias:
        result *= (1.0 + 2.0 * com) / (2.0 * com)

    return result

@Substitution("Exponentially-weighted moving " "correlation", _binary_arg, "")
@Appender(_ewm_doc)
def ewmcorr(arg1, arg2, com=None, span=None, min_periods=0,
            time_rule=None):
    X, Y = _prep_binary(arg1, arg2)

    X = _conv_timerule(X, time_rule)
    Y = _conv_timerule(Y, time_rule)

    mean = lambda x: ewma(x, com=com, span=span, min_periods=min_periods)
    var = lambda x: ewmvar(x, com=com, span=span, min_periods=min_periods,
                           bias=True)
    return (mean(X*Y) - mean(X)*mean(Y)) / np.sqrt(var(X) * var(Y))

def _prep_binary(arg1, arg2):
    if not isinstance(arg2, type(arg1)):
        raise Exception('Input arrays must be of the same type!')

    # mask out values, this also makes a common index...
    X = arg1 + 0 * arg2
    Y = arg2 + 0 * arg1

    return X, Y

#-------------------------------------------------------------------------------
# Python interface to Cython functions

def _conv_timerule(arg, time_rule):
    types = (DataFrame, Series)
    if time_rule is not None and isinstance(arg, types):
        # Conform to whatever frequency needed.
        arg = arg.asfreq(time_rule)

    return arg

def _require_min_periods(p):
    def _check_func(minp, window):
        if minp is None:
            return window
        else:
            return max(p, minp)
    return _check_func

def _use_window(minp, window):
    if minp is None:
        return window
    else:
        return minp

def _rolling_func(func, desc, check_minp=_use_window):
    @Substitution(desc, _unary_arg, _type_of_input)
    @Appender(_doc_template)
    @wraps(func)
    def f(arg, window, min_periods=None, time_rule=None):
        def call_cython(arg, window, minp):
            minp = check_minp(minp, window)
            return func(arg, window, minp)
        return _rolling_moment(arg, window, call_cython, min_periods,
                               time_rule=time_rule)

    return f

rolling_max = _rolling_func(_tseries.roll_max, 'Moving maximum')
rolling_min = _rolling_func(_tseries.roll_min, 'Moving minimum')
rolling_sum = _rolling_func(_tseries.roll_sum, 'Moving sum')
rolling_mean = _rolling_func(_tseries.roll_mean, 'Moving mean')
rolling_median = _rolling_func(_tseries.roll_median_cython, 'Moving median')

_ts_std = lambda *a, **kw: np.sqrt(_tseries.roll_var(*a, **kw))
rolling_std = _rolling_func(_ts_std, 'Unbiased moving standard deviation',
                            check_minp=_require_min_periods(2))
rolling_var = _rolling_func(_tseries.roll_var, 'Unbiased moving variance',
                            check_minp=_require_min_periods(2))
rolling_skew = _rolling_func(_tseries.roll_skew, 'Unbiased moving skewness',
                             check_minp=_require_min_periods(3))
rolling_kurt = _rolling_func(_tseries.roll_kurt, 'Unbiased moving kurtosis',
                             check_minp=_require_min_periods(4))

def rolling_quantile(arg, window, quantile, min_periods=None, time_rule=None):
    """Moving quantile

    Parameters
    ----------
    arg : Series, DataFrame
    window : Number of observations used for calculating statistic
    quantile : 0 <= quantile <= 1
    min_periods : int
        Minimum number of observations in window required to have a value
    time_rule : {None, 'WEEKDAY', 'EOM', 'W@MON', ...}, default=None
        Name of time rule to conform to before computing statistic

    Returns
    -------
    y : type of input argument
    """

    def call_cython(arg, window, minp):
        minp = _use_window(minp, window)
        return _tseries.roll_quantile(arg, window, minp, quantile)
    return _rolling_moment(arg, window, call_cython, min_periods,
                           time_rule=time_rule)

def rolling_apply(arg, window, func, min_periods=None, time_rule=None):
    """Generic moving function application

    Parameters
    ----------
    arg : Series, DataFrame
    window : Number of observations used for calculating statistic
    func : function
        Must produce a single value from an ndarray input
    min_periods : int
        Minimum number of observations in window required to have a value
    time_rule : {None, 'WEEKDAY', 'EOM', 'W@MON', ...}, default=None
        Name of time rule to conform to before computing statistic

    Returns
    -------
    y : type of input argument
    """
    def call_cython(arg, window, minp):
        minp = _use_window(minp, window)
        return _tseries.roll_generic(arg, window, minp, func)
    return _rolling_moment(arg, window, call_cython, min_periods,
                           time_rule=time_rule)
