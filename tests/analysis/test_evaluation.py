import xarray as xr
# import numpy as np
# from pandas._libs.tslibs.timestamps import Timestamp

from src.analysis.evaluation import (
    # spatial_rmse,
    # spatial_r2,
    # rmse,
    # annual_scores,
    # annual_scores_to_dataframe,
    # read_pred_data,
    # read_true_data,
    # monthly_score,
    # plot_predictions,
    read_train_data,
    read_test_data
)

from ..utils import (
    _create_dummy_precip_data,
    _create_features_dir,
)


class TestEvaluation:
    def test_read_train_data(self, tmp_path):
        _create_features_dir(tmp_path, train=True)

        X, y = read_train_data(tmp_path)
        assert isinstance(X, xr.Dataset)
        assert isinstance(y, xr.DataArray)

    def test_read_test_data(self, tmp_path):
        _create_features_dir(tmp_path, train=True)
        _create_features_dir(tmp_path, train=False)

        X, y = read_test_data(tmp_path)
        assert isinstance(X, xr.Dataset)
        assert isinstance(y, xr.DataArray)

    def test_spatial_r2(self, tmp_path):
        _create_dummy_precip_data(tmp_path)
