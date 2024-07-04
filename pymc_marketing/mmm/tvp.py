#   Copyright 2024 The PyMC Labs Developers
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
"""
Time Varying Gaussian Process Multiplier for Marketing Mix Modeling (MMM).
Designed to model time-varying effects in marketing mix models (MMM).

This module provides a time-varying Gaussian Process (GP) multiplier,
using the Hilbert Space Gaussian Process (HSGP) approximation.

Examples
--------

Create a basic PyMC model using the time-varying GP multiplier:

.. code-block:: python

    import numpy as np
    import pymc as pm
    import pandas as pd
    from pymc_marketing.mmm.tvp import create_time_varying_gp_multiplier, infer_time_index

    # Generate example data
    np.random.seed(0)
    dates = pd.date_range(start="2020-01-01", periods=365)
    sales = np.random.normal(100, 10, size=len(dates))

    # Infer time index
    time_index = infer_time_index(dates, dates, time_resolution=5)

    # Define model configuration
    model_config = {
        "sales_tvp_config": {
            "m": 200,
            "L": None,
            "eta_lam": 1,
            "ls_mu": None,
            "ls_sigma": 5,
            "cov_func": None,
        }
    }

    with pm.Model() as model:
        # Shared time index variable
        time_index_shared = pm.Data("time_index", time_index)

        # Base parameter
        base_sales = pm.Normal("base_sales", mu=100, sigma=10)

        # Time-varying GP multiplier
        varying_coefficient = create_time_varying_gp_multiplier(
            name="sales",
            dims="time",
            time_index=time_index_shared,
            time_index_mid=int(len(dates) / 2),
            time_resolution=5,
            model_config=model_config,
        )

        # Final sales parameter
        sales_estimated = base_sales * varying_coefficient

        # Likelihood
        pm.Normal("obs", mu=sales_estimated, sigma=10, observed=sales)

    # Sample from the model
    with model:
        trace = pm.sample()

    # Plot results
    import matplotlib.pyplot as plt

    pm.plot_trace(trace, var_names=["base_sales"])
    plt.show()

"""

import numpy as np
import numpy.typing as npt
import pandas as pd
import pymc as pm
import pytensor.tensor as pt
from pymc.distributions.shape_utils import Dims

from pymc_marketing.constants import DAYS_IN_YEAR


from pymc_marketing.constants import DAYS_IN_YEAR

def time_varying_prior(
    name: str,
    X: pt.sharedvar.TensorSharedVariable,
    dims: Dims,
    X_mid: int | float | None = None,
    m: int = 200,
    L: int | float | None = None,
    eta_lam: float = 1,
    ls_mu: float = 5,
    ls_sigma: float = 5,
    cov_func: pm.gp.cov.Covariance | None = None,
) -> pt.TensorVariable:
    """Time varying prior, based on the Hilbert Space Gaussian Process (HSGP).

    For more information see `pymc.gp.HSGP <https://www.pymc.io/projects/docs/en/stable/api/gp/generated/pymc.gp.HSGP.html>`_.

    Parameters
    ----------
    name : str
        Name of the prior and associated variables.
    X : 1d array-like of int or float
        Time points.
    X_mid : int or float
        Midpoint of the time points.
    dims : tuple of str or str
        Dimensions of the prior. If a tuple, the first element is the name of
        the time dimension, and the second may be any other dimension, across
        which independent time varying priors for each coordinate are desired
        (e.g. channels).
    m : int
        Number of basis functions.
    L : int
        Extent of basis functions. Set this to reflect the expected range of
        in+out-of-sample data (considering that time-indices are zero-centered).
        Default is `X_mid * 2` (identical to `c=2` in HSGP).
    eta_lam : float
        Exponential prior for the variance.
    ls_mu : float
        Mean of the inverse gamma prior for the lengthscale.
    ls_sigma : float
        Standard deviation of the inverse gamma prior for the lengthscale.
    cov_func : pm.gp.cov.Covariance
        Covariance function.

    Returns
    -------
    pt.TensorVariable
        Time-varying prior.

    References
    ----------
    -   Ruitort-Mayol, G., and Anderson, M., and Solin, A., and Vehtari, A. (2022). Practical
        Hilbert Space Approximate Bayesian Gaussian Processes for Probabilistic Programming

    -   Solin, A., Sarkka, S. (2019) Hilbert Space Methods for Reduced-Rank Gaussian Process
        Regression.
    """

    if X_mid is None:
        X_mid = float(X.mean().eval())
    if L is None:
        L = X_mid * 2

    model = pm.modelcontext(None)

    if cov_func is None:
        eta = pm.Exponential(f"{name}_eta", lam=eta_lam)
        ls = pm.InverseGamma(f"{name}_ls", mu=ls_mu, sigma=ls_sigma)
        cov_func = eta**2 * pm.gp.cov.Matern52(1, ls=ls)

    model.add_coord("m", np.arange(m))  # type: ignore
    hsgp_dims: str | tuple[str, str] = "m"
    if isinstance(dims, tuple):
        hsgp_dims = (dims[1], "m")

    gp = pm.gp.HSGP(m=[m], L=[L], cov_func=cov_func)
    phi, sqrt_psd = gp.prior_linearized(X=X[:, None] - X_mid)
    hsgp_coefs = pm.Normal(f"{name}_hsgp_coefs", dims=hsgp_dims)
    f = phi @ (hsgp_coefs * sqrt_psd).T
    f = pt.softplus(f)
    centered_f = f - f.mean(axis=0) + 1
    return pm.Deterministic(name, centered_f, dims=dims)


def create_time_varying_gp_multiplier(
    name: str,
    dims: Dims,
    time_index: pt.sharedvar.TensorSharedVariable,
    time_index_mid: int,
    time_resolution: int,
    model_config: dict,
) -> pt.TensorVariable:
    """Create a time-varying Gaussian Process multiplier.

    Create a time-varying Gaussian Process multiplier based on the provided parameters.

    Parameters
    ----------
    name : str
        Name of the Gaussian Process multiplier.
    dims : tuple[str, str] | str
        Dimensions for the multiplier.
    time_index : pt.sharedvar.TensorSharedVariable
        Shared variable containing time points.
    time_index_mid : int
        Midpoint of the time points.
    time_resolution : int
        Resolution of time points.
    model_config : dict
        Configuration dictionary for the model.

    Returns
    -------
    pt.TensorVariable
        Time-varying Gaussian Process multiplier for a given variable.
    """

    tvp_config = model_config[f"{name}_tvp_config"]

    if tvp_config["L"] is None:
        tvp_config["L"] = time_index_mid + DAYS_IN_YEAR / time_resolution
    if tvp_config["ls_mu"] is None:
        tvp_config["ls_mu"] = DAYS_IN_YEAR / time_resolution * 2

    multiplier = time_varying_prior(
        name=f"{name}_temporal_latent_multiplier",
        X=time_index,
        X_mid=time_index_mid,
        dims=dims,
        **tvp_config,
    )
    return multiplier

def infer_time_index(
    date_series_new: pd.Series, date_series: pd.Series, time_resolution: int
) -> npt.NDArray[np.int_]:
    """Infer the time-index given a new dataset.

    Infers the time-indices by calculating the number of days since the first date in the dataset.
    """
    return ((date_series_new - date_series[0]).days // time_resolution).astype(int)
