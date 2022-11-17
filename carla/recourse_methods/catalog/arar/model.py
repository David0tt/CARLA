from carla.recourse_methods.api import RecourseMethod

from carla.recourse_methods.processing import (
    check_counterfactuals,
    merge_default_parameters,
)


# Custom recourse implementations need to
# inherit from the RecourseMethod interface
class MyRecourseMethod(RecourseMethod):
    def __init__(self, mlmodel, hyperparameters):
        super().__init__(mlmodel)
        # the constructor can be used to load the recourse method,
        # or construct everything necessary

    # Generate and return encoded and
    # scaled counterfactual examples
    def get_counterfactuals(self, factuals: pd.DataFrame):
        # This property is responsible to generate and output
        # encoded and scaled (i.e. transformed) counterfactual examples
        # as pandas DataFrames.
        # Concretely this means that e.g. the counterfactuals should have
        # the same one-hot encoding as the factuals, and e.g. they both
        # should be min-max normalized with the same range.
        # It's expected that there is a single counterfactual per factual,
        # however in case a counterfactual cannot be found it should be NaN.
            [...]
        return counterfactual_examples