from typing import Dict, Optional, List

import carla

from carla.recourse_methods.api import RecourseMethod

import torch

import numpy as np


from carla import log

from tqdm import tqdm

import pandas as pd

from carla.recourse_methods.processing import (
    check_counterfactuals,
    merge_default_parameters,
)

from carla.recourse_methods.processing import reconstruct_encoding_constraints



class ARAR(RecourseMethod):
    """
    Implementation of ARAR from Dominguez-Olmedo et. al. [1]_.

    Parameters
    ----------
    mlmodel : carla.models.MLModel
        Black-Box-Model
    hyperparams : dict
        Dictionary containing hyperparameters. See notes below for its contents.

    Methods
    -------
    get_counterfactuals:
        Generate counterfactual examples for given factuals.

    Notes
    -----
    - hyperparams
        Hyperparameter contains important information for the recourse method to initialize.
        Please make sure to pass all values as dict with the following keys.

        * "lr": float, learning rate of Adam
        * "lambd_init": float, initial lambda regulating BCE loss and L2 loss
        * "decay_rate": float < 1, at each outer iteration lambda is decreased by a factor of "decay_rate"
        * "inner_iters": int, number of inner optimization steps (for a fixed lambda)
        * "outer_iters": int, number of outer optimization steps (where lambda is decreased)
        * "inner_max_pgd": bool, whether to use PGD or a first order approximation (FGSM) to solve the inner max
        * "early_stop": bool, whether to do early stopping for the inner iterations
        * "binary_cat_features": bool, default: True
        * "y_target": [0,1], the target class.
        * "epsilon": float, amount of uncertainty, maximum perturbation magnitude (2-norm)
            If true, the encoding of x is done by drop_if_binary.

    """

    _DEFAULT_HYPERPARAMS = {
        'lr': 0.1, 
        'lambd_init': 1.0, 
        'decay_rate': 0.9, 
        'outer_iters': 100, 
        'inner_iters': 50, 
        'recourse_lr': 0.1,
        'inner_max_pgd': False,
        'early_stop': False,
        'binary_cat_features': True,
        'y_target': 1,
        'epsilon': 0.05
    }


    def __init__(self, mlmodel: carla.models.MLModel, hyperparams: Optional[Dict] = None):
        supported_backends = ["pytorch"]
        if mlmodel.backend not in supported_backends:
            raise ValueError(
                f"{mlmodel.backend} is not in supported backends {supported_backends}"
            )
        super().__init__(mlmodel)

        self.device = "cuda" if torch.cuda.is_available() else "cpu" # TODO fix CUDA


        checked_hyperparams = merge_default_parameters(
            hyperparams, self._DEFAULT_HYPERPARAMS
        )
        
        self._lr = checked_hyperparams["lr"]
        self._lambd_init = checked_hyperparams["lambd_init"]
        self._decay_rate = checked_hyperparams["decay_rate"]
        self._outer_iters = checked_hyperparams["outer_iters"]
        self._inner_iters = checked_hyperparams["inner_iters"]
        self._recourse_lr = checked_hyperparams["recourse_lr"]
        self._inner_max_pgd = checked_hyperparams["inner_max_pgd"]
        self._early_stop = checked_hyperparams["early_stop"]
        self._bce_loss = torch.nn.BCEWithLogitsLoss(reduction='none')
        self._binary_cat_features = checked_hyperparams["binary_cat_features"]
        self._y_target = checked_hyperparams["y_target"]
        self._epsilon = checked_hyperparams["epsilon"]
        
        if self._y_target not in [0,1]:
            raise ValueError(
                f"{self._y_target} is not a supported target class (0 or 1)"
            )

    def find_recourse(self, x, cat_feature_indices: List[int], interv_set = None, bounds = None, robust=False, scm=None, verbose=True):
        """
        Find a recourse action for some particular intervention set (implementation of Algorithm 1 in the paper)
        Inputs:     x: torch.Tensor with shape (N, D), negatively classified instances for which to generate recourse
                    cat_features_indices: List[int], List of positions of categorical features in x.
                    bounds: torch.Tensor with shape (N, D, 2), containing the min and max interventions
                    target: float, target label for the BCE loss (normally 1, that is, favourably classifier)
                    robust: bool, whether to guard against epsilon uncertainty
                    scm: type scm.SCM, structural causal model governing the causal relationships between features
                    interv_set: list of int, indices of the actionable features
                    verbose: bool
        Outputs:    actions: np.array with shape (N, D), recourse actions found
                    valid: np.array with shape (N, ), whether the corresponding recourse action is valid
                    cost: np.array with shape (N, ), cost of the recourse actions found (L1 norm)
                    cfs: np.array with shape (N, D), counterfactuals found (follow from x and actions)
        """
        D = x.shape[1]

        # print("x", x)

        # x_og = torch.Tensor(x, device=self.device)

        x_og = x.clone()


        x_pertb = torch.autograd.Variable(torch.zeros(x.shape, device = self.device), requires_grad=True)  # to calculate the adversarial
                                                                                     # intervention on the features
        ae_tol = 1e-4  # for early stopping
        actions = torch.zeros(x.shape, device=self.device)  # store here valid recourse found so far

        target_vec = torch.ones(x.shape[0], device=self.device) * self._y_target  # to feed into the BCE loss
        unfinished = torch.ones(x.shape[0], device=self.device)  # instances for which recourse was not found so far

        # Define variable for which to do gradient descent, which can be updated with optimizer
        delta = torch.autograd.Variable(torch.zeros(x.shape, device=self.device), requires_grad=True)
        optimizer = torch.optim.Adam([delta], self._lr)

        # Models the effect of the recourse intervention on the features
        def recourse_model(x, delta):
            if scm is None:
                return x + delta  # IMF
            else:
                if not interv_set:
                    raise ValueError("Intervention set may not be none if an SCM is used")
                return scm.counterfactual(x, delta, interv_set)  # counterfactual

        # Perturbation model is only used when generating robust recourse, models perturbations on the features
        def perturbation_model(x, pertb, delta):
            if scm is None:
                return recourse_model(x, delta) + pertb
            else:
                x_prime = scm.counterfactual(x, pertb, np.arange(D), [True] * D)
                return recourse_model(x_prime, delta)

        # Solve the first order approximation to the inner maximization problem
        def solve_first_order_approx(x_og, x_pertb, delta, target_vec):
            x_adv = perturbation_model(x_og, x_pertb, delta.detach())  # x_pertb is 0, only to backprop
            loss_x = torch.mean(self._bce_loss(self._mlmodel.predict(x_adv).squeeze(), target_vec))
            grad = torch.autograd.grad(loss_x, x_pertb, create_graph=False)[0]
            #sometime the grad is zero therefore it is not possible to normalize it
            sum = torch.sum(grad, dim=-1)
            grad[sum!=0] = grad [sum!=0]/ torch.linalg.norm(grad[sum!=0], dim=-1, keepdims=True) * self._epsilon
            return grad  # akin to FGSM attack



        lambd = self._lambd_init
        prev_batch_loss = np.inf  # for early stopping
        pbar = tqdm(range(self._outer_iters)) if verbose else range(self._outer_iters)
        for outer_iter in pbar:
            for inner_iter in range(self._inner_iters):
                optimizer.zero_grad()

                # Find the adversarial perturbation (first order approximation, as in the paper)
                if robust:
                    pertb = solve_first_order_approx(x_og, x_pertb, delta, target_vec)
                    if self._inner_max_pgd:
                        # Solve inner maximization with projected gradient descent
                        pertb = torch.autograd.Variable(pertb, requires_grad=True)
                        optimizer2 = torch.optim.SGD([pertb], lr=0.1)

                        for _ in range(10):
                            optimizer2.zero_grad()
                            loss_pertb = torch.mean(self._bce_loss(self._mlmodel.predict(x_og + pertb + delta.detach()).squeeze(),
                                                                #   torch.zeros(x.shape[0])))
                                                                  target_vec))
                            loss_pertb.backward()
                            optimizer2.step()

                            # Project to L2 ball, and with the linearity mask
                            with torch.no_grad():
                                norm = torch.linalg.norm(pertb, dim=-1)
                                too_large = norm > self._epsilon
                                pertb[too_large] = pertb[too_large] / norm[too_large, None] * self._epsilon
                            x_cf = x_og + pertb.detach() + delta

                            x_cf = reconstruct_encoding_constraints(x_cf, cat_feature_indices, self._binary_cat_features)
                    else:
                        x_cf = perturbation_model(x_og, pertb.detach(), delta)
                        x_cf = reconstruct_encoding_constraints(x_cf, cat_feature_indices, self._binary_cat_features)
                else:
                    x_cf = recourse_model(x_og, delta)
                    x_cf = reconstruct_encoding_constraints(x_cf, cat_feature_indices, self._binary_cat_features)

                with torch.no_grad():
                    # To continue optimazing, either the counterfactual or the adversarial counterfactual must be
                    # negatively classified
                    if self._y_target == 1:                                                                    # TODO our model returns floats in [0,1], so we check <= 0.5
                        pre_unfinished_1 = self._mlmodel.predict(recourse_model(x_og, delta.detach())) <= 0.5  # cf +1 # TODO if we want other target class we have to adapt this
                        pre_unfinished_2 = self._mlmodel.predict(x_cf) <= 0.5  # cf adversarial
                    elif self._y_target == 0:
                        pre_unfinished_1 = self._mlmodel.predict(recourse_model(x_og, delta.detach())) >= 0.5
                        pre_unfinished_2 = self._mlmodel.predict(x_cf) >= 0.5

                    pre_unfinished = torch.logical_or(pre_unfinished_1, pre_unfinished_2)
                    
                    # Add new solution to solutions
                    pre_unfinished = pre_unfinished.squeeze() # TODO added for compatability, since the our model predict returns shape=[50, 1] and theirs returned shape=[50]
                    new_solution = torch.logical_and(unfinished, torch.logical_not(pre_unfinished))
                    actions[new_solution] = torch.clone(delta[new_solution].detach())
                    unfinished = torch.logical_and(pre_unfinished, unfinished)

                # Compute loss
                clf_loss = self._bce_loss(self._mlmodel.predict(x_cf).squeeze(), target_vec) # TODO squeeze added for compatability
                l1_loss = torch.sum(torch.abs(delta), -1)
                loss = clf_loss + lambd * l1_loss

                # Apply mask over the ones where recourse has already been found
                loss_mask = unfinished.to(torch.float) * loss
                loss_mean = torch.mean(loss_mask)

                # Update x_cf
                loss_mean.backward()
                optimizer.step()

                # Satisfy the constraints on the features, by projecting delta
                if bounds:    
                    with torch.no_grad():
                        delta[:] = torch.min(torch.max(delta, bounds[..., 0]), bounds[..., 1])

                # For early stopping
                if self._early_stop and inner_iter % (self._inner_iters // 10) == 0:
                    if loss_mean > prev_batch_loss * (1 - ae_tol):
                        break
                    prev_batch_loss = loss_mean

            lambd *= self._decay_rate

            if verbose:
                pbar.set_description("Pct left: %.3f Lambda: %.4f" % (float(unfinished.sum()/x_cf.shape[0]), lambd))

            # Get out of the loop if recourse was found for every individual
            if not torch.any(unfinished):
                break

        valid = torch.logical_not(unfinished).detach().cpu().numpy()
        cfs = recourse_model(x_og, actions).detach().cpu().numpy()
        cost = torch.sum(torch.abs(actions), -1).detach().cpu().numpy()
        return actions.detach().cpu().numpy(), valid, cost, cfs


    def get_counterfactuals(self, factuals: pd.DataFrame) -> pd.DataFrame:
        # This property is responsible to generate and output
        # encoded and scaled (i.e. transformed) counterfactual examples
        # as pandas DataFrames.
        # Concretely this means that e.g. the counterfactuals should have
        # the same one-hot encoding as the factuals, and e.g. they both
        # should be min-max normalized with the same range.
        # It's expected that there is a single counterfactual per factual,
        # however in case a counterfactual cannot be found it should be NaN.
        factuals = self._mlmodel.get_ordered_features(factuals)
        
        encoded_feature_names = self._mlmodel.data.encoder.get_feature_names(
            self._mlmodel.data.categorical
        )
        cat_features_indices = [
            factuals.columns.get_loc(feature) for feature in encoded_feature_names
        ]


        # device = "cpu"

        x = torch.from_numpy(factuals.to_numpy().astype(np.float32)).to(self.device)


        # cfs = self.find_recourse(factuals, inverv_set, bounds) # TODO
        actions, valid, cost, cfs = self.find_recourse(x, cat_features_indices, robust=True)


        df_cfs = pd.DataFrame(cfs, columns=factuals.columns)


        # TODO we need to enforce the constrainsts, something like 
            # x_new_enc = reconstruct_encoding_constraints(
            #     x_new, cat_feature_indices, binary_cat_features
            # )
        # everywhere where we have x_cf

        pred = self._mlmodel.predict(cfs)
        negative_label = 0
        if self._y_target == 0:
            negative_label =1
        df_cfs = check_counterfactuals(self._mlmodel, df_cfs, factuals.index, negative_label=negative_label)


        df_cfs = self._mlmodel.get_ordered_features(df_cfs)

        return df_cfs