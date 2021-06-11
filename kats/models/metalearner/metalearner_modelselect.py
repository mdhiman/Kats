#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""A module for meta-learner model selection.

This module contains:
    - :class:`MetaLearnModelSelect` for meta-learner models selection, which recommends the forecasting model based on time series or time series features;
    - :class:`RandomDownSampler` for creating balanced dataset via downsampling.
"""

import ast
import logging
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple, Union

import joblib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from kats.consts import TimeSeriesData
from kats.tsfeatures.tsfeatures import TsFeatures
from sklearn import metrics
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.naive_bayes import GaussianNB
from sklearn.neighbors import KNeighborsClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC


class MetaLearnModelSelect:
    """Meta-learner framework on forecasting model selection.
    This framework uses classification algorithms to recommend suitable forecasting models.
    For training, it uses time series features as inputs and the best forecasting models as labels.
    For prediction, it takes time series or time series features as inputs to predict the most suitable forecasting model.
    The class provides count_category, preprocess, plot_feature_comparison, get_corr_mtx, plot_corr_heatmap, train, pred, pred_by_feature, pred_fuzzy, load_model and save_model.

    Attributes:
        metadata: Optional; A list of dictionaries representing the meta-data of time series (e.g., the meta-data generated by GetMetaData object).
                  Each dictionary d must contain at least 3 components: 'hpt_res', 'features' and 'best_model'. d['hpt_res'] represents the best hyper-parameters for each candidate model and the corresponding errors;
                  d['features'] are time series features, and d['best_model'] is a string representing the best candidate model of the corresponding time series data.
                  metadata should not be None unless load_model is True. Default is None.
        load_model: Optional; A boolean to specify whether or not to load a trained model. Default is False.

    Sample Usage:
        >>> mlms = MetaLearnModelSelect(data)
        >>> mlms.train(n_trees=200, test_size=0.1, eval_method='mean') # Train a meta-learner model selection model.
        >>> mlms.pred(TSdata) # Predict/recommend forecasting model for a new time series data.
        >>> mlms2.pred(TSdata, n_top=3) # Predict/recommend the top 3 most suitable forecasting model.
        >>> mlms.save_model("mlms.pkl") # Save the trained model.
        >>> mlms2 = MetaLearnModelSelect(metadata=None, load_model=True) # Create a new object and then load a pre-trained model.
        >>> mlms2.load_model("mlms.pkl")
    """

    def __init__(
        self, metadata: Optional[List[Dict[str, Any]]] = None, load_model: bool = False
    ) -> None:
        if not load_model:
            # pyre-fixme[6]: Expected `Sized` for 1st param but got
            #  `Optional[List[typing.Any]]`.
            if len(metadata) <= 30:
                msg = "Dataset is too small to train a meta learner!"
                logging.error(msg)
                raise ValueError(msg)

            if metadata is None:
                msg = "Missing metadata!"
                logging.error(msg)
                raise ValueError(msg)

            if "hpt_res" not in metadata[0]:
                msg = "Missing best hyper-params, not able to train a meta learner!"
                logging.error(msg)
                raise ValueError(msg)

            if "features" not in metadata[0]:
                msg = "Missing time series features, not able to train a meta learner!"
                logging.error(msg)
                raise ValueError(msg)

            if "best_model" not in metadata[0]:
                msg = "Missing best models, not able to train a meta learner!"
                logging.error(msg)
                raise ValueError(msg)

            self.metadata = metadata
            self._reorganize_data()
            self._validate_data()

            self.scale = False
            self.clf = None
        elif load_model:
            pass
        else:
            msg = "Fail to initiate MetaLearnModelSelect."
            raise ValueError(msg)

    def _reorganize_data(self) -> None:
        hpt_list = []
        metadataX_list = []
        metadataY_list = []
        for i in range(len(self.metadata)):
            if isinstance(self.metadata[i]["hpt_res"], str):
                hpt_list.append(ast.literal_eval(self.metadata[i]["hpt_res"]))
            else:
                hpt_list.append(self.metadata[i]["hpt_res"])

            if isinstance(self.metadata[i]["features"], str):
                metadataX_list.append(
                    list(ast.literal_eval(self.metadata[i]["features"]).values())
                )
            else:
                metadataX_list.append(list(self.metadata[i]["features"].values()))

            metadataY_list.append(self.metadata[i]["best_model"])

        self.col_namesX = list(self.metadata[0]["features"].keys())
        self.hpt = pd.Series(hpt_list, name="hpt")
        self.metadataX = pd.DataFrame(metadataX_list, columns=self.col_namesX)
        self.metadataX.fillna(0, inplace=True)
        self.metadataY = pd.Series(metadataY_list, name="y")
        self.x_mean = np.average(self.metadataX.values, axis=0)
        self.x_std = np.std(self.metadataX.values, axis=0)
        self.x_std[self.x_std == 0] = 1.0

    def _validate_data(self):
        num_class = self.metadataY.nunique()
        if num_class == 1:
            msg = "Only one class in the label column (best_model), not able to train a classifier!"
            logging.error(msg)
            raise ValueError(msg)

        local_count = list(self.count_category().values())
        if min(local_count) * num_class < 30:
            msg = "Not recommend to do downsampling! Dataset will be too small after downsampling!"
            logging.info(msg)
        elif max(local_count) > min(local_count) * 5:
            msg = "Number of obs in majority class is much greater than in minority class. Downsampling is recommended!"
            logging.info(msg)
        else:
            msg = "No significant data imbalance problem, no need to do downsampling."
            logging.info(msg)

    def count_category(self) -> Dict[str, int]:
        """Count the number of observations of each candidate model in meta-data.

        Returns:
            A dictionary storing the number of observations of each candidate model in meta-data.
        """

        return Counter(self.metadataY)

    def preprocess(self, downsample: bool = True, scale: bool = False) -> None:
        """Pre-process meta data before training a classifier.

        There are 2 options in this function: 1) whether or not to downsample meta-data to ensure each candidate model has the same number of observations;
        and 2) whether or not to rescale the time series features to zero-mean and unit-variance.

        Args:
            downsample: Optional; A boolean to specify whether or not to downsample meta-data to ensure each candidate model has the same number of observations.
                        Default is True.
            scale: Optional; A boolean to specify whether or not to rescale the time series features to zero-mean and unit-variance.

        Returns:
            None
        """

        if downsample:
            self.hpt, self.metadataX, self.metadataY = RandomDownSampler(
                self.hpt, self.metadataX, self.metadataY
            ).fit_resample()
            logging.info("Successfully applied random downsampling!")
            self.x_mean = np.average(self.metadataX.values, axis=0)
            self.x_std = np.std(self.metadataX.values, axis=0)
            self.x_std[self.x_std == 0] = 1.0

        if scale:
            self.scale = True
            self.metadataX = (self.metadataX - self.x_mean) / self.x_std
            logging.info(
                "Successfully scaled data by centering to the mean and component-wise scaling to unit variance!"
            )

    def plot_feature_comparison(self, i: int, j: int) -> None:
        """Generate the time series features comparison plot.

        Args:
            i: A integer representing the index of one feature vector from feature matrix to be compared.
            j: A integer representing the other index of one feature vector from feature matrix to be compared.

        Returns:
            None
        """

        combined = pd.concat([self.metadataX.iloc[i], self.metadataX.iloc[j]], axis=1)
        combined.columns = [
            str(self.metadataY.iloc[i]) + " model",
            str(self.metadataY.iloc[j]) + " model",
        ]
        # pyre-fixme[29]: `CachedAccessor` is not a function.
        combined.plot(kind="bar", figsize=(12, 6))

    def get_corr_mtx(self) -> pd.DataFrame:
        """Calculate correlation matrix of feature matrix.

        Returns:
            A pd.DataFrame representing the correlation matrix of time series features.
        """

        return self.metadataX.corr()

    def plot_corr_heatmap(self, camp: str = "RdBu_r") -> None:
        """Generate heat-map for correlation matrix of feature matrix.

        Args:
            camp: Optional; A string representing the olor bar used to generate heat-map. Default is "RdBu_r".

        Returns:
            None
        """

        fig, _ = plt.subplots(figsize=(8, 6))
        _ = sns.heatmap(
            self.get_corr_mtx(),
            cmap=camp,
            yticklabels=self.metadataX.columns,
            xticklabels=self.metadataX.columns,
        )

    def train(
        self,
        method: str = "RandomForest",
        eval_method: str = "mean",
        test_size: float = 0.1,
        n_trees: int = 500,
        n_neighbors: int = 5,
    ) -> Dict[str, Any]:
        """Train a meta-learner model selection model (i.e., a classifier).

        Args:
            method: Optional; A string representing the name of the classification algorithm. Can be 'RandomForest', 'GBDT', 'SVM', 'KNN' or 'NaiveBayes'. Default is 'RandomForest'.
            eval_method: Optional; A string representing the aggregation method used for computing errors. Can be 'mean' or 'median'. Default is 'mean'.
            test_size: Optional; A float representing the proportion of test set, which should be within (0, 1). Default is 0.1.
            n_trees: Optional; An integer representing the number of trees in random forest model. Default is 500.
            n_neighbors: Optional; An integer representing the number of neighbors in KNN model. Default is 5.

        Returns:
            A dictionary summarizing the performance of the trained classifier on both training and validation set.
        """

        if method not in ["RandomForest", "GBDT", "SVM", "KNN", "NaiveBayes"]:
            msg = "Only support RandomForest, GBDT, SVM, KNN, and NaiveBayes method."
            logging.error(msg)
            raise ValueError(msg)

        if eval_method not in ["mean", "median"]:
            msg = "Only support mean and median as evaluation method."
            logging.error(msg)
            raise ValueError(msg)

        if test_size <= 0 or test_size >= 1:
            msg = "Illegal test set."
            logging.error(msg)
            raise ValueError(msg)

        x_train, x_test, y_train, y_test, hpt_train, hpt_test = train_test_split(
            self.metadataX, self.metadataY, self.hpt, test_size=test_size
        )

        if method == "RandomForest":
            clf = RandomForestClassifier(n_estimators=n_trees)
        elif method == "GBDT":
            clf = GradientBoostingClassifier()
        elif method == "SVM":
            clf = make_pipeline(StandardScaler(), SVC(gamma="auto"))
        elif method == "KNN":
            clf = KNeighborsClassifier(n_neighbors=n_neighbors)
        else:
            clf = GaussianNB()

        clf.fit(x_train, y_train)
        y_fit = clf.predict(x_train)
        y_pred = clf.predict(x_test)

        # calculate model errors
        fit_error, pred_error = {}, {}

        # evaluate method
        em = np.mean if eval_method == "mean" else np.median

        # meta learning errors
        fit_error["meta-learn"] = em(
            [hpt_train.iloc[i][c][-1] for i, c in enumerate(y_fit)]
        )
        pred_error["meta-learn"] = em(
            [hpt_test.iloc[i][c][-1] for i, c in enumerate(y_pred)]
        )

        # pre-selected model errors, for all candidate models
        for label in self.metadataY.unique():
            fit_error[label] = em(
                [hpt_train.iloc[i][label][-1] for i in range(len(hpt_train))]
            )
            pred_error[label] = em(
                [hpt_test.iloc[i][label][-1] for i in range(len(hpt_test))]
            )

        self.clf = clf
        return {
            "fit_error": fit_error,
            "pred_error": pred_error,
            "clf_accuracy": metrics.accuracy_score(y_test, y_pred),
        }

    def save_model(self, file_name: str) -> None:
        """Save the trained model.

        Args:
            file_name: A string representing the path to save the trained model.

        Returns:
            None.
        """

        if self.clf is None:
            msg = "Haven't trained a model."
            logging.error(msg)
            raise ValueError(msg)
        else:
            joblib.dump(self.__dict__, file_name)
            logging.info("Successfully saved the trained model!")

    def load_model(self, file_name: str) -> None:
        """Load a pre-trained model.

        Args:
            file_name: A string representing the path to load the pre-trained model.

        Returns:
            None.
        """

        try:
            self.__dict__ = joblib.load(file_name)
            logging.info("Successfully loaded a pre-trained model!")
        except Exception:
            msg = "No existing pre-trained model. Please change file path or train a model first!"
            logging.error(msg)
            raise ValueError(msg)

    def pred(
        self, source_ts: TimeSeriesData, ts_scale: bool = True, n_top: int = 1
    ) -> Union[str, List[str]]:
        """Predict the best forecasting model for a new time series data.

        Args:
            source_ts: :class:`kats.consts.TimeSeriesData` object representing the new time series data.
            ts_scale: Optional; A boolean to specify whether or not to rescale time series data (i.e., normalizing it with its maximum vlaue) before calculating features. Default is True.
            n_top: Optional; A integer for the number of top model names to return. Default is 1.

        Returns:
            A string or a list of strings of the names of forecasting models.
        """

        ts = TimeSeriesData(pd.DataFrame(source_ts.to_dataframe().copy()))
        if self.clf is None:
            msg = "Haven't trained a model. Please train a model or load a model before predicting."
            logging.error(msg)
            raise ValueError(msg)

        if ts_scale:
            # scale time series to make ts features more stable
            ts.value /= ts.value.max()
            msg = "Successful scaled! Each value of TS has been divided by the max value of TS."
            logging.info(msg)

        new_features = TsFeatures().transform(ts)
        new_features_vector = np.asarray(list(new_features.values()))
        if np.any(np.isnan(new_features_vector)):
            msg = (
                "Features of the test time series contains NaN value, consider processing it. Features are: "
                f"{new_features}. Fill in NaNs with 0."
            )
            logging.warning(msg)
        return self.pred_by_feature([new_features_vector], n_top=n_top)[0]

    def pred_by_feature(
        self,
        source_x: Union[np.ndarray, List[np.ndarray], pd.DataFrame],
        n_top: int = 1,
    ) -> np.ndarray:
        """Predict the best forecasting models given a list/dataframe of time series features
        Args:
            source_x: the time series features of the time series that one wants to predict, can be a np.ndarray, a list of np.ndarray or a pd.DataFrame.
            n_top: Optional; An integer for the number of top model names to return. Default is 1.

        Returns:
            An array of strings representing the forecasing models. If n_top=1, a 1-d np.ndarray will be returned. Otherwise, a 2-d np.ndarray will be returned.
        """

        if self.clf is None:
            msg = "Haven't trained a model. Please train a model or load a model before predicting."
            logging.error(msg)
            raise ValueError(msg)
        if isinstance(source_x, List):
            x = np.row_stack(source_x)
        elif isinstance(source_x, np.ndarray):
            x = source_x.copy()
        else:
            msg = f"Invalid source_x type: {type(source_x)}."
            logging.error(msg)
            raise ValueError(msg)
        if self.scale:
            x = (x - self.x_mean) / self.x_std
        x[np.isnan(x)] = 0.0
        if n_top == 1:
            return self.clf.predict(x)
        prob = self.clf.predict_proba(x)
        order = np.argsort(-prob, axis=1)
        classes = np.array(self.clf.classes_)
        return classes[order][:, :n_top]

    def _bootstrap(self, data: np.ndarray, rep: int = 200) -> float:
        """Helper function for bootstrap test and returns the pvalue."""

        diff = data[:, 0] - data[:, 1]
        n = len(diff)
        idx = np.random.choice(np.arange(n), n * rep)
        sample = diff[idx].reshape(-1, n)
        bs = np.average(sample, axis=1)
        pvalue = np.average(bs < 0)
        return pvalue

    def pred_fuzzy(
        self, source_ts: TimeSeriesData, ts_scale: bool = True, sig_level: float = 0.2
    ) -> Dict[str, Any]:
        """Predict a forecasting model for a new time series data using fuzzy method.

        The fuzzy method returns the best candiate model and the second best model will be returned if there is no statistically significant difference between them.
        The statistical test is based on the bootstrapping samples drawn from the fitted random forest model. This function is only available for random forest classifier.

        Args:
            source_ts: :class:`kats.consts.TimeSeriesData` object representing the new time series data.
            ts_scale: Optional; A boolean to specify whether or not to rescale time series data (i.e., normalizing it with its maximum vlaue) before calculating features. Default is True.
            sig_level: Optional; A float representing the significance level for bootstrap test. If pvalue>=sig_level, then we deem there is no difference between the best and the second best model.
                       Default is 0.2.

        Returns:
            A dictionary of prediction results, including forecasting models, their probability of being th best forecasting models and the pvalues of bootstrap tests.
        """

        ts = TimeSeriesData(pd.DataFrame(source_ts.to_dataframe().copy()))
        if ts_scale:
            # scale time series to make ts features more stable
            ts.value /= ts.value.max()
        test = np.asarray(list(TsFeatures().transform(ts).values()))
        test[np.isnan(test)] = 0.0
        if self.scale:
            test = (test - self.x_mean) / self.x_std
        test = test.reshape([1, -1])
        m = len(self.clf.estimators_)
        data = np.array(
            [self.clf.estimators_[i].predict_proba(test)[0] for i in range(m)]
        )
        prob = self.clf.predict_proba(test)[0]
        idx = np.argsort(-prob)[:2]
        pvalue = self._bootstrap(data[:, idx[:2]])
        if pvalue >= sig_level:
            label = self.clf.classes_[idx[:2]]
            prob = prob[idx[:2]]
        else:
            label = self.clf.classes_[idx[:1]]
            prob = prob[idx[:1]]
        ans = {"label": label, "probability": prob, "pvalue": pvalue}
        return ans

    def __str__(self):
        return "MetaLearnModelSelect"


class RandomDownSampler:
    """An assistant class for class MetaLearnModelSelect to do random downsampling.

    RandomDownSampler provides methods for creating a balanced dataset via downsampling. It contains fit_resample.

    Attributes:
        hpt: A `pandas.Series` object storing the best hyper-parameters and the corresponding errors for each model.
        dataX: A `pandas.DataFrame` object representing the time series features matrix.
        dataY: A `pandas.Series` object representing the best models for the corresponding time series.
    """

    def __init__(self, hpt: pd.Series, dataX: pd.DataFrame, dataY: pd.Series) -> None:
        self.hpt = hpt
        self.dataX = dataX
        self.dataY = dataY
        self.col_namesX = self.dataX.columns

    def fit_resample(self) -> Tuple[pd.Series, pd.DataFrame, pd.Series]:
        """Create balanced dataset via random downsampling.

        Returns:
            A tuple containing the `pandas.Series` object of the best hyper-parameters and the corresponding errors, the `pandas.DataFrame` object of the downsampled time series features,
            and the `pandas.Series` object of the downsampled best models for the corresponding time series.
        """

        resampled_x, resampled_y, resampled_hpt = [], [], []
        # naive down-sampler technique for data imbalance problem
        min_n = min(Counter(self.dataY).values())

        idx_dict = defaultdict(list)
        for i, c in enumerate(self.dataY):
            idx_dict[c].append(i)

        for key in idx_dict:
            idx_dict[key] = np.random.choice(idx_dict[key], size=min_n, replace=False)
            resampled_x += self.dataX.iloc[np.asarray(idx_dict[key]), :].values.tolist()
            resampled_y += list(self.dataY.iloc[np.asarray(idx_dict[key])])
            resampled_hpt += list(self.hpt.iloc[np.asarray(idx_dict[key])])

        resampled_x = pd.DataFrame(resampled_x)
        resampled_x.columns = self.col_namesX

        resampled_y = pd.Series(resampled_y, name="y")
        resampled_hpt = pd.Series(resampled_hpt, name="hpt")

        return resampled_hpt, resampled_x, resampled_y
