#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

import abc
from typing import Mapping, Optional, Tuple, Union

import numpy as np
import torch
from aepsych.config import Config
from aepsych.utils import dim_grid, _process_bounds, get_jnd_multid, make_scaled_sobol
from gpytorch.kernels import Kernel
from gpytorch.likelihoods import Likelihood
from gpytorch.means import Mean
from scipy.optimize import minimize

torch.set_default_dtype(torch.double)  # TODO: find a better way to prevent type errors


class AEPsychModel(abc.ABC):
    outcome_type = ""

    @abc.abstractmethod
    def __init__(
        self,
        lb: Union[np.ndarray, torch.Tensor],
        ub: Union[np.ndarray, torch.Tensor],
        dim: Optional[int],
        mean_module: Mean,
        covar_module: Kernel,
        likelihood: Likelihood,
    ):
        self.lb, self.ub, self.dim = _process_bounds(lb, ub, dim)
        self.bounds_ = torch.stack([self.lb, self.ub])

        self.mean_module = mean_module
        self.covar_module = covar_module
        self.likelihood = likelihood

    @abc.abstractmethod
    def fit(self, train_x: torch.Tensor, train_y: torch.Tensor) -> None:
        pass

    def update(
        self, train_x: torch.Tensor, train_y: torch.Tensor, warmstart: bool = True
    ) -> None:
        self.fit(train_x, train_y)

    @abc.abstractmethod
    def predict(
        self, x: Union[torch.Tensor, np.ndarray], probability_space: bool = False
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        pass

    @abc.abstractmethod
    def sample(
        self, x: Union[torch.Tensor, np.ndarray], num_samples: int
    ) -> torch.Tensor:
        pass

    def get_max(self) -> Tuple[float, np.ndarray]:
        """Return the maximum of the modeled function
        Returns:
            Tuple[float, np.ndarray]: Tuple containing the max and its location (argmax).
        """
        return self._get_extremum("max")

    def _get_extremum(
        self, extremum_type: str, n_samples: int = 1000
    ) -> Tuple[float, np.ndarray]:
        """Return the extremum (min or max) of the modeled function
        Args:
            extremum_type (str): type of extremum (currently 'min' or 'max'
            n_samples (int, optional): number of coarse grid points to sample for optimization estimate.
        Returns:
            Tuple[float, np.ndarray]: Tuple containing the min and its location (argmin).
        """

        def signed_model(x, sign=1):
            return sign * self.predict(torch.tensor([x]))[0].numpy()

        bounds = zip(self.lb.numpy(), self.ub.numpy())

        # generate a coarse sample to compute an initial estimate.
        d = make_scaled_sobol(self.lb, self.ub, n_samples, seed=0)

        fmean, _ = self.predict(d)

        if extremum_type == "max":
            estimate = d[torch.where(fmean == torch.max(fmean))[0][0]].numpy()
            a = minimize(
                signed_model, estimate, args=-1, method="Powell", bounds=bounds
            )
            return -a.fun, a.x
        elif extremum_type == "min":
            estimate = d[torch.where(fmean == torch.min(fmean))[0][0]]
            a = minimize(signed_model, estimate, args=1, method="Powell", bounds=bounds)
            return a.fun, a.x

        else:
            raise RuntimeError(
                f"Unknown extremum type: '{extremum_type}'! Valid types: 'min', 'max' "
            )

    def get_min(self) -> Tuple[float, np.ndarray]:
        """Return the minimum of the modeled function
        Returns:
            Tuple[float, np.ndarray]: Tuple containing the min and its location (argmin).
        """
        return self._get_extremum("min")

    def inv_query(
        self,
        y: float,
        locked_dims: Mapping[int, float],
        probability_space: bool = False,
        n_samples: int = 1000,
    ) -> Tuple[float, torch.Tensor]:
        """Query the model inverse.
        Return nearest x such that f(x) = queried y, and also return the
            value of f at that point.
        Args:
            y (float): Points at which to find the inverse.
            locked_dims (Mapping[int, float]): Dimensions to fix, so that the
                inverse is along a slice of the full surface.
            probability_space (bool, optional): Is y (and therefore the
                returned nearest_y) in probability space instead of latent
                function space? Defaults to False.
        Returns:
            Tuple[float, np.ndarray]: Tuple containing the value of f
                nearest to queried y and the x position of this value.
        """

        def model_distance(x, pt, probability_space):
            return np.abs(
                self.predict(torch.tensor([x]), probability_space)[0].numpy() - pt
            )

        # Look for point with value closest to y, subject the dict of locked dims

        query_lb = self.lb.clone()
        query_ub = self.ub.clone()

        for locked_dim in locked_dims.keys():
            query_lb[locked_dim] = locked_dims[locked_dim]
            query_ub[locked_dim] = locked_dims[locked_dim]

        d = make_scaled_sobol(query_lb, query_ub, n_samples, seed=0)

        bounds = zip(query_lb.numpy(), query_ub.numpy())

        fmean, _ = self.predict(d, probability_space)

        f = torch.abs(fmean - y)
        estimate = d[torch.where(f == torch.min(f))[0][0]].numpy()
        a = minimize(
            model_distance,
            estimate,
            args=(y, probability_space),
            method="Powell",
            bounds=bounds,
        )
        val = self.predict(torch.tensor([a.x]), probability_space)[0].item()
        return val, torch.Tensor(a.x)

    def get_jnd(
        self,
        grid: Optional[Union[np.ndarray, torch.Tensor]] = None,
        cred_level: float = None,
        intensity_dim: int = -1,
        confsamps: int = 500,
        method: str = "step",
    ) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor, torch.Tensor]]:
        """Calculate the JND.

        Note that JND can have multiple plausible definitions
        outside of the linear case, so we provide options for how to compute it.
        For method="step", we report how far one needs to go over in stimulus
        space to move 1 unit up in latent space (this is a lot of people's
        conventional understanding of the JND).
        For method="taylor", we report the local derivative, which also maps to a
        1st-order Taylor expansion of the latent function. This is a formal
        generalization of JND as defined in Weber's law.
        Both definitions are equivalent for linear psychometric functions.

        Args:
            grid (Optional[np.ndarray], optional): Mesh grid over which to find the JND.
                Defaults to a square grid of size as determined by aepsych.utils.dim_grid
            cred_level (float, optional): Credible level for computing an interval.
                Defaults to None, computing no interval.
            intensity_dim (int, optional): Dimension over which to compute the JND.
                Defaults to -1.
            confsamps (int, optional): Number of posterior samples to use for
                computing the credible interval. Defaults to 500.
            method (str, optional): "taylor" or "step" method (see docstring).
                Defaults to "step".

        Raises:
            RuntimeError: for passing an unknown method.

        Returns:
            Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor, torch.Tensor]]: either the
                mean JND, or a median, lower, upper tuple of the JND posterior.
        """
        if grid is None:
            grid = self.dim_grid()

        # this is super awkward, back into intensity dim grid assuming a square grid
        gridsize = int(grid.shape[0] ** (1 / grid.shape[1]))
        coords = torch.linspace(
            self.lb[intensity_dim], self.ub[intensity_dim], gridsize
        )

        if cred_level is None:
            fmean, _ = self.predict(grid)
            fmean = fmean.reshape(*[gridsize for i in range(self.dim)])

            if method == "taylor":
                return torch.tensor(1 / np.gradient(fmean, coords, axis=intensity_dim))
            elif method == "step":
                return torch.clip(
                    torch.tensor(
                        get_jnd_multid(
                            fmean.detach().numpy(),
                            coords.detach().numpy(),
                            mono_dim=intensity_dim,
                        )
                    ),
                    0,
                    np.inf,
                )

        alpha = 1 - cred_level  # type: ignore
        qlower = alpha / 2
        qupper = 1 - alpha / 2

        fsamps = self.sample(grid, confsamps)
        if method == "taylor":
            jnds = torch.tensor(
                1
                / np.gradient(
                    fsamps.reshape(confsamps, *[gridsize for i in range(self.dim)]),
                    coords,
                    axis=intensity_dim,
                )
            )
        elif method == "step":
            samps = [s.reshape((gridsize,) * self.dim) for s in fsamps]
            jnds = torch.stack(
                [get_jnd_multid(s, coords, mono_dim=intensity_dim) for s in samps]
            )
        else:
            raise RuntimeError(f"Unknown method {method}!")
        upper = torch.clip(torch.quantile(jnds, qupper, axis=0), 0, np.inf)  # type: ignore
        lower = torch.clip(torch.quantile(jnds, qlower, axis=0), 0, np.inf)  # type: ignore
        median = torch.clip(torch.quantile(jnds, 0.5, axis=0), 0, np.inf)  # type: ignore
        return median, lower, upper

    def dim_grid(self, gridsize: int = 30):
        return dim_grid(self.lb, self.ub, self.dim, gridsize)

    def set_train_data(self, x: torch.Tensor, y: torch.Tensor) -> None:
        """Set the training data for the model

        Args:
            x (torch.Tensor): training X points
            y ([type]): Training y points
        """
        self.train_inputs = (x,)
        self.train_targets = y

    @classmethod
    @abc.abstractmethod
    def from_config(cls, config: Config) -> AEPsychModel:
        pass
