from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import numpy as np
import pandas as pd
from pandas import Timestamp
from random import shuffle
from pathlib import Path
import pickle
import torch
import xarray as xr

from typing import cast, Dict, Optional, Union, List, Tuple

from .data import DataLoader


class OneDimensionalLoader(DataLoader):
    #  NOTE: do we want to keep saving these to memory?
    #       or just use a DataLoader to read from one ds
    def __init__(
        self,
        data_path: Path = Path("data"),
        batch_file_size: int = 1,
        mode: str = "train",
        shuffle_data: bool = True,
        clear_nans: bool = True,
        normalize: bool = True,
        predict_delta: bool = False,
        experiment: str = "one_month_forecast",
        mask: Optional[List[bool]] = None,
        seq_length: Optional[List[int]] = None,
        to_tensor: bool = False,
        ignore_vars: Optional[List[str]] = None,
        monthly_aggs: bool = True,
        static: Optional[str] = "features",
        device: str = "cpu",
        spatial_mask: Optional[xr.DataArray] = None,
        normalize_y: bool = False,
    ) -> None:

        self.batch_file_size = batch_file_size
        self.mode = mode
        self.shuffle = shuffle_data
        self.experiment = experiment
        self.data_files = self._load_datasets(
            data_path=data_path,
            mode=mode,
            shuffle_data=shuffle_data,
            experiment=experiment,
            mask=mask,
            seq_length=seq_length,
        )
        self.predict_delta = predict_delta

        self.normalizing_dict = None
        self.normalize_y = normalize_y
        if normalize_y:
            # need to load the normalizing dict, and it doesn't really make sense
            # to normalize the output but not the input
            if not normalize:
                print("Forcing normalize to be True since normalize_y is True")
            normalize = True
        if normalize:
            with (data_path / f"features/{experiment}/normalizing_dict.pkl").open(
                "rb"
            ) as f:
                self.normalizing_dict = pickle.load(f)

        self.monthly_aggs = monthly_aggs
        self.to_tensor = to_tensor
        self.ignore_vars = ignore_vars

        self.static: Optional[xr.Dataset] = None
        self.max_loc_int: Optional[int] = None
        self.static_normalizing_dict = None

        if static is not None:
            if static == "features":
                self.static = xr.open_dataset(data_path / "features/static/data.nc")
                if normalize:
                    static_normalizer_path = (
                        data_path / "features/static/normalizing_dict.pkl"
                    )
                    with static_normalizer_path.open("rb") as f:
                        self.static_normalizing_dict = pickle.load(f)
            if static == "embeddings":
                # in case no static dataset was generated, we use the first
                # historical dataset
                self.static, self.max_loc_int = self._loc_to_int(
                    xr.open_dataset(self.data_files[0] / "x.nc")
                )

        self.device = torch.device(device)
        self.spatial_mask = spatial_mask

        if spatial_mask is not None:
            assert (
                clear_nans is True
            ), f"The spatial mask uses NaNs to get rid of values - this requires clear_nans to be true"
        self.clear_nans = clear_nans

    def __iter__(self):
        if self.mode == "train":
            return _TrainIter(self)
        else:
            return _TestIter(self)

    def __len__(self) -> int:
        return len(self.data_files) // self.batch_file_size

    @staticmethod
    def _loc_to_int(base_ds: xr.Dataset) -> Tuple[xr.Dataset, int]:
        """
        returns a dataset with the lat and lon coordinates preserved, and
        a unique increasing integer for each lat-lon combination
        """
        assert {"lat", "lon"} <= set(
            base_ds.dims
        ), "Dimensions named lat and lon must be in the reference grid"
        base_ds = base_ds[["lat", "lon"]]

        # next, we fill the values with unique integers
        unique_values = np.arange(0, len(base_ds.lat) * len(base_ds.lon)).reshape(
            (len(base_ds.lat), len(base_ds.lon))
        )
        base_ds["encoding"] = (("lat", "lon"), unique_values)

        return base_ds, int(unique_values.max())

    @staticmethod
    def _load_datasets(
        data_path: Path,
        mode: str,
        shuffle_data: bool,
        experiment: str,
        mask: Optional[List[bool]] = None,
        seq_length: Optional[List[int]] = None,
    ) -> List[Path]:

        data_folder = data_path / f"features/{experiment}/{mode}"
        output_paths: List[Path] = []

        for subtrain in data_folder.iterdir():
            if (subtrain / "x.nc").exists() and (subtrain / "y.nc").exists():
                if seq_length is None:
                    output_paths.append(subtrain)
                else:
                    month = int(str(subtrain.parts[-1]).split("_")[-2])
                    if month in seq_length:
                        output_paths.append(subtrain)

        if mask is not None:
            output_paths.sort()
            assert len(output_paths) == len(
                mask
            ), f"Output path and mask must be the same length!"
            output_paths = [o_p for o_p, include in zip(output_paths, mask) if include]

        if shuffle_data:
            shuffle(output_paths)

        return output_paths


# --------------
# ITERATORS
# --------------
class _BaseIter:
    """Base iterator
    """

    def __init__(self, loader: DataLoader) -> None:
        self.data_files = loader.data_files
        self.batch_file_size = loader.batch_file_size
        self.shuffle = loader.shuffle
        self.clear_nans = loader.clear_nans
        self.monthly_aggs = loader.monthly_aggs
        self.to_tensor = loader.to_tensor
        self.experiment = loader.experiment
        self.ignore_vars = loader.ignore_vars
        self.device = loader.device
        self.predict_delta = loader.predict_delta
        self.spatial_mask = loader.spatial_mask
        self.normalize_y = loader.normalize_y

        self.static = loader.static
        self.static_normalizing_dict = loader.static_normalizing_dict

        self.static_array: Optional[np.ndarray] = None

        if self.shuffle:
            # makes sure they are shuffled every epoch
            shuffle(self.data_files)

        self.normalizing_dict = loader.normalizing_dict
        self.normalizing_array: Optional[Dict[str, np.ndarray]] = None
        self.normalizing_array_ym: Optional[Dict[str, np.ndarray]] = None
        self.normalizing_array_static: Optional[Dict[str, np.ndarray]] = None

        self.idx = 0
        self.max_idx = len(loader.data_files)

    def __iter__(self):
        return self

    def _get_prev_y_var(
        self, folder: Path, y_var: str, num_examples: int
    ) -> np.ndarray:

        # first, we will try loading the previous year
        year, month = folder.name.split("_")
        previous_year = int(year) - 1

        new_path = folder.parent / f"{previous_year}_{month}"
        if new_path.exists():
            y = xr.open_dataset(new_path / "y.nc")
            coord = [c for c in y.coords if c != "time"][0]
            y = y.transpose("time", coord)
            y_np = y[y_var].values
            y_np = y_np.reshape(y_np.shape[0], y_np.shape[1])
            y_np = np.moveaxis(y_np, -1, 0)

            if self.normalizing_dict is not None:
                y_np = (
                    y_np - self.normalizing_dict[y_var]["mean"]
                ) / self.normalizing_dict[y_var]["std"]
            return y_np
        else:
            # the mean will be 0 (if normalizing is True), so this is actually not too bad
            # if normalizing is not true, we don't have a normalizing dict to find the mean with,
            # so there is not much we can do.
            return np.zeros((num_examples, 1))

    def calculate_static_normalizing_array(
        self, data_vars: List[str]
    ) -> Dict[str, np.ndarray]:

        self.static_normalizing_dict = cast(
            Dict[str, Dict[str, float]], self.static_normalizing_dict
        )

        mean, std = [], []

        normalizing_dict_keys = self.static_normalizing_dict.keys()
        for var in data_vars:
            for norm_var in normalizing_dict_keys:
                if var == norm_var:
                    mean.append(self.static_normalizing_dict[norm_var]["mean"])
                    std.append(self.static_normalizing_dict[norm_var]["std"])
                    break

        normalizing_array = cast(
            Dict[str, np.ndarray], {"mean": np.asarray(mean), "std": np.asarray(std)}
        )
        return normalizing_array

    def calculate_normalizing_array(
        self, data_vars: List[str]
    ) -> Dict[str, np.ndarray]:
        # If we've made it here, normalizing_dict is definitely not None
        self.normalizing_dict = cast(Dict[str, Dict[str, float]], self.normalizing_dict)

        mean, std = [], []
        normalizing_dict_keys = self.normalizing_dict.keys()
        for var in data_vars:
            for norm_var in normalizing_dict_keys:
                if var.endswith(norm_var):
                    mean.append(self.normalizing_dict[norm_var]["mean"])
                    std.append(self.normalizing_dict[norm_var]["std"])
                    break

        mean_np, std_np = np.asarray(mean), np.asarray(std)

        normalizing_array = cast(
            Dict[str, np.ndarray], {"mean": mean_np, "std": std_np}
        )
        return normalizing_array

    def _calculate_aggs(self, x: xr.Dataset) -> np.ndarray:
        yearly_mean = x.mean(dim=["time", "lat", "lon"])
        yearly_agg = yearly_mean.to_array().values

        if (self.normalizing_dict is not None) and (self.normalizing_array is None):
            self.normalizing_array_ym = self.calculate_normalizing_array(
                list(yearly_mean.data_vars)
            )

        if self.normalizing_array_ym is not None:
            yearly_agg = (
                yearly_agg - self.normalizing_array_ym["mean"]
            ) / self.normalizing_array_ym["std"]
        return yearly_agg

    def _calculate_historical(
        self, x: xr.Dataset, y: xr.Dataset
    ) -> Tuple[np.ndarray, np.ndarray]:
        x = self._add_extra_dims(x, False, self.monthly_aggs)

        x_np, y_np = x.to_array().values, y.to_array().values

        # first, x
        x_np = x_np.reshape(x_np.shape[0], x_np.shape[1], x_np.shape[2] * x_np.shape[3])
        x_np = np.moveaxis(np.moveaxis(x_np, 0, 1), -1, 0)

        # then, y
        y_np = y_np.reshape(y_np.shape[0], y_np.shape[1], y_np.shape[2] * y_np.shape[3])
        y_np = np.moveaxis(y_np, -1, 0).reshape(-1, 1)

        if (self.normalizing_dict is not None) and (self.normalizing_array is None):
            self.normalizing_array = self.calculate_normalizing_array(list(x.data_vars))
        if self.normalizing_array is not None:
            x_np = (x_np - self.normalizing_array["mean"]) / (
                self.normalizing_array["std"]
            )

        if self.normalize_y:
            # normalizing_dict will not be None
            y_var = list(y.data_vars)[0]
            if not self.predict_delta:
                y_np = (
                    (
                        y_np - self.normalizing_dict[y_var]["mean"]  # type: ignore
                    )
                    / self.normalizing_dict[y_var]["std"]  # type: ignore
                )
            else:
                # if we are doing predict_delta, then there is no need to shift by mean, since
                # the x we will be adding to has already been shifting. Shifting this value would
                # be "double shifting"
                y_np = y_np / self.normalizing_dict[y_var]["std"]  # type: ignore

        return x_np, y_np

    @staticmethod
    def _calculate_target_months(y: xr.Dataset, num_instances: int) -> np.ndarray:
        # then, the x month
        assert len(y.time) == 1, (
            "Expected y to only have 1 timestamp!" f"Got {len(y.time)}"
        )
        target_month = datetime.strptime(
            str(y.time.values[0])[:-3], "%Y-%m-%dT%H:%M:%S.%f"
        ).month
        x_months = np.array([target_month] * num_instances)

        return x_months

    def _calculate_latlons(self, x: xr.Dataset) -> Tuple[np.ndarray, np.ndarray]:
        # then, latlons
        lons, lats = np.meshgrid(x.lon.values, x.lat.values)
        flat_lats, flat_lons = lats.reshape(-1, 1), lons.reshape(-1, 1)
        latlons = np.concatenate((flat_lats, flat_lons), axis=-1)
        train_latlons = np.concatenate((flat_lats, flat_lons), axis=-1)

        if self.normalizing_array is not None:
            train_latlons / [90, 180]

        return latlons, train_latlons

    def _calculate_static(self, num_instances: int) -> np.ndarray:
        if self.static_array is None:
            static_np = self.static.to_array().values  # type: ignore
            static_np = static_np.reshape(
                static_np.shape[0], static_np.shape[1] * static_np.shape[2]
            )
            static_np = np.moveaxis(static_np, -1, 0)
            assert static_np.shape[0] == num_instances

            if self.static_normalizing_dict is not None:
                vars = list(self.static.data_vars)  # type: ignore
                self.static_normalizing_array = self.calculate_static_normalizing_array(
                    vars
                )

                static_np = (
                    static_np - self.static_normalizing_array["mean"]
                ) / self.static_normalizing_array["std"]
            self.static_array = static_np
        return self.static_array

    def apply_spatial_mask(
        self, x: xr.Dataset, y: xr.Dataset
    ) -> Tuple[xr.Dataset, xr.Dataset]:

        if self.spatial_mask is None:
            return x, y

        else:
            # anywhere where the mask is 1, make NaN
            return x.where(~self.spatial_mask), y.where(~self.spatial_mask)

    def _calculate_historical_target(self, x: xr.Dataset, y_var: str) -> np.ndarray:
        """Calculate the previous timestep for the target_variable
            (used in predict_delta experiment).
        """
        # get teh previous timestep
        # NOTE: hardcoding order=1 for predict_delta
        order = 1
        x_np = x[y_var].isel(time=-order).values
        x_np = x_np.reshape(x_np.shape[0] * x_np.shape[1], 1)
        # x_np = np.moveaxis(np.moveaxis(x_np, 0, 1), -1, 0)
        return x_np

    def _calculate_change(
        self, x: xr.Dataset, y: xr.Dataset, order: int = 1
    ) -> xr.Dataset:
        """Rather than predicting the raw VCI value, calculate the change
        in the VCI relative to the previous timestep (if order == 1).

        Arguments:
        ---------
        x: xr.Dataset
            the predictor timestep variables (including target variable)
        y: xr.Dataset
            the predicted timestep target variable
        order: int = 1
            the number of timesteps before the predicted timestep to calculate
            the derivative. 1 means that we are calculating the change
            from the previous timestep.
        """
        # get the last (#order) timestep from the X data
        y_var = [v for v in y.data_vars][0]
        prev_ts = x[y_var].isel(time=-order)

        # calculate the derivative
        return (y[y_var] - prev_ts).to_dataset(name=y_var)

    def ds_folder_to_np(
        self, folder: Path, clear_nans: bool = True, to_tensor: bool = False
    ) -> ModelArrays:

        x, y = xr.open_dataset(folder / "x.nc"), xr.open_dataset(folder / "y.nc")

        if self.predict_delta:
            # TODO: do this ONCE not at each read-in of the data
            y = self._calculate_change(x, y)
        assert len(list(y.data_vars)) == 1, (
            f"Expect only 1 target variable! " f"Got {len(list(y.data_vars))}"
        )
        if self.ignore_vars is not None:
            #  only include the vars in ignore_vars that are in x.data_vars
            self.ignore_vars = [
                v for v in self.ignore_vars if v in [var_ for var_ in x.data_vars]
            ]
            x = x.drop(self.ignore_vars)

        target_time = pd.to_datetime(y.time.values[0])
        if self.experiment == "nowcast":
            x_datetimes = [
                pd.to_datetime(time)
                for time in x.time.values
                if pd.to_datetime(time) != target_time
            ]
        else:
            x_datetimes = [pd.to_datetime(time) for time in x.time.values]

        x, y = self.apply_spatial_mask(x, y)

        yearly_agg = self._calculate_aggs(
            x
        )  # before to avoid aggs from surrounding pixels

        # calculate normalized values in these functions
        x_np, y_np = self._calculate_historical(x, y)
        x_months = self._calculate_target_months(y, x_np.shape[0])
        yearly_agg = np.vstack([yearly_agg] * x_np.shape[0])
        if self.static is not None:
            static_np = self._calculate_static(x_np.shape[0])
        else:
            static_np = None

        prev_y_var = self._get_prev_y_var(folder, list(y.data_vars)[0], y_np.shape[0])

        latlons, train_latlons = self._calculate_latlons(x)

        if self.experiment == "nowcast":
            # if nowcast then we have a TrainData.current
            historical = x_np[:, :-1, :]  # all timesteps except the final
            current = self.get_current_array(  # only select NON-TARGET vars
                x=x, y=y, x_np=x_np
            )

            train_data = TrainData(
                current=current,
                historical=historical,
                seq_length=x_months,
                latlons=train_latlons,
                yearly_aggs=yearly_agg,
                static=static_np,
                prev_y_var=prev_y_var,
            )

        else:
            train_data = TrainData(
                current=None,
                historical=x_np,
                seq_length=x_months,
                latlons=train_latlons,
                yearly_aggs=yearly_agg,
                static=static_np,
                prev_y_var=prev_y_var,
            )

        assert y_np.shape[0] == x_np.shape[0], (
            f"x and y data have a different "
            f"number of instances! x: {x_np.shape[0]}, y: {y_np.shape[0]}"
        )

        if clear_nans:
            # remove nans if they are in the x or y data
            historical_nans, y_nans = np.isnan(train_data.historical), np.isnan(y_np)
            if train_data.static is not None:
                static_nans = np.isnan(train_data.static)
                static_nans_summed = static_nans.sum(axis=-1)
            else:
                static_nans_summed = np.zeros((y_nans.shape[0],))

            historical_nans_summed = historical_nans.reshape(
                historical_nans.shape[0],
                historical_nans.shape[1] * historical_nans.shape[2],
            ).sum(axis=-1)
            y_nans_summed = y_nans.sum(axis=-1)
            prev_y_var_summed = np.isnan(prev_y_var).sum(axis=-1)

            notnan_indices = np.where(
                (historical_nans_summed == 0)
                & (y_nans_summed == 0)
                & (static_nans_summed == 0)
                & (prev_y_var_summed == 0)
            )[0]
            if self.experiment == "nowcast":
                current_nans = np.isnan(train_data.current)
                current_nans_summed = current_nans.sum(axis=-1)
                notnan_indices = np.where(
                    (historical_nans_summed == 0)
                    & (y_nans_summed == 0)
                    & (current_nans_summed == 0)
                    & (static_nans_summed == 0)
                    & (prev_y_var_summed == 0)
                )[0]

            train_data.filter(notnan_indices)

            y_np = y_np[notnan_indices]
            latlons = latlons[notnan_indices]

        y_var = list(y.data_vars)[0]
        model_arrays = ModelArrays(
            x=train_data,
            y=y_np,
            x_vars=list(x.data_vars),
            y_var=y_var,
            latlons=latlons,
            target_time=target_time,
            historical_times=x_datetimes,
        )

        if to_tensor:
            model_arrays.to_tensor(self.device)

        if self.predict_delta:
            # NOTE: data is not normalised in this function
            model_arrays.predict_delta = True
            historical_target_np = self._calculate_historical_target(x, y_var)
            historical_target_np = historical_target_np[notnan_indices].flatten()
            model_arrays.historical_target = historical_target_np
        return model_arrays

    @staticmethod
    def _add_extra_dims(
        x: xr.Dataset,
        surrounding_pixels: Optional[int] = None,
        monthly_agg: bool = True,
    ) -> xr.Dataset:
        original_vars = list(x.data_vars)
        coord = [c for c in x.coords if c != "time"][0]

        if monthly_agg:
            # first, the means
            monthly_mean_values = x.mean(dim=[coord])
            mean_dataset = xr.ones_like(x) * monthly_mean_values

            for var in mean_dataset.data_vars:
                x[f"spatial_mean_{var}"] = mean_dataset[var]
        return x

    @staticmethod
    def get_current_array(x: xr.Dataset, y: xr.Dataset, x_np: np.ndarray) -> np.ndarray:
        # get the target variable
        target_var = [y for y in y.data_vars][0]

        # get the target time and target_time index
        target_time = y.time
        x_datetimes = [pd.to_datetime(time) for time in x.time.values]
        y_datetime = pd.to_datetime(target_time.values[0])
        time_ix = [ix for ix, time in enumerate(x_datetimes) if time == y_datetime][0]

        # get the X features and X feature indices
        relevant_indices = [
            idx for idx, feat in enumerate(x.data_vars) if not feat.endswith(target_var)
        ]

        # (latlon, time, data_var)
        current = x_np[:, time_ix, relevant_indices]

        assert len(current.shape) == 2, (
            "Expected array: (lat*lon, time, dims)" f"Got:{current.shape}"
        )
        return current


class _TrainIter(_BaseIter):
    def __next__(
        self,
    ) -> Tuple[
        Tuple[Union[np.ndarray, torch.Tensor], ...], Union[np.ndarray, torch.Tensor]
    ]:

        global_modelarrays: Optional[ModelArrays] = None

        if self.idx < self.max_idx:

            cur_max_idx = min(self.idx + self.batch_file_size, self.max_idx)
            while self.idx < cur_max_idx:
                subfolder = self.data_files[self.idx]
                arrays = self.ds_folder_to_np(
                    subfolder, clear_nans=self.clear_nans, to_tensor=False
                )
                if arrays.x.historical.shape[0] == 0:
                    print(f"{subfolder} returns no values. Skipping")

                    # remove the empty element from the list
                    self.data_files.pop(self.idx)
                    self.max_idx -= 1
                    self.idx -= 1  # we're going to add one later

                    cur_max_idx = min(cur_max_idx + 1, self.max_idx)

                if global_modelarrays is None:
                    global_modelarrays = arrays
                else:
                    global_modelarrays.concatenate(arrays)

                self.idx += 1
            if global_modelarrays is not None:
                if self.to_tensor:
                    global_modelarrays.to_tensor(self.device)

                return (
                    (
                        global_modelarrays.x.historical,
                        global_modelarrays.x.seq_length,
                        global_modelarrays.x.latlons,
                        global_modelarrays.x.current,
                        global_modelarrays.x.yearly_aggs,
                        global_modelarrays.x.static,
                        global_modelarrays.x.prev_y_var,
                    ),
                    global_modelarrays.y,
                )
            else:
                raise StopIteration()

        else:  # final_x_curr >= self.max_idx
            raise StopIteration()


class _TestIter(_BaseIter):
    def __next__(self) -> Dict[str, ModelArrays]:

        if self.idx < self.max_idx:
            out_dict = {}

            cur_max_idx = min(self.idx + self.batch_file_size, self.max_idx)
            while self.idx < cur_max_idx:
                subfolder = self.data_files[self.idx]
                arrays = self.ds_folder_to_np(
                    subfolder, clear_nans=self.clear_nans, to_tensor=self.to_tensor
                )
                if arrays.x.historical.shape[0] == 0:
                    print(f"{subfolder} returns no values. Skipping")
                    # remove the empty element from the list
                    self.data_files.pop(self.idx)
                    self.max_idx -= 1
                    cur_max_idx = min(cur_max_idx + 1, self.max_idx)
                else:
                    out_dict[subfolder.parts[-1]] = arrays
                self.idx += 1

            if len(out_dict) == 0:
                raise StopIteration()
            return out_dict
        else:
            raise StopIteration()