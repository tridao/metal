import copy
import json
import os
import random
from collections import defaultdict
from subprocess import check_output
from time import strftime

import numpy as np
import torch
from scipy.sparse import issparse


class Checkpointer(object):
    def __init__(
        self, model_class, checkpoint_min=-1, checkpoint_runway=0, verbose=True
    ):
        """Saves checkpoints as applicable based on a reported metric.

        Args:
            checkpoint_min (float): the initial "best" score to beat
            checkpoint_runway (int): don't save any checkpoints for the first
                this many iterations
        """
        self.model_class = model_class
        self.best_model = None
        self.best_iteration = None
        self.best_score = checkpoint_min
        self.checkpoint_runway = checkpoint_runway
        self.verbose = verbose
        if checkpoint_runway and verbose:
            print(
                f"No checkpoints will be saved in the first "
                f"checkpoint_runway={checkpoint_runway} iterations."
            )

    def checkpoint(self, model, iteration, score):
        if iteration >= self.checkpoint_runway:
            is_best = score > self.best_score
            if is_best:
                if self.verbose:
                    print(
                        f"Saving model at iteration {iteration} with best "
                        f"score {score:.3f}"
                    )
                self.best_model = copy.deepcopy(model.state_dict())
                self.best_iteration = iteration
                self.best_score = score

    def restore(self, model):
        if self.best_model is None:
            raise Exception(
                f"Best model was never found. Best score = "
                f"{self.best_score}"
            )
        if self.verbose:
            print(
                f"Restoring best model from iteration {self.best_iteration} "
                f"with score {self.best_score:.3f}"
            )
            model.load_state_dict(self.best_model)
            return model


def rargmax(x, eps=1e-8):
    """Argmax with random tie-breaking

    Args:
        x: a 1-dim numpy array
    Returns:
        the argmax index
    """
    idxs = np.where(abs(x - np.max(x, axis=0)) < eps)[0]
    return np.random.choice(idxs)


def hard_to_soft(Y_h, k):
    """Converts a 1D tensor of hard labels into a 2D tensor of soft labels

    Args:
        Y_h: an [n], or [n,1] tensor of hard (int) labels in {1,...,k}
        k: the largest possible label in Y_h
    Returns:
        Y_s: a torch.FloatTensor of shape [n, k] where Y_s[i, j-1] is the soft
            label for item i and label j
    """
    Y_h = Y_h.clone()
    Y_h = Y_h.squeeze()
    assert Y_h.dim() == 1
    assert (Y_h >= 1).all()
    assert (Y_h <= k).all()
    n = Y_h.shape[0]
    Y_s = torch.zeros((n, k), dtype=Y_h.dtype, device=Y_h.device)
    for i, j in enumerate(Y_h):
        Y_s[i, j - 1] = 1.0
    return Y_s


def arraylike_to_numpy(array_like):
    """Convert a 1d array-like (e.g,. list, tensor, etc.) to an np.ndarray"""

    orig_type = type(array_like)

    # Convert to np.ndarray
    if isinstance(array_like, np.ndarray):
        pass
    elif isinstance(array_like, list):
        array_like = np.array(array_like)
    elif issparse(array_like):
        array_like = array_like.toarray()
    elif isinstance(array_like, torch.Tensor):
        array_like = array_like.numpy()
    elif not isinstance(array_like, np.ndarray):
        array_like = np.array(array_like)
    else:
        msg = (
            f"Input of type {orig_type} could not be converted to 1d "
            "np.ndarray"
        )
        raise ValueError(msg)

    # Correct shape
    if (array_like.ndim > 1) and (1 in array_like.shape):
        array_like = array_like.flatten()
    if array_like.ndim != 1:
        raise ValueError("Input could not be converted to 1d np.array")

    # Convert to ints
    if any(array_like % 1):
        raise ValueError("Input contains at least one non-integer value.")
    array_like = array_like.astype(np.dtype(int))

    return array_like


def convert_labels(Y, source, dest):
    """Convert a matrix from one label type to another

    Args:
        X: A np.ndarray or torch.Tensor of labels (ints)
        source: The convention the labels are currently expressed in
        dest: The convention to convert the labels to

    Conventions:
        'categorical': [0: abstain, 1: positive, 2: negative]
        'plusminus': [0: abstain, 1: positive, -1: negative]
        'onezero': [0: negative, 1: positive]

    Note that converting to 'onezero' will combine abstain and negative labels.
    """
    if Y is None:
        return Y
    if isinstance(Y, np.ndarray):
        Y = Y.copy()
    elif isinstance(Y, torch.Tensor):
        Y = Y.clone()
    else:
        raise ValueError("Unrecognized label data type.")
    negative_map = {"categorical": 2, "plusminus": -1, "onezero": 0}
    Y[Y == negative_map[source]] = negative_map[dest]
    return Y


def plusminus_to_categorical(Y):
    return convert_labels(Y, "plusminus", "categorical")


def categorical_to_plusminus(Y):
    return convert_labels(Y, "categorical", "plusminus")


def recursive_merge_dicts(x, y, misses="report", verbose=None):
    """
    Merge dictionary y into a copy of x, overwriting elements of x when there
    is a conflict, except if the element is a dictionary, in which case recurse.

    misses: what to do if a key in y is not in x
        'insert'    -> set x[key] = value
        'exception' -> raise an exception
        'report'    -> report the name of the missing key
        'ignore'    -> do nothing

    TODO: give example here (pull from tests)
    """

    def recurse(x, y, misses="report", verbose=1):
        found = True
        for k, v in y.items():
            found = False
            if k in x:
                found = True
                if isinstance(x[k], dict):
                    if not isinstance(v, dict):
                        msg = (
                            f"Attempted to overwrite dict {k} with "
                            f"non-dict: {v}"
                        )
                        raise ValueError(msg)
                    recurse(x[k], v, misses, verbose)
                else:
                    if x[k] == v:
                        msg = f"Reaffirming {k}={x[k]}"
                    else:
                        msg = f"Overwriting {k}={x[k]} to {k}={v}"
                        x[k] = v
                    if verbose > 1 and k != "verbose":
                        print(msg)
            else:
                for kx, vx in x.items():
                    if isinstance(vx, dict):
                        found = recurse(
                            vx, {k: v}, misses="ignore", verbose=verbose
                        )
                    if found:
                        break
            if not found:
                msg = f'Could not find kwarg "{k}" in destination dict.'
                if misses == "insert":
                    x[k] = v
                    if verbose > 1:
                        print(f"Added {k}={v} from second dict to first")
                elif misses == "exception":
                    raise ValueError(msg)
                elif misses == "report":
                    print(msg)
                else:
                    pass
        return found

    # If verbose is not provided, look for an value in y first, then x
    # (Do this because 'verbose' kwarg is often inside one or both of x and y)
    if verbose is None:
        verbose = y.get("verbose", x.get("verbose", 1))

    z = copy.deepcopy(x)
    recurse(z, y, misses, verbose)
    return z


def split_data(
    *inputs,
    splits=[0.5, 0.5],
    shuffle=True,
    stratify_by=None,
    index_only=False,
    seed=None,
):
    """Splits inputs into multiple splits of defined sizes

    Args:
        inputs: correlated tuples/lists/arrays/matrices/tensors to split
        splits: list containing split sizes (fractions or counts);
        shuffle: if True, shuffle the data before splitting
        stratify_by: (None or an input) if not None, use these labels to
            stratify the splits (separating the data into groups by these
            labels and sampling from those, rather than from the population at
            large); overrides shuffle
        index_only: if True, return only the indices of the new splits, not the
            split data itself
        seed: (int) random seed

    Example usage:
        Ls, Xs, Ys = split_data(L, X, Y, splits=[0.8, 0.1, 0.1])
        OR
        assignments = split_data(Y, splits=[0.8, 0.1, 0.1], index_only=True)

    Note: This is very similar to scikit-learn's train_test_split() method,
        but with support for more than two splits.
    """

    def fractions_to_counts(fracs, n):
        """Converts a list of fractions to a list of counts that sum to n"""
        counts = [int(np.round(n * frac)) for frac in fracs]
        # Ensure sum of split counts sums to n
        counts[-1] = n - sum(counts[:-1])
        return counts

    def slice_data(data, indices):
        if isinstance(data, list) or isinstance(data, tuple):
            return [d for i, d in enumerate(data) if i in set(indices)]
        else:
            try:
                # Works for np.ndarray, scipy.sparse, torch.Tensor
                return data[indices]
            except TypeError:
                raise Exception(
                    f"split_data() currently only accepts inputs "
                    f"of type tuple, list, np.ndarray, scipy.sparse, or "
                    f"torch.Tensor; not {type(data)}"
                )

    # Setting random seed
    if seed is not None:
        random.seed(seed)

    try:
        n = len(inputs[0])
    except TypeError:
        n = inputs[0].shape[0]
    num_splits = len(splits)

    # Check splits for validity and convert to fractions
    if all(isinstance(x, int) for x in splits):
        if not sum(splits) == n:
            raise ValueError(
                f"Provided split counts must sum to n ({n}), not {sum(splits)}."
            )
        fracs = [count / n for count in splits]

    elif all(isinstance(x, float) for x in splits):
        if not sum(splits) == 1.0:
            raise ValueError(
                f"Split fractions must sum to 1.0, not {sum(splits)}."
            )
        fracs = splits

    else:
        raise ValueError("Splits must contain all ints or all floats.")

    # Make sampling pools
    if stratify_by is None:
        pools = [np.arange(n)]
    else:
        pools = defaultdict(list)
        for i, val in enumerate(stratify_by):
            pools[val].append(i)
        pools = list(pools.values())

    # Make index assignments
    assignments = [[] for _ in range(num_splits)]
    for pool in pools:
        if shuffle or stratify_by is not None:
            random.shuffle(pool)

        counts = fractions_to_counts(fracs, len(pool))
        counts.insert(0, 0)
        cum_counts = np.cumsum(counts)
        for i in range(num_splits):
            assignments[i].extend(pool[cum_counts[i] : cum_counts[i + 1]])

    if index_only:
        return assignments
    else:
        outputs = []
        for data in inputs:
            data_splits = []
            for split in range(num_splits):
                data_splits.append(slice_data(data, assignments[split]))
            outputs.append(data_splits)

        if len(outputs) == 1:
            return outputs[0]
        else:
            return outputs


def place_on_gpu(data):
    """Utility to place data on GPU, where data could be a torch.Tensor, a tuple
    or list of Tensors, or a tuple or list of tuple or lists of Tensors"""
    if isinstance(data, (list, tuple)):
        for i in range(len(data)):
            data[i] = place_on_gpu(data[i])
        return data
    elif isinstance(data, torch.Tensor):
        return data.cuda()
    else:
        return ValueError(f"Data type {type(data)} not recognized.")


#
# LOGGING
#
class LogWriter(object):
    """Class for writing simple JSON logs at end of runs, with interface for
    storing per-iter data as well.

    Args:
        log_dir: (str) The path to the base log directory, or defaults to
            current working directory.
        run_dir: (str) The name of the sub-directory, or defaults to the date,
            strftime("%Y_%m_%d").
        run_name: (str) The name of the run, or defaults to the time,
            strftime("%H_%M_%S).

        Log is saved to 'log_dir/run_dir/{run_name}.json'
    """

    def __init__(self, log_dir=None, run_dir=None, run_name=None):
        start_date = strftime("%Y_%m_%d")
        start_time = strftime("%H_%M_%S")

        # Set logging subdirectory + make sure exists
        log_dir = log_dir or os.getcwd()
        run_dir = run_dir or start_date
        self.log_subdir = os.path.join(log_dir, run_dir)
        if not os.path.exists(self.log_subdir):
            os.makedirs(self.log_subdir)

        # Set JSON log path
        run_name = run_name or start_time
        self.log_path = os.path.join(self.log_subdir, f"{run_name}.json")

        # Initialize log
        # Note we have a separate section for during-run metrics
        commit = check_output(["git", "rev-parse", "--short", "HEAD"]).strip()
        self.log = {
            "start-date": start_date,
            "start-time": start_time,
            "commit": str(commit),
            "config": None,
            "run-log": defaultdict(list),
        }

    def add_config(self, config):
        self.log["config"] = config

    def add_scalar(self, name, val, i):
        # Note: Does not handle deduplication of (name, val) entries w same i
        self.log["run-log"][name].append((i, val))

    def write(self):
        """Dump JSON to file"""
        with open(self.log_path, "w") as f:
            json.dump(self.log, f, indent=1)

    def close(self):
        self.write()
