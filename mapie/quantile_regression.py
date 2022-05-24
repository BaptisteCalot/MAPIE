from __future__ import annotations
from typing import Optional, Tuple, Union, cast, Iterable, List

import numpy as np
from sklearn.base import RegressorMixin, clone
from sklearn.linear_model import QuantileRegressor
from sklearn.pipeline import Pipeline
from sklearn.utils.validation import (
    indexable,
    check_is_fitted,
    _num_samples,
    _check_y,
)
from typing_extensions import TypedDict

from ._typing import ArrayLike, NDArray
from .utils import (
    check_alpha_and_n_samples,
    check_n_features_in,
    check_null_weight,
    fit_estimator,
    check_lower_upper_bounds,
)
from ._compatibility import np_quantile
from .regression import MapieRegressor


class MapieQuantileRegressor(MapieRegressor):
    valid_methods_ = ["quantile"]
    fit_attributes = [
        "estimators_",
        "conformity_scores_",
        "n_samples_calib",
        "n_features_in_"
    ]

    Params = TypedDict(
        "Params",
        {
            "loss_name": str,
            "alpha_name": str
        },
    )
    quantile_estimator_params = {
        "GradientBoostingRegressor": Params(
            loss_name="loss",
            alpha_name="alpha"
        ),
        "QuantileRegressor": Params(
            loss_name="quantile",
            alpha_name="quantile"
        ),
        "HistGradientBoostingRegressor": Params(
            loss_name="loss",
            alpha_name="alpha"
        ),
        "LGBMRegressor": Params(
            loss_name="objective",
            alpha_name="alpha"
        )
    }

    def __init__(
        self,
        estimator: Optional[Union[RegressorMixin, Pipeline]] = None,
        method: str = "quantile",
        alpha: float = 0.1,
        cv: Union[int, str] = None,
    ) -> None:
        super().__init__(
            estimator=estimator,
            method=method,
            cv=cv
        )
        self.alpha = alpha

    def _check_alpha(
        self,
        alpha: float,
    ) -> NDArray:
        """
        Perform several checks on the alpha value and changes it from
        a float to an ArrayLike

        Parameters
        ----------
        alpha : float
            Can only be a float value between 0 and 0.5.
            Represent the uncertainty of the confidence interval.
            Lower alpha produce larger (more conservative) prediction
            intervals. Alpha is the complement of the target coverage level.
            Only used at prediction time. By default 0.1

        Returns
        -------
        ArrayLike
            An ArrayLike of three values, first one being the lower quantile
            value, second the upper quantile value and final being the quantile
            at which the prediction will be made.

        Raises
        ------
        ValueError
            If alpha is not a float.
        ValueError
            If the value of alpha is not between 0 and 0.5.
        """
        if isinstance(alpha, float):
            if np.any(np.logical_or(alpha <= 0, alpha >= 0.5)):
                raise ValueError(
                    "Invalid alpha. Allowed values are between 0 and 0.5."
                )
            else:
                alpha_np = np.array([alpha / 2, 1 - alpha / 2, 0.5])
        else:
            raise ValueError(
                "Invalid alpha. Allowed values are float."
            )
        return alpha_np

    def _check_estimator(
        self,
        estimator: Optional[Union[RegressorMixin, Pipeline]] = None
    ) -> Union[RegressorMixin, Pipeline]:
        """
        Perform several checks on the estimator to check if it has
        all the required specifications to be used with this methodology.

        Parameters
        ----------
        estimator : Optional[RegressorMixin], optional
            Estimator to check, by default ``None``.

        Returns
        -------
        RegressorMixin
            The estimator itself or a default ``QuantileRegressor`` instance
            with ``solver`` set to "highs".

        Raises
        ------
        ValueError
            If the estimator fit or predict methods.
        ValueError
            We check if it's a known estimator that does quantile regression
            according to the dictionnary set quantile_estimator_params.
            This dictionnary will need to be updated with the latest new
            available estimators.
        ValueError
            The estimator does not have the "loss_name" in its parameters and
            therefore can not be used as an estimator.
        ValueError
            There is no quantile "loss_name" and therefore this estimator
            can not be used as a ``MapieQuantileRegressor``.
        ValueError
            The parameter to set the alpha value does not exist in this
            estimator and therefore we cannot use it.
        """
        if estimator is None:
            return QuantileRegressor(solver="highs")
        if not (hasattr(estimator, "fit") and hasattr(estimator, "predict")):
            raise ValueError(
                "Invalid estimator. "
                "Please provide a regressor with fit and predict methods."
            )
        if isinstance(estimator, Pipeline):
            self._check_estimator(estimator[-1])
            return estimator
        else:
            name_estimator = estimator.__class__.__name__
            if name_estimator == "QuantileRegressor":
                return estimator
            else:
                if name_estimator in self.quantile_estimator_params:
                    param_estimator = estimator.get_params()
                    loss_name, alpha_name = self.quantile_estimator_params[
                        name_estimator].values()
                    if loss_name in param_estimator:
                        if param_estimator[loss_name] != "quantile":
                            raise ValueError(
                                "You need to set the loss/objective argument"
                                + " of your base model ``quantile``."
                            )
                        else:
                            if alpha_name in param_estimator:
                                return estimator
                            else:
                                raise ValueError(
                                    "The matching parameter alpha_name for"
                                    + " estimator does not exist."
                                )
                    else:
                        raise ValueError(
                            "The matching parameter loss_name for"
                            + " estimator does not exist."
                        )
                else:
                    raise ValueError(
                        "The base model does not seem to be accepted"
                        + " by MapieQuantileRegressor."
                    )

    def _check_cv(
        self,
        cv: Union[int, str] = None
    ) -> Union[int, str]:
        """
        Check if cv argument is None or "split".

        Parameters
        ----------
        cv : Union[int, str], optional
           cv to check, by default ``None``.

        Returns
        -------
        str
            cv itself or a default "split".

        Raises
        ------
        ValueError
            Raises an error if the cv is anything else but the method "split".
            Only the split method has been implemented.
        """
        if cv is None:
            return "split"
        if cv == "split":
            return cv
        else:
            raise ValueError(
                "Invalid cv method."
            )

    def fit(  # type: ignore
        self,
        X: ArrayLike,
        y: ArrayLike,
        X_calib: ArrayLike,
        y_calib: ArrayLike,
        sample_weight: Optional[ArrayLike] = None,
    ) -> MapieQuantileRegressor:
        """
        Fit estimator and compute residuals used for prediction intervals.
        All the clones of the estimators for different quantile values are
        stored in order alpha/2, 1 - alpha/2, 0.5 in the ``estimators_``
        attribute. Residuals for the first two estimators and the maximum
        of residuals among these residuals are stored in the
        ``conformity_scores_`` attribute.

        Parameters
        ----------
        X : ArrayLike of shape (n_samples, n_features)
            Training data.
        y : ArrayLike of shape (n_samples,)
            Training labels.
        X_calib : ArrayLike of shape (n_samples, n_features)
            Calibration data.
        y_calib : ArrayLike of shape (n_samples,)
            Calibration labels.
        sample_weight : Optional[ArrayLike] of shape (n_samples,)
            Sample weights for fitting the out-of-fold models.
            If None, then samples are equally weighted.
            If some weights are null,
            their corresponding observations are removed
            before the fitting process and hence have no residuals.
            If weights are non-uniform, residuals are still uniformly weighted.

            By default ``None``.

        Returns
        -------
        MapieQuantileRegressor
             The model itself.
        """
        # Checks
        self._check_parameters()
        checked_estimator = self._check_estimator(self.estimator)
        alpha = self._check_alpha(self.alpha)
        self.cv = self._check_cv(self.cv)
        X, y = indexable(X, y)
        X_calib, y_calib = indexable(X_calib, y_calib)
        y = _check_y(y)
        y_calib = _check_y(y_calib)
        self.n_samples_calib = _num_samples(y_calib)
        check_alpha_and_n_samples(self.alpha, self.n_samples_calib)
        self.n_features_in_ = check_n_features_in(
            X,
            estimator=checked_estimator
            )
        sample_weight, X, y = check_null_weight(
            sample_weight,
            X,
            y
        )
        y = cast(NDArray, y)

        # Initialization
        self.estimators_: List[RegressorMixin] = []

        # Work
        y_calib_preds = np.full(
            shape=(3, self.n_samples_calib),
            fill_value=np.nan
        )

        if isinstance(checked_estimator, Pipeline):
            estimator = checked_estimator[-1]
        else:
            estimator = checked_estimator
        name_estimator = estimator.__class__.__name__
        for i, alpha_ in enumerate(alpha):
            alpha_name = self.quantile_estimator_params[
                name_estimator
                ]["alpha_name"]
            cloned_estimator_ = clone(checked_estimator)
            params = {alpha_name: alpha_}
            if isinstance(checked_estimator, Pipeline):
                cloned_estimator_[-1].set_params(**params)
            else:
                cloned_estimator_.set_params(**params)
            self.estimators_.append(fit_estimator(
                cloned_estimator_, X, y, sample_weight
            ))
            y_calib_preds[i] = self.estimators_[-1].predict(X_calib)

        self.conformity_scores_ = np.full(
                shape=(3, self.n_samples_calib),
                fill_value=np.nan
            )
        self.conformity_scores_[0] = y_calib_preds[0]-y_calib
        self.conformity_scores_[1] = y_calib-y_calib_preds[1]
        self.conformity_scores_[2] = np.max(
            [
                self.conformity_scores_[0],
                self.conformity_scores_[1]
            ], axis=0
        )
        return self

    def predict(
        self,
        X: ArrayLike,
        ensemble: Optional[bool] = False,
        alpha: Optional[Union[float, Iterable[float]]] = None,
        symmetry: Optional[bool] = True,
    ) -> Union[NDArray, Tuple[NDArray, NDArray]]:
        """
        Predict target on new samples with confidence intervals.
        Residuals from the training set and predictions from the model clones
        are central to the computation.
        Prediction Intervals for a given ``alpha`` are deduced from the
        quantile regression at the alpha values: alpha/2, 1 - (alpha/2)
        while adding a constant based uppon their residuals.

        Parameters
        ----------
        X : ArrayLike of shape (n_samples, n_features)
            Test data.
        symmetry : Optional[bool], optional
            Deciding factor to whether to find the quantile value for
            each residuals separatly or to use the maximum of the two
            combined. This results in having either symmetric constants
            added for both upper and lower bounds or not.

        Returns
        -------
        Union[NDArray, Tuple[NDArray, NDArray]]

        - NDArray of shape (n_samples,) if alpha is None.

        - Tuple[NDArray, NDArray] of shapes
        (n_samples,) and (n_samples, 2, n_alpha) if alpha is not None.

            - [:, 0, :]: Lower bound of the prediction interval.
            - [:, 1, :]: Upper bound of the prediction interval.
        """
        check_is_fitted(self, self.fit_attributes)
        check_alpha_and_n_samples(self.alpha, self.n_samples_calib)

        n = self.n_samples_calib
        q = (1 - (self.alpha)) * (1 + (1 / n))

        y_preds = np.full(
            shape=(3, _num_samples(X)),
            fill_value=np.nan
        )
        for i, est in enumerate(self.estimators_):
            y_preds[i] = est.predict(X)
        if symmetry:
            quantile = np.full(
                2,
                np_quantile(
                    self.conformity_scores_[2], q, method="higher"
                )
            )
        else:
            check_alpha_and_n_samples(self.alpha / 2, n)
            q = (1 - (self.alpha / 2)) * (1 + (1 / n))
            quantile = np.array(
                [
                    np_quantile(
                        self.conformity_scores_[0], q, method="higher"
                    ),
                    np_quantile(
                        self.conformity_scores_[1], q, method="higher"
                    )
                ]
            )
        y_pred_low = y_preds[0][:, np.newaxis] - quantile[0]
        y_pred_up = y_preds[1][:, np.newaxis] + quantile[1]
        check_lower_upper_bounds(y_preds, y_pred_low, y_pred_up)
        return y_preds[2], np.stack([y_pred_low, y_pred_up], axis=1)
