import gzip
import json
import os
import shutil
from os.path import join
from warnings import warn

try:
    # Python 3+
    from urllib.request import urlopen
except ImportError:
    # Python 2
    from urllib2 import urlopen


import numpy as np
import scipy.sparse

from sklearn.externals import _arff
from .base import get_data_home
from ..externals.six import string_types, PY2
from ..externals.six.moves.urllib.error import HTTPError
from ..utils import Bunch

__all__ = ['fetch_openml']

_OPENML_PREFIX = "https://openml.org/"
_SEARCH_NAME = "api/v1/json/data/list/data_name/{}/limit/2"
_DATA_INFO = "api/v1/json/data/{}"
_DATA_FEATURES = "api/v1/json/data/features/{}"
_DATA_FILE = "data/v1/download/{}"


def _open_openml_url(openml_path, data_home):
    """
    Returns a resource from OpenML.org. Caches it to data_home if required.

    Parameters
    ----------
    openml_path : str
        OpenML URL that will be accessed. This will be prefixes with
        _OPENML_PREFIX

    data_home : str
        Directory to which the files will be cached. If None, no caching will
        be applied.

    Returns
    -------
    result : stream
        A stream to the OpenML resource
    """
    if data_home is None:
        return urlopen(_OPENML_PREFIX + openml_path)
    local_path = os.path.join(data_home, 'openml.org', openml_path + ".gz")
    if not os.path.exists(local_path):
        try:
            os.makedirs(os.path.dirname(local_path))
        except OSError:
            # potentially, the directory has been created already
            pass

        try:
            with gzip.GzipFile(local_path, 'wb') as fdst:
                fsrc = urlopen(_OPENML_PREFIX + openml_path)
                shutil.copyfileobj(fsrc, fdst)
                fsrc.close()
        except Exception:
            os.unlink(local_path)
            raise
    # XXX: unnecessary decompression on first access
    return gzip.GzipFile(local_path, 'rb')


def _get_json_content_from_openml_api(url, error_message, raise_if_error,
                                      data_home):
    """
    Loads json data from the openml api

    Parameters
    ----------
    url : str
        The URL to load from. Should be an official OpenML endpoint

    error_message : str or None
        The error message to raise if an acceptable OpenML error is thrown
        (acceptable error is, e.g., data id not found. Other errors, like 404's
        will throw the native error message)

    raise_if_error : bool
        Whether to raise an error if OpenML returns an acceptable error (e.g.,
        date not found). If this argument is set to False, a None is returned
        in case of acceptable errors. Note that all other errors (e.g., 404)
        will still be raised as normal.

    data_home : str or None
        Location to cache the response. None if no cache is required.

    Returns
    -------
    json_data : json or None
        the json result from the OpenML server if the call was successful;
        None otherwise iff raise_if_error was set to False and the error was
        ``acceptable``
    """
    data_found = True
    try:
        response = _open_openml_url(url, data_home)
    except HTTPError as error:
        # 412 is an OpenML specific error code, indicating a generic error
        # (e.g., data not found)
        if error.code == 412:
            data_found = False
        else:
            raise error
    if not data_found:
        # not in except for nicer traceback
        if raise_if_error:
            raise ValueError(error_message)
        else:
            return None
    json_data = json.loads(response.read().decode("utf-8"))
    response.close()
    return json_data


def _split_sparse_columns(arff_data, include_columns):
    """
    obtains several columns from sparse arff representation. Additionally, the
    column indices are re-labelled, given the columns that are not included.
    (e.g., when including [1, 2, 3], the columns will be relabelled to
    [0, 1, 2])

    Parameters
    ----------
    arff_data : tuple
        A tuple of three lists of equal size; first list indicating the value,
        second the x coordinate and the third the y coordinate.

    include_columns : list
        A list of columns to include.

    Returns
    -------
    arff_data_new : tuple
        Subset of arff data with only the include columns indicated by the
        include_columns argument.
    """
    arff_data_new = (list(), list(), list())
    reindexed_columns = {column_idx: array_idx for array_idx, column_idx
                         in enumerate(include_columns)}
    for val, row_idx, col_idx in zip(arff_data[0], arff_data[1], arff_data[2]):
        if col_idx in include_columns:
            arff_data_new[0].append(val)
            arff_data_new[1].append(row_idx)
            arff_data_new[2].append(reindexed_columns[col_idx])
    return arff_data_new


def _sparse_data_to_array(arff_data, include_columns):
    # turns the sparse data back into an array (can't use toarray() function,
    # as this does only work on numeric data)
    num_obs = max(arff_data[1]) + 1
    y_shape = (num_obs, len(include_columns))
    reindexed_columns = {column_idx: array_idx for array_idx, column_idx
                         in enumerate(include_columns)}
    # TODO: improve for efficiency
    y = np.empty(y_shape, dtype=np.float64)
    for val, row_idx, col_idx in zip(arff_data[0], arff_data[1], arff_data[2]):
        if col_idx in include_columns:
            y[row_idx, reindexed_columns[col_idx]] = val
    return y


def _convert_arff_data(arff_data, col_slice_x, col_slice_y):
    """
    converts the arff object into the appropriate matrix type (np.array or
    scipy.sparse.csr_matrix) based on the 'data part' (i.e., in the
    liac-arff dict, the object from the 'data' key)

    Parameters
    ----------
    arff_data : list or dict
        as obtained from liac-arff object

    col_slice_x : list
        The column indices that are sliced from the original array to return
        as X data

    col_slice_y : list
        The column indices that are sliced from the original array to return
        as y data

    Returns
    -------
    X : np.array or scipy.sparse.csr_matrix
    y : np.array
    """
    if isinstance(arff_data, list):
        data = np.array(arff_data, dtype=np.float64)
        X = np.array(data[:, col_slice_x], dtype=np.float64)
        y = np.array(data[:, col_slice_y], dtype=np.float64)
        return X, y
    elif isinstance(arff_data, tuple):
        arff_data_X = _split_sparse_columns(arff_data, col_slice_x)
        num_obs = max(arff_data[1]) + 1
        X_shape = (num_obs, len(col_slice_x))
        X = scipy.sparse.coo_matrix(
            (arff_data_X[0], (arff_data_X[1], arff_data_X[2])),
            shape=X_shape, dtype=np.float64)
        X = X.tocsr()
        y = _sparse_data_to_array(arff_data, col_slice_y)
        return X, y
    else:
        # This should never happen
        raise ValueError('Unexpected Data Type obtained from arff.')


def _get_data_info_by_name(name, version, data_home):
    """
    Utilizes the openml dataset listing api to find a dataset by
    name/version
    OpenML api function:
    https://www.openml.org/api_docs#!/data/get_data_list_data_name_data_name

    Parameters
    ----------
    name : str
        name of the dataset

    version : int or str
        If version is an integer, the exact name/version will be obtained from
        OpenML. If version is a string (value: "active") it will take the first
        version from OpenML that is annotated as active. Any other string
        values except "active" are treated as integer.

    data_home : str or None
        Location to cache the response. None if no cache is required.

    Returns
    -------
    first_dataset : json
        json representation of the first dataset object that adhired to the
        search criteria

    """
    if version == "active":
        # situation in which we return the oldest active version
        url = _SEARCH_NAME.format(name) + "/status/active/"
        error_msg = "No active dataset {} found.".format(name)
        json_data = _get_json_content_from_openml_api(url, error_msg, True,
                                                      data_home)
        res = json_data['data']['dataset']
        if len(res) > 1:
            warn("Multiple active versions of the dataset matching the name"
                 " {name} exist. Versions may be fundamentally different, "
                 "returning version"
                 " {version}.".format(name=name, version=res[0]['version']))
        return res[0]

    # an integer version has been provided
    url = (_SEARCH_NAME + "/data_version/{}").format(name, version)
    json_data = _get_json_content_from_openml_api(url, None, False,
                                                  data_home)
    if json_data is None:
        # we can do this in 1 function call if OpenML does not require the
        # specification of the dataset status (i.e., return datasets with a
        # given name / version regardless of active, deactivated, etc. )
        # TODO: feature request OpenML.
        url += "/status/deactivated"
        error_msg = "Dataset {} with version {} not found.".format(name,
                                                                   version)
        json_data = _get_json_content_from_openml_api(url, error_msg, True,
                                                      data_home)

    return json_data['data']['dataset'][0]


def _get_data_description_by_id(data_id, data_home):
    # OpenML API function: https://www.openml.org/api_docs#!/data/get_data_id
    url = _DATA_INFO.format(data_id)
    error_message = "Dataset with data_id {} not found.".format(data_id)
    json_data = _get_json_content_from_openml_api(url, error_message, True,
                                                  data_home)
    return json_data['data_set_description']


def _get_data_features(data_id, data_home):
    # OpenML function:
    # https://www.openml.org/api_docs#!/data/get_data_features_id
    url = _DATA_FEATURES.format(data_id)
    error_message = "Dataset with data_id {} not found.".format(data_id)
    json_data = _get_json_content_from_openml_api(url, error_message, True,
                                                  data_home)
    return json_data['data_features']['feature']


def _download_data_arff(file_id, sparse, data_home, encode_nominal=True):
    # Accesses an ARFF file on the OpenML server. Documentation:
    # https://www.openml.org/api_data_docs#!/data/get_download_id
    # encode_nominal argument is to ensure unit testing, do not alter in
    # production!
    url = _DATA_FILE.format(file_id)
    response = _open_openml_url(url, data_home)
    if sparse is True:
        return_type = _arff.COO
    else:
        return_type = _arff.DENSE

    if PY2:
        arff_file = _arff.load(response, encode_nominal=encode_nominal,
                               return_type=return_type, )
    else:
        arff_file = _arff.loads(response.read().decode('utf-8'),
                                encode_nominal=encode_nominal,
                                return_type=return_type)
    response.close()
    return arff_file


def _verify_target_data_type(features_dict, target_columns):
    # verifies the data type of the y array in case there are multiple targets
    # (throws an error if these targets do not comply with sklearn support)
    if not isinstance(target_columns, list):
        raise ValueError('target_column should be list, '
                         'got: %s' % type(target_columns))
    found_types = set()
    for target_column in target_columns:
        if target_column not in features_dict:
            raise KeyError('Could not find target_column={}')
        if features_dict[target_column]['data_type'] == "numeric":
            found_types.add(np.float64)
        else:
            found_types.add(object)

        # note: we compare to a string, not boolean
        if features_dict[target_column]['is_ignore'] == 'true':
            warn('target_column={} has flag is_ignore.'.format(
                target_column))
        if features_dict[target_column]['is_row_identifier'] == 'true':
            warn('target_column={} has flag is_row_identifier.'.format(
                target_column))
    if len(found_types) > 1:
        raise ValueError('Can only handle homogeneous multi-target datasets, '
                         'i.e., all targets are either numeric or '
                         'categorical.')


def fetch_openml(name=None, version='active', data_id=None, data_home=None,
                 target_column='default-target', cache=True):
    """Fetch dataset from openml by name or dataset id.

    Datasets are uniquely identified by either an integer ID or by a
    combination of name and version (i.e. there might be multiple
    versions of the 'iris' dataset). Please give either name or data_id
    (not both). In case a name is given, a version can also be
    provided.

    .. note:: EXPERIMENTAL

        The API is experimental in version 0.20 (particularly the return value
        structure), and might have small backward-incompatible changes in
        future releases.

    Parameters
    ----------
    name : str or None
        String identifier of the dataset. Note that OpenML can have multiple
        datasets with the same name.

    version : integer or 'active', default='active'
        Version of the dataset. Can only be provided if also ``name`` is given.
        If 'active' the oldest version that's still active is used. Since
        there may be more than one active version of a dataset, and those
        versions may fundamentally be different from one another, setting an
        exact version is highly recommended.

    data_id : int or None
        OpenML ID of the dataset. The most specific way of retrieving a
        dataset. If data_id is not given, name (and potential version) are
        used to obtain a dataset.

    data_home : string or None, default None
        Specify another download and cache folder for the data sets. By default
        all scikit-learn data is stored in '~/scikit_learn_data' subfolders.

    target_column : string, list or None, default 'default-target'
        Specify the column name in the data to use as target. If
        'default-target', the standard target column a stored on the server
        is used. If ``None``, all columns are returned as data and the
        target is ``None``. If list (of strings), all columns with these names
        are returned as multi-target (Note: not all scikit-learn classifiers
        can handle all types of multi-output combinations)

    cache : boolean, default=True
        Whether to cache downloaded datasets using joblib.

    Returns
    -------

    data : Bunch
        Dictionary-like object, with attributes:

        data : np.array or scipy.sparse.csr_matrix of floats
            The feature matrix. Categorical features are encoded as ordinals.
        target : np.array
            The regression target or classification labels, if applicable.
            Dtype is float if numeric, and object if categorical.
        DESCR : str
            The full description of the dataset
        feature_names : list
            The names of the dataset columns
        categories : dict
            Maps each categorical feature name to a list of values, such
            that the value encoded as i is ith in the list.
        details : dict
            More metadata from OpenML

        .. note:: EXPERIMENTAL

            This interface is **experimental** as at version 0.20 and
            subsequent releases may change attributes without notice
            (although there should only be minor changes to ``data``
            and ``target``).

        Missing values in the 'data' are represented as NaN's. Missing values
        in 'target' are represented as NaN's (numerical target) or None
        (categorical target)
    """
    data_home = get_data_home(data_home=data_home)
    data_home = join(data_home, 'openml')
    if cache is False:
        # no caching will be applied
        data_home = None

    # check valid function arguments. data_id XOR (name, version) should be
    # provided
    if name is not None:
        # OpenML is case-insensitive, but the caching mechanism is not
        # convert all data names (str) to lower case
        name = name.lower()
        if data_id is not None:
            raise ValueError(
                "Dataset data_id={} and name={} passed, but you can only "
                "specify a numeric data_id or a name, not "
                "both.".format(data_id, name))
        data_info = _get_data_info_by_name(name, version, data_home)
        data_id = data_info['did']
    elif data_id is not None:
        # from the previous if statement, it is given that name is None
        if version is not "active":
            raise ValueError(
                "Dataset data_id={} and version={} passed, but you can only "
                "specify a numeric data_id or a version, not "
                "both.".format(data_id, name))
    else:
        raise ValueError(
            "Neither name nor data_id are provided. Please provide name or "
            "data_id.")

    data_description = _get_data_description_by_id(data_id, data_home)
    if data_description['status'] != "active":
        warn("Version {} of dataset {} is inactive, meaning that issues have "
             "been found in the dataset. Try using a newer version from "
             "this URL: {}".format(
                data_description['version'],
                data_description['name'],
                data_description['url']))

    # download data features, meta-info about column types
    features_list = _get_data_features(data_id, data_home)

    for feature in features_list:
        if 'true' in (feature['is_ignore'], feature['is_row_identifier']):
            continue
        if feature['data_type'] == 'string':
            raise ValueError('STRING attributes are not yet supported')

    if target_column == "default-target":
        # determines the default target based on the data feature results
        # (which is currently more reliable than the data description;
        # see issue: https://github.com/openml/OpenML/issues/768)
        target_column = [feature['name'] for feature in features_list
                         if feature['is_target'] == 'true']
    elif isinstance(target_column, string_types):
        # for code-simplicity, make target_column by default a list
        target_column = [target_column]
    elif target_column is None:
        target_column = []
    elif not isinstance(target_column, list):
        raise TypeError("Did not recognize type of target_column"
                        "Should be six.string_type, list or None. Got: "
                        "{}".format(type(target_column)))
    data_columns = [feature['name'] for feature in features_list
                    if (feature['name'] not in target_column and
                        feature['is_ignore'] != 'true' and
                        feature['is_row_identifier'] != 'true')]

    # prepare which columns and data types should be returned for the X and y
    features_dict = {feature['name']: feature for feature in features_list}

    # XXX: col_slice_y should be all nominal or all numeric
    _verify_target_data_type(features_dict, target_column)

    col_slice_y = [int(features_dict[col_name]['index'])
                   for col_name in target_column]

    col_slice_x = [int(features_dict[col_name]['index'])
                   for col_name in data_columns]
    for col_idx in col_slice_y:
        feat = features_list[col_idx]
        nr_missing = int(feat['number_of_missing_values'])
        if nr_missing > 0:
            raise ValueError('Target column {} has {} missing values. '
                             'Missing values are not supported for target '
                             'columns. '.format(feat['name'], nr_missing))

    # determine arff encoding to return
    return_sparse = False
    if data_description['format'].lower() == 'sparse_arff':
        return_sparse = True

    # obtain the data
    arff = _download_data_arff(data_description['file_id'], return_sparse,
                               data_home)
    arff_data = arff['data']
    nominal_attributes = {k: v for k, v in arff['attributes']
                          if isinstance(v, list)}
    for feature in features_list:
        if 'true' in (feature['is_row_identifier'],
                      feature['is_ignore']) and (feature['name'] not in
                                                 target_column):
            del nominal_attributes[feature['name']]
    X, y = _convert_arff_data(arff_data, col_slice_x, col_slice_y)

    is_classification = {col_name in nominal_attributes
                         for col_name in target_column}
    if not is_classification:
        # No target
        pass
    elif all(is_classification):
        y = np.hstack([np.take(np.asarray(nominal_attributes.pop(col_name),
                                          dtype='O'),
                               y[:, i:i+1].astype(int))
                       for i, col_name in enumerate(target_column)])
    elif any(is_classification):
        raise ValueError('Mix of nominal and non-nominal targets is not '
                         'currently supported')

    description = u"{}\n\nDownloaded from openml.org.".format(
        data_description.pop('description'))

    # reshape y back to 1-D array, if there is only 1 target column; back
    # to None if there are not target columns
    if y.shape[1] == 1:
        y = y.reshape((-1,))
    elif y.shape[1] == 0:
        y = None

    bunch = Bunch(
        data=X, target=y, feature_names=data_columns,
        DESCR=description, details=data_description,
        categories=nominal_attributes,
        url="https://www.openml.org/d/{}".format(data_id))

    return bunch
