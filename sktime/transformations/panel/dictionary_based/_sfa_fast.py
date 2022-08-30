# -*- coding: utf-8 -*-
"""Symbolic Fourier Approximation (SFA) Transformer.

Configurable SFA transform for discretising time series into words.

"""

__author__ = ["Patrick Schäfer"]
__all__ = ["SFAFast"]

import math
import sys
from warnings import simplefilter

import numpy as np
import pandas as pd
from numba import (
    NumbaPendingDeprecationWarning,
    NumbaTypeSafetyWarning,
    njit,
    objmode,
    prange,
)
from numba.core import types
from numba.typed import Dict
from scipy.sparse import csr_matrix
from sklearn.feature_selection import chi2, f_classif
from sklearn.preprocessing import KBinsDiscretizer
from sklearn.tree import DecisionTreeClassifier
from sklearn.utils import check_random_state

from sktime.transformations.base import BaseTransformer
from sktime.utils.validation.panel import check_X

# The binning methods to use: equi-depth, equi-width, information gain or kmeans
binning_methods = {"equi-depth", "equi-width", "information-gain", "kmeans", "quantile"}

simplefilter(action="ignore", category=NumbaPendingDeprecationWarning)
simplefilter(action="ignore", category=NumbaTypeSafetyWarning)


class SFAFast(BaseTransformer):
    """Symbolic Fourier Approximation (SFA) Transformer.

    Overview: for each series:
        run a sliding window across the series
        for each window
            shorten the series with DFT
            discretise the shortened series into bins set by MFC
            form a word from these discrete values
    by default SFA produces a single word per series (window_size=0)
    if a window is used, it forms a histogram of counts of words.

    Parameters
    ----------
        word_length:         int, default = 8
            length of word to shorten window to (using PAA)

        alphabet_size:       int, default = 4
            number of values to discretise each value to

        force_alphabet_size_two:    bool, default=True
            if set to True, will apply binning with alphabet of size 4 but transform
            with only alphabet of size 2.

        window_size:         int, default = 12
            size of window for sliding. Input series
            length for whole series transform

        norm:                boolean, default = False
            mean normalise words by dropping first fourier coefficient

        binning_method:      {"equi-depth", "equi-width", "information-gain", "kmeans",
                              "quantile"},
                             default="equi-depth"
            the binning method used to derive the breakpoints.

        anova:               boolean, default = False
            If True, the Fourier coefficient selection is done via a one-way
            ANOVA test. If False, the first Fourier coefficients are selected.
            Only applicable if labels are given

        variance:               boolean, default = False
            If True, the Fourier coefficient selection is done via the largest
            variance. If False, the first Fourier coefficients are selected.
            Only applicable if labels are given

        save_words:          boolean, default = False
            whether to save the words generated for each series (default False)

        bigrams:             boolean, default = False
            whether to create bigrams of SFA words

        feature_selection: {"chi2", "none", "random"}, default: chi2
            Sets the feature selections strategy to be used. Chi2 reduces the number
            of words significantly and is thus much faster (preferred). Random also
            reduces the number significantly. None applies not feature selectiona and
            yields large bag of words, e.g. much memory may be needed.

        p_threshold:  int, default=0.05 (disabled by default)
            If feature_selection=chi2 is chosen, feature selection is applied based on
            the chi-squared test. This is the p-value threshold to use for chi-squared
            test on bag-of-words (lower means more strict). 1 indicates that the test
            should not be performed.

        max_feature_count:  int, default=256
            If feature_selection=random is chosen, this parameter defines the number of
            randomly chosen unique words used.

        skip_grams:     boolean, default = False
            whether to create skip-grams of SFA words

        return_sparse:  boolean, default=True
            if set to true, a scipy sparse matrix will be returned as BOP model.
            If set to false a dense array will be returned as BOP model. Sparse
            arrays are much more compact.

        n_jobs:     int, optional, default = 1
            The number of jobs to run in parallel for both `transform`.
            ``-1`` means using all processors.

        return_pandas_data_series:          boolean, default = False
            set to true to return Pandas Series as a result of transform.
            setting to true reduces speed significantly but is required for
            automatic test.

    Attributes
    ----------
    breakpoints: = []
    num_insts = 0
    num_atts = 0


    References
    ----------
    .. [1] Schäfer, Patrick, and Mikael Högqvist. "SFA: a symbolic fourier approximation
    and  index for similarity search in high dimensional datasets." Proceedings of the
    15th international conference on extending database technology. 2012.
    """

    _tags = {
        "univariate-only": True,
        "scitype:transform-input": "Series",
        # what is the scitype of X: Series, or Panel
        "scitype:transform-output": "Series",
        # what scitype is returned: Primitives, Series, Panel
        "scitype:instancewise": False,  # is this an instance-wise transform?
        "X_inner_mtype": "numpy3D",  # which mtypes do _fit/_predict support for X?
        "y_inner_mtype": "pd_Series_Table",  # which mtypes does y require?
        "requires_y": True,  # does y need to be passed in fit?
    }

    def __init__(
        self,
        word_length=8,
        alphabet_size=4,
        window_size=12,
        norm=False,
        binning_method="equi-depth",
        anova=False,
        variance=False,
        bigrams=False,
        skip_grams=False,
        save_words=False,
        force_alphabet_size_two=True,
        feature_selection="none",
        max_feature_count=256,
        p_threshold=0.05,
        random_state=None,
        return_sparse=True,
        return_pandas_data_series=False,
        n_jobs=1,
    ):
        self.dfts = []
        self.breakpoints = []

        # we cannot select more than window_size many letters in a word
        offset = 2 if norm else 0
        self.word_length = word_length
        self.word_length_actual = min(window_size - offset, word_length)
        self.dft_length = (
            window_size - offset
            if (anova or variance) is True
            else self.word_length_actual
        )
        # make dft_length an even number (same number of reals and imags)
        self.dft_length = self.dft_length + self.dft_length % 2
        self.word_length_actual = self.word_length_actual + self.word_length_actual % 2

        self.support = np.arange(self.word_length_actual)

        self.alphabet_size = alphabet_size
        self.window_size = window_size

        self.norm = norm
        self.inverse_sqrt_win_size = 1.0 / math.sqrt(window_size)

        self.binning_dft = None
        self.save_words = save_words

        self.binning_method = binning_method
        self.anova = anova
        self.variance = variance

        self.bigrams = bigrams
        self.skip_grams = skip_grams
        self.n_jobs = n_jobs
        self.force_alphabet_size_two = force_alphabet_size_two

        self.n_instances = 0
        self.series_length = 0

        self.letter_bits = 0
        self.word_bits = 0
        self.max_bits = 0

        # Feature selection part
        self.feature_selection = feature_selection
        self.max_feature_count = max_feature_count
        self.feature_count = 0
        self.relevant_features = None

        # feature selection is applied based on the chi-squared test.
        self.p_threshold = p_threshold

        self.return_sparse = return_sparse
        self.return_pandas_data_series = return_pandas_data_series

        self.random_state = random_state

        super(SFAFast, self).__init__()

        if not return_pandas_data_series:
            self._output_convert = "off"

    def fit_transform(self, X, y=None):
        """Fit to data, then transform it."""
        if self.alphabet_size < 2:
            raise ValueError("Alphabet size must be an integer greater than 2")

        if self.binning_method == "information-gain" and y is None:
            raise ValueError(
                "Class values must be provided for information gain binning"
            )

        if self.variance and self.anova:
            raise ValueError("Please set either variance or anova feature selection")

        if self.binning_method not in binning_methods:
            raise TypeError("binning_method must be one of: ", binning_methods)

        self.letter_bits = np.uint32(math.ceil(math.log2(self.alphabet_size)))
        self.word_bits = self.word_length_actual * self.letter_bits
        self.max_bits = np.uint32(
            self.word_bits * 2 if self.bigrams else self.word_bits
        )

        X = check_X(X, enforce_univariate=True, coerce_to_numpy=True)
        X = X.squeeze(1)

        self.n_instances, self.series_length = X.shape
        self.breakpoints = self._binning(X, y)
        self._is_fitted = True

        # force alphabet of size 2
        if self.force_alphabet_size_two and self.breakpoints.shape[1] == 4:
            bp = np.zeros((self.breakpoints.shape[0], 2))
            bp[:, 0] = self.breakpoints[:, 1]
            bp[:, 1] = np.inf
            self.breakpoints = bp

        words, dfts = _transform_case(
            X,
            self.window_size,
            self.dft_length,
            self.norm,
            self.support,
            self.anova,
            self.variance,
            self.breakpoints,
            self.letter_bits,
            self.bigrams,
            self.skip_grams,
            self.inverse_sqrt_win_size,
        )

        if self.save_words:
            self.dfts = dfts

        # fitting: learns the feature selection strategy, too
        return self.transform_to_bag(words, y)

    def fit(self, X, y=None):
        """Calculate word breakpoints using MCB or IGB.

        Parameters
        ----------
        X : pandas DataFrame or 3d numpy array, input time series.
        y : array_like, target values (optional, ignored).

        Returns
        -------
        self: object
        """
        self.fit_transform(X, y)
        return self

    def _transform(self, X, y=None):
        """Transform data into SFA words.

        Parameters
        ----------
        X : pandas DataFrame or 3d numpy array, input time series.
        y : array_like, target values (optional, ignored).

        Returns
        -------
        List of dictionaries containing SFA words
        """
        # self.check_is_fitted()
        # X = check_X(X, enforce_univariate=True, coerce_to_numpy=True)
        X = X.squeeze(1)

        words, dfts = _transform_case(
            X,
            self.window_size,
            self.dft_length,
            self.norm,
            self.support,
            self.anova,
            self.variance,
            self.breakpoints,
            self.letter_bits,
            self.bigrams,
            self.skip_grams,
            self.inverse_sqrt_win_size,
        )

        if self.save_words:
            self.dfts = dfts

        # TODO count subgroups of two letters of the words?

        # transform: applies the feature selection strategy
        empty_dict = Dict.empty(
            key_type=types.uint32,
            value_type=types.uint32,
        )

        # transform
        bags = create_bag_transform(
            self.feature_count,
            self.feature_selection,
            self.relevant_features if self.relevant_features else empty_dict,
            words,
            self.bigrams,
        )[0]

        if self.return_pandas_data_series:
            bb = pd.DataFrame()
            bb[0] = [pd.Series(bag) for bag in bags]
            return bb
        elif self.return_sparse:
            bags = csr_matrix(bags, dtype=np.uint32)
        return bags

    def transform_to_bag(self, words, y=None):
        """Transform words to bag-of-pattern and apply feature selection."""
        bag_of_words = None
        rng = check_random_state(self.random_state)

        if self.feature_selection == "none" and not self.bigrams:
            bag_of_words = create_bag_none(
                self.breakpoints,
                words.shape[0],
                words,
                self.word_length_actual,
            )
        else:
            feature_names = create_feature_names(words)

            if self.feature_selection == "none" and self.bigrams:
                feature_count = len(list(feature_names))
                relevant_features_idx = np.arange(feature_count, dtype=np.uint32)
                bag_of_words, self.relevant_features = create_bag_feature_selection(
                    words.shape[0],
                    relevant_features_idx,
                    np.array(list(feature_names)),
                    words,
                )

            # Random feature selection
            elif self.feature_selection == "random":
                feature_count = min(self.max_feature_count, len(feature_names))
                relevant_features_idx = rng.choice(
                    len(feature_names), replace=False, size=feature_count
                )
                bag_of_words, self.relevant_features = create_bag_feature_selection(
                    words.shape[0],
                    relevant_features_idx,
                    np.array(list(feature_names)),
                    words,
                )

            # Chi-squared feature selection
            elif self.feature_selection == "chi2":
                feature_count = len(list(feature_names))
                relevant_features_idx = np.arange(feature_count, dtype=np.uint32)
                bag_of_words, _ = create_bag_feature_selection(
                    words.shape[0],
                    relevant_features_idx,
                    np.array(list(feature_names)),
                    words,
                )

                chi2_statistics, p = chi2(bag_of_words, y)
                # feature_count = min(self.max_feature_count, len(feature_names))
                # relevant_features_idx = np.argsort(p)[:feature_count]
                relevant_features_idx = np.where(p <= self.p_threshold)[0]

                self.relevant_features = Dict.empty(
                    key_type=types.uint32, value_type=types.uint32
                )
                for k, v in zip(
                    np.array(list(feature_names))[relevant_features_idx],
                    np.arange(len(relevant_features_idx)),
                ):
                    self.relevant_features[k] = v

                # select subset of features
                bag_of_words = bag_of_words[:, relevant_features_idx]

        self.feature_count = bag_of_words.shape[1]

        if self.return_pandas_data_series:
            bb = pd.DataFrame()
            bb[0] = [pd.Series(bag) for bag in bag_of_words]
            return bb
        elif self.return_sparse:
            bag_of_words = csr_matrix(bag_of_words, dtype=np.uint32)
        return bag_of_words

    def _binning(self, X, y=None):
        dft = _binning_dft(
            X,
            self.window_size,
            self.series_length,
            self.dft_length,
            self.norm,
            self.inverse_sqrt_win_size,
        )

        if y is not None:
            y = np.repeat(y, dft.shape[0] / len(y))

        if self.variance and y is not None:
            # determine variance
            dft_variance = np.var(dft, axis=0)

            # select word-length-many indices with the largest variance
            self.support = np.argsort(-dft_variance)[: self.word_length_actual]

            # sort remaining indices
            self.support = np.sort(self.support)

            # select the Fourier coefficients with highest f-score
            dft = dft[:, self.support]
            self.dft_length = np.max(self.support) + 1
            self.dft_length = self.dft_length + self.dft_length % 2  # even

        if self.anova and y is not None:
            non_constant = np.where(
                ~np.isclose(dft.var(axis=0), np.zeros_like(dft.shape[1]))
            )[0]

            # select word-length many indices with best f-score
            if self.word_length_actual <= non_constant.size:
                f, _ = f_classif(dft[:, non_constant], y)
                self.support = non_constant[np.argsort(-f)][: self.word_length_actual]

            # sort remaining indices
            self.support = np.sort(self.support)

            # select the Fourier coefficients with highest f-score
            dft = dft[:, self.support]
            self.dft_length = np.max(self.support) + 1
            self.dft_length = self.dft_length + self.dft_length % 2  # even

        if self.binning_method == "information-gain":
            return self._igb(dft, y)
        elif self.binning_method == "kmeans" or self.binning_method == "quantile":
            return self._k_bins_discretizer(dft)
        else:
            return self._mcb(dft)

    def _k_bins_discretizer(self, dft):
        encoder = KBinsDiscretizer(
            n_bins=self.alphabet_size, strategy=self.binning_method
        )
        encoder.fit(dft)
        if encoder.bin_edges_.ndim == 1:
            breaks = encoder.bin_edges_.reshape((-1, 1))
        else:
            breaks = encoder.bin_edges_
        breakpoints = np.zeros((self.word_length_actual, self.alphabet_size))

        for letter in range(self.word_length_actual):
            for bp in range(1, len(breaks[letter]) - 1):
                breakpoints[letter, bp - 1] = breaks[letter, bp]

        breakpoints[:, self.alphabet_size - 1] = sys.float_info.max
        return breakpoints

    def _mcb(self, dft):
        breakpoints = np.zeros((self.word_length_actual, self.alphabet_size))

        dft = np.round(dft, 2)
        for letter in range(self.word_length_actual):
            column = np.sort(dft[:, letter])
            bin_index = 0

            # use equi-depth binning
            if self.binning_method == "equi-depth":
                target_bin_depth = len(dft) / self.alphabet_size

                for bp in range(self.alphabet_size - 1):
                    bin_index += target_bin_depth
                    breakpoints[letter, bp] = column[int(bin_index)]

            # use equi-width binning aka equi-frequency binning
            elif self.binning_method == "equi-width":
                target_bin_width = (column[-1] - column[0]) / self.alphabet_size

                for bp in range(self.alphabet_size - 1):
                    breakpoints[letter, bp] = (bp + 1) * target_bin_width + column[0]

        breakpoints[:, self.alphabet_size - 1] = sys.float_info.max
        return breakpoints

    def _igb(self, dft, y):
        breakpoints = np.zeros((self.word_length_actual, self.alphabet_size))
        clf = DecisionTreeClassifier(
            criterion="entropy",
            max_depth=np.uint32(np.log2(self.alphabet_size)),
            max_leaf_nodes=self.alphabet_size,
            random_state=1,
        )

        for i in range(self.word_length_actual):
            clf.fit(dft[:, i][:, None], y)
            threshold = clf.tree_.threshold[clf.tree_.children_left != -1]
            for bp in range(len(threshold)):
                breakpoints[i, bp] = threshold[bp]
            for bp in range(len(threshold), self.alphabet_size):
                breakpoints[i, bp] = np.inf

        return np.sort(breakpoints, axis=1)

    def _shorten_bags(self, word_len, y):
        if self.save_words is False:
            raise ValueError(
                "Words from transform must be saved using save_word to shorten bags."
            )

        if word_len > self.word_length_actual:
            word_len = self.word_length_actual

        new_len = min(word_len, self.dfts.shape[2])
        self.dfts = self.dfts[:, :, :new_len]
        self.breakpoints = self.breakpoints[:new_len, :]
        self.support = self.support[:new_len]
        self.word_length_actual = word_len
        self.dft_length = new_len

        words = generate_words(
            self.dfts,
            self.bigrams,
            self.skip_grams,
            self.window_size,
            self.breakpoints,
            self.letter_bits,
        )

        # retrain feature selection-strategy
        return self.transform_to_bag(words, y)

    @classmethod
    def get_test_params(cls, parameter_set="default"):
        """Return testing parameter settings for the estimator.

        Parameters
        ----------
        parameter_set : str, default="default"
            Name of the set of test parameters to return, for use in tests. If no
            special parameters are defined for a value, will return `"default"` set.


        Returns
        -------
        params : dict or list of dict, default = {}
            Parameters to create testing instances of the class
            Each dict are parameters to construct an "interesting" test instance, i.e.,
            `MyClass(**params)` or `MyClass(**params[i])` creates a valid test instance.
            `create_test_instance` uses the first (or only) dictionary in `params`
        """
        # small window size for testing
        params = {
            "window_size": 4,
            "return_sparse": False,
            "return_pandas_data_series": True,
        }
        return params

    def set_fitted(self):
        """Whether `fit` has been called."""
        self._is_fitted = True

    def __getstate__(self):
        """Return state as dictionary for pickling, required for typed Dict objects."""
        state = self.__dict__.copy()

        if type(state["relevant_features"]) == Dict:
            state["relevant_features"] = dict(state["relevant_features"])
        return state

    def __setstate__(self, state):
        """Set current state using input pickling, required for typed Dict objects."""
        self.__dict__.update(state)
        if type(self.relevant_features) == dict:
            typed_dict = Dict.empty(key_type=types.uint32, value_type=types.uint32)
            for key, value in self.relevant_features.items():
                typed_dict[key] = value
            self.relevant_features = typed_dict


@njit(fastmath=True, cache=True)
def _binning_dft(
    X, window_size, series_length, dft_length, norm, inverse_sqrt_win_size
):
    num_windows_per_inst = math.ceil(series_length / window_size)

    # Splits individual time series into windows and returns the DFT for each
    data = np.zeros((len(X), num_windows_per_inst, window_size))
    for i in range(len(X)):
        for j in range(num_windows_per_inst - 1):
            data[i, j] = X[i, window_size * j : window_size * (j + 1)]

        start = series_length - window_size
        data[i, -1] = X[i, start:series_length]

    dft = np.zeros((len(X), num_windows_per_inst, dft_length))
    for i in range(len(X)):
        return_val = _fast_fourier_transform(
            data[i], norm, dft_length, inverse_sqrt_win_size
        )
        dft[i] = return_val

    return dft.reshape(dft.shape[0] * dft.shape[1], dft_length)


@njit(fastmath=True, cache=True)
def _fast_fourier_transform(X, norm, dft_length, inverse_sqrt_win_size):
    """Perform a discrete fourier transform using the fast fourier transform.

    if self.norm is True, then the first term of the DFT is ignored

    Input
    -------
    X : The training input samples.  array-like or sparse matrix of
    shape = [n_samps, num_atts]

    Returns
    -------
    1D array of fourier term, real_0,imag_0, real_1, imag_1 etc, length
    num_atts or
    num_atts-2 if if self.norm is True
    """
    # first two are real and imaginary parts
    start = 2 if norm else 0
    length = start + dft_length
    dft = np.zeros((len(X), length))  # , dtype=np.float64

    stds = np.zeros(len(X))
    for i in range(len(stds)):
        stds[i] = np.std(X[i])
    # stds = np.std(X, axis=1)  # not available in numba
    stds = np.where(stds < 1e-8, 1e-8, stds)

    with objmode(X_ffts="complex128[:,:]"):
        X_ffts = np.fft.rfft(X, axis=1)  # complex128
    reals = np.real(X_ffts)  # float64[]
    imags = np.imag(X_ffts)  # float64[]
    dft[:, 0::2] = reals[:, 0 : length // 2]
    dft[:, 1::2] = imags[:, 0 : length // 2]
    dft /= stds.reshape(-1, 1)
    dft *= inverse_sqrt_win_size

    return dft[:, start:]


@njit(fastmath=True, cache=True)  # njit and  is not working here?
def _transform_case(
    X,
    window_size,
    dft_length,
    norm,
    support,
    anova,
    variance,
    breakpoints,
    letter_bits,
    bigrams,
    skip_grams,
    inverse_sqrt_win_size,
):
    dfts = _mft(
        X,
        window_size,
        dft_length,
        norm,
        support,
        anova,
        variance,
        inverse_sqrt_win_size,
    )

    return (
        generate_words(
            dfts, bigrams, skip_grams, window_size, breakpoints, letter_bits
        ),
        dfts,
    )


@njit(fastmath=True, cache=True)
def _calc_incremental_mean_std(series, end, window_size):
    stds = np.zeros(end)
    window = series[0:window_size]
    series_sum = np.sum(window)
    square_sum = np.sum(np.multiply(window, window))

    r_window_length = 1.0 / window_size
    mean = series_sum * r_window_length
    buf = math.sqrt(square_sum * r_window_length - mean * mean)
    stds[0] = buf if buf > 1e-8 else 1e-8

    for w in range(1, end):
        series_sum += series[w + window_size - 1] - series[w - 1]
        mean = series_sum * r_window_length
        square_sum += (
            series[w + window_size - 1] * series[w + window_size - 1]
            - series[w - 1] * series[w - 1]
        )
        buf = math.sqrt(square_sum * r_window_length - mean * mean)
        stds[w] = buf if buf > 1e-8 else 1e-8

    return stds


@njit(fastmath=True, cache=True)
def _get_phis(window_size, length):
    phis = np.zeros(length)
    i = np.arange(length // 2)
    const = 2 * np.pi / window_size
    phis[0::2] = np.cos((-i) * const)
    phis[1::2] = -np.sin((-i) * const)
    return phis


@njit(fastmath=True, cache=True)
def generate_words(dfts, bigrams, skip_grams, window_size, breakpoints, letter_bits):
    needed_size = dfts.shape[1]
    if bigrams:
        # allocate memory for bigrams
        needed_size += max(0, dfts.shape[1] - window_size)
    if skip_grams:
        # allocate memory for 2- and 3-skip-grams
        needed_size += max(0, 2 * dfts.shape[1] - 5 * window_size)

    words = np.zeros((dfts.shape[0], needed_size), dtype=np.uint32)

    letter_bits = np.uint32(letter_bits)
    word_bits = dfts.shape[2] * letter_bits

    # special case: binary breakpoints
    if breakpoints.shape[1] == 2:
        vector = np.zeros((breakpoints.shape[0]), dtype=np.float32)
        for i in range(breakpoints.shape[0]):
            vector[i] = breakpoints.shape[1] ** i

        for a in prange(dfts.shape[0]):
            match = (dfts[a] <= breakpoints[:, 0]).astype(np.float32)
            words[a, : dfts.shape[1]] = np.dot(match, vector).astype(np.uint32)

    # general case: alphabet-size many breakpoints
    else:
        for a in prange(dfts.shape[0]):
            for i in range(dfts.shape[2]):
                words[a, : dfts.shape[1]] = (
                    words[a, : dfts.shape[1]] << letter_bits
                ) | np.digitize(dfts[a, :, i], breakpoints[i], right=True)

    # add bigrams
    if bigrams:
        for a in range(0, dfts.shape[1] - window_size):
            first_word = words[:, a]
            second_word = words[:, a + window_size]
            words[:, dfts.shape[1] + a] = (first_word << word_bits) | second_word

    # add 2,3-skip-grams
    if skip_grams:
        for s in range(2, 4):
            for a in range(0, dfts.shape[1] - s * window_size):
                first_word = words[:, a]
                second_word = words[:, a + s * window_size]
                words[:, dfts.shape[1] + a] = (first_word << word_bits) | second_word

    return words


@njit(fastmath=True, cache=True)
def _mft(
    X, window_size, dft_length, norm, support, anova, variance, inverse_sqrt_win_size
):
    start_offset = 2 if norm else 0
    length = dft_length + start_offset + dft_length % 2
    end = max(1, len(X[0]) - window_size + 1)

    #  compute mask for only those indices needed and not all indices
    if anova or variance:
        support = support + start_offset
        indices = np.full(length, False)
        mask = np.full(length, False)
        for s in support:
            indices[s] = True
            mask[s] = True
            if (s % 2) == 0:  # even
                indices[s + 1] = True
            else:  # uneven
                indices[s - 1] = True
        mask = mask[indices]
    else:
        indices = np.full(length, True)

    phis = _get_phis(window_size, length)
    transformed = np.zeros((X.shape[0], end, length))

    # 1. First run using DFT
    with objmode(X_ffts="complex128[:,:]"):
        X_ffts = np.fft.rfft(X[:, :window_size], axis=1)  # complex128
    reals = np.real(X_ffts)  # float64[]
    imags = np.imag(X_ffts)  # float64[]
    transformed[:, 0, 0::2] = reals[:, 0 : length // 2]
    transformed[:, 0, 1::2] = imags[:, 0 : length // 2]

    # 2. Other runs using MFT
    # X2 = X.reshape(X.shape[0], X.shape[1], 1)
    # Bugfix to allow for slices on original X like in TEASER
    X2 = X.copy().reshape(X.shape[0], X.shape[1], 1)

    # compute only those indices needed and not all
    phis2 = phis[indices]
    transformed2 = transformed[:, :, indices]
    for i in range(1, end):
        reals = transformed2[:, i - 1, 0::2] + X2[:, i + window_size - 1] - X2[:, i - 1]
        imags = transformed2[:, i - 1, 1::2]
        transformed2[:, i, 0::2] = (
            reals * phis2[:length:2] - imags * phis2[1 : (length + 1) : 2]
        )
        transformed2[:, i, 1::2] = (
            reals * phis2[1 : (length + 1) : 2] + phis2[:length:2] * imags
        )

    transformed2 = transformed2 * inverse_sqrt_win_size

    # compute STDs
    stds = np.zeros((X.shape[0], end))
    for a in range(X.shape[0]):
        stds[a] = _calc_incremental_mean_std(X[a], end, window_size)

    # divide all by stds and use only the best indices
    if anova or variance:
        return transformed2[:, :, mask] / stds.reshape(stds.shape[0], stds.shape[1], 1)
    else:
        return (transformed2 / stds.reshape(stds.shape[0], stds.shape[1], 1))[
            :, :, start_offset:
        ]


@njit(cache=True, fastmath=True)
def create_feature_names(sfa_words):
    feature_names = set()
    for t_words in sfa_words:
        for t_word in t_words:
            feature_names.add(t_word)
    return feature_names


@njit(cache=True, fastmath=True)
def create_bag_none(breakpoints, n_instances, sfa_words, word_length):
    feature_count = np.uint32(breakpoints.shape[1] ** word_length)
    all_win_words = np.zeros((n_instances, feature_count), dtype=np.uint32)

    for j in range(len(sfa_words)):
        all_win_words[j, :] = np.bincount(sfa_words[j], minlength=feature_count)

    return all_win_words


@njit(cache=True, fastmath=True)
def create_bag_feature_selection(
    n_instances, relevant_features_idx, feature_names, sfa_words
):
    relevant_features = Dict.empty(key_type=types.uint32, value_type=types.uint32)
    for k, v in zip(
        feature_names[relevant_features_idx],
        np.arange(len(relevant_features_idx), dtype=np.uint32),
    ):
        relevant_features[k] = v

    all_win_words = np.zeros((n_instances, len(relevant_features_idx)), dtype=np.uint32)
    for j in range(len(sfa_words)):
        for key in sfa_words[j]:
            if key in relevant_features:
                all_win_words[j, relevant_features[key]] += 1
    return all_win_words, relevant_features


@njit(cache=True, fastmath=True)
def create_bag_transform(
    feature_count, feature_selection, relevant_features, sfa_words, bigrams
):
    # merging arrays
    all_win_words = np.zeros((len(sfa_words), feature_count), np.uint32)
    for j in range(len(sfa_words)):
        if feature_selection == "none" and not bigrams:
            all_win_words[j, :] = np.bincount(sfa_words[j], minlength=feature_count)
        else:
            for _, key in enumerate(sfa_words[j]):
                if key in relevant_features:
                    o = relevant_features[key]
                    all_win_words[j, o] += 1

    return all_win_words, feature_count
