import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

import tensorflow.keras as keras
from tensorflow.keras.layers import *
from tensorflow.keras.models import Model
from tensorflow.keras.losses import binary_crossentropy
from tensorflow.keras import backend as K
from tensorflow.keras.callbacks import ReduceLROnPlateau, Callback


class LossHistory(Callback):
    """This class is used for saving the validation loss and the training loss per epoch."""

    def on_train_begin(self, logs={}):
        """Initialise the lists where the loss of training and validation will be saved."""
        self.losses = []
        self.val_losses = []

    def on_epoch_end(self, epoch, logs={}):
        """Save the loss of training and validation set at the end of each epoch."""
        self.losses.append(logs.get("loss"))
        self.val_losses.append(logs.get("val_loss"))


class Metrics(Callback):
    """Callback function used to calculate the NDCG@k metric of validation set at the end of each epoch.
    Weights of the model with the highest NDCG@k value is saved."""

    def __init__(self, model, val_tr, val_te, mapper, k, save_path=None):

        """Initialize the class parameters.
        Args:
            model: trained model for validation.
            val_tr (numpy.ndarray, float): the click matrix for the validation set training part.
            val_te (numpy.ndarray, float): the click matrix for the validation set testing part.
            mapper (AffinityMatrix): the mapper for converting click matrix to dataframe.
            k (int): number of top k items per user (optional).
            save_path (str): Default path to save weights.
        """
        # Model
        self.model = model

        # Initial value of NDCG
        self.best_ndcg = 0.0

        # Validation data: training and testing parts
        self.val_tr = val_tr
        self.val_te = val_te

        # Mapper for converting from sparse matrix to dataframe
        self.mapper = mapper

        # Top k items to recommend
        self.k = k

        # Options to save the weights of the model for future use
        self.save_path = save_path

    def on_train_begin(self, logs={}):
        """Initialise the list for validation NDCG@k."""
        self._data = []

    def recommend_k_items(self, x, k, remove_seen=True):
        """Returns the top-k items ordered by a relevancy score.
        Obtained probabilities are used as recommendation score.
        Args:
            x (numpy.ndarray, int32): input click matrix.
            k (scalar, int32): the number of items to recommend.
        Returns:
            numpy.ndarray: A sparse matrix containing the top_k elements ordered by their score.
        """
        # obtain scores
        score = self.model.predict(x)

        if remove_seen:
            # if true, it removes items from the train set by setting them to zero
            seen_mask = np.not_equal(x, 0)
            score[seen_mask] = 0

        # get the top k items
        top_items = np.argpartition(-score, range(k), axis=1)[:, :k]

        # get a copy of the score matrix
        score_c = score.copy()

        # set to zero the k elements
        score_c[np.arange(score_c.shape[0])[:, None], top_items] = 0

        # set to zeros all elements other then the k
        top_scores = score - score_c

        return top_scores

    def on_epoch_end(self, batch, logs={}):
        """At the end of each epoch calculate NDCG@k of the validation set.
        If the model performance is improved, the model weights are saved.
        Update the list of validation NDCG@k by adding obtained value.
        """
        # recommend top k items based on training part of validation set
        top_k = self.recommend_k_items(x=self.val_tr, k=self.k, remove_seen=True)

        # convert recommendations from sparse matrix to dataframe
        top_k_df = self.mapper.map_back_sparse(top_k, kind="prediction")
        test_df = self.mapper.map_back_sparse(self.val_te, kind="ratings")

        # calculate NDCG@k
        NDCG = ndcg_at_k(test_df, top_k_df, col_prediction="prediction", k=self.k)

        # check if there is an improvement in NDCG, if so, update the weights of the saved model
        if NDCG > self.best_ndcg:
            self.best_ndcg = NDCG

            # save the weights of the optimal model
            if self.save_path is not None:
                self.model.save(self.save_path)

        self._data.append(NDCG)

    def get_data(self):
        """Returns a list of the NDCG@k of the validation set metrics calculated
        at the end of each epoch."""
        return self._data


class StandardVAE:
    """Standard Variational Autoencoders (VAE) for Collaborative Filtering implementation."""

    def __init__(
        self,
        n_users,
        original_dim,
        intermediate_dim=200,
        latent_dim=70,
        n_epochs=400,
        batch_size=100,
        k=100,
        verbose=1,
        drop_encoder=0.5,
        drop_decoder=0.5,
        beta=1.0,
        annealing=False,
        anneal_cap=1.0,
        seed=None,
        save_path=None,
    ):

        """Initialize class parameters.
        Args:
            n_users (int): Number of unique users in the train set.
            original_dim (int): Number of unique items in the train set.
            intermediate_dim (int): Dimension of intermediate space.
            latent_dim (int): Dimension of latent space.
            n_epochs (int): Number of epochs for training.
            batch_size (int): Batch size.
            k (int): number of top k items per user.
            verbose (int): Whether to show the training output or not.
            drop_encoder (float): Dropout percentage of the encoder.
            drop_decoder (float): Dropout percentage of the decoder.
            beta (float): a constant parameter β in the ELBO function,
                  when you are not using annealing (annealing=False)
            annealing (bool): option of using annealing method for training the model (True)
                  or not using annealing, keeping a constant beta (False)
            anneal_cap (float): maximum value that beta can take during annealing process.
            seed (int): Seed.
            save_path (str): Default path to save weights.
        """
        # Seed
        self.seed = seed
        np.random.seed(self.seed)

        # Parameters
        self.n_users = n_users
        self.original_dim = original_dim
        self.intermediate_dim = intermediate_dim
        self.latent_dim = latent_dim
        self.n_epochs = n_epochs
        self.batch_size = batch_size
        self.k = k
        self.verbose = verbose

        # Compute samples per epoch
        self.number_of_batches = self.n_users // self.batch_size

        # Annealing parameters
        self.anneal_cap = anneal_cap
        self.annealing = annealing

        if self.annealing == True:
            self.beta = K.variable(0.0)
        else:
            self.beta = beta

        # Compute total annealing steps
        self.total_anneal_steps = (
            self.number_of_batches * (self.n_epochs - int(self.n_epochs * 0.2))
        ) // self.anneal_cap

        # Dropout parameters
        self.drop_encoder = drop_encoder
        self.drop_decoder = drop_decoder

        # Path to save optimal model
        self.save_path = save_path

        # Create StandardVAE model
        self._create_model()

    def _create_model(self):
        """Build and compile model."""
        # Encoding
        self.x = Input(shape=(self.original_dim,))
        self.dropout_encoder = Dropout(self.drop_encoder)(self.x)
        self.h = Dense(self.intermediate_dim, activation="tanh")(self.dropout_encoder)
        self.z_mean = Dense(self.latent_dim)(self.h)
        self.z_log_var = Dense(self.latent_dim)(self.h)

        # Sampling
        self.z = Lambda(self._take_sample, output_shape=(self.latent_dim,))(
            [self.z_mean, self.z_log_var]
        )

        # Decoding
        self.h_decoder = Dense(self.intermediate_dim, activation="tanh")
        self.dropout_decoder = Dropout(self.drop_decoder)
        self.x_bar = Dense(self.original_dim, activation="softmax")
        self.h_decoded = self.h_decoder(self.z)
        self.h_decoded_ = self.dropout_decoder(self.h_decoded)
        self.x_decoded = self.x_bar(self.h_decoded_)

        # Training
        self.model = Model(self.x, self.x_decoded)
        self.model.compile(
            optimizer=keras.optimizers.Adam(learning_rate=0.001),
            loss=self._get_vae_loss,
        )

    def _get_vae_loss(self, x, x_bar):
        """Calculate negative ELBO (NELBO)."""
        # Reconstruction error: logistic log likelihood
        reconst_loss = self.original_dim * binary_crossentropy(x, x_bar)

        # Kullback–Leibler divergence
        kl_loss = 0.5 * K.sum(
            -1 - self.z_log_var + K.square(self.z_mean) + K.exp(self.z_log_var), axis=-1
        )

        return reconst_loss + self.beta * kl_loss

    def _take_sample(self, args):
        """Sample epsilon ∼ N (0,I) and compute z via reparametrization trick."""
        """Calculate latent vector using the reparametrization trick.
           The idea is that sampling from N (_mean, _var) is s the same as sampling from _mean+ epsilon * _var
           where epsilon ∼ N(0,I)."""
        # sampling from latent dimension for decoder/generative part of network
        _mean, _log_var = args
        epsilon = K.random_normal(
            shape=(K.shape(_mean)[0], self.latent_dim),
            mean=0.0,
            stddev=1.0,
            seed=self.seed,
        )

        return _mean + K.exp(_log_var / 2) * epsilon

    def nn_batch_generator(self, x_train):
        """Used for splitting dataset in batches.
        Args:
            x_train (numpy.ndarray): The click matrix for the train set with float values.
        """
        # Shuffle the batch
        np.random.seed(self.seed)
        shuffle_index = np.arange(np.shape(x_train)[0])
        np.random.shuffle(shuffle_index)
        x = x_train[shuffle_index, :]
        y = x_train[shuffle_index, :]

        # Iterate until making a full epoch
        counter = 0
        while 1:
            index_batch = shuffle_index[
                self.batch_size * counter : self.batch_size * (counter + 1)
            ]
            # Decompress batch
            x_batch = x[index_batch, :]
            y_batch = y[index_batch, :]
            counter += 1
            yield (np.array(x_batch), np.array(y_batch))

            # Stopping rule
            if counter >= self.number_of_batches:
                counter = 0

    def fit(self, x_train, x_valid, x_val_tr, x_val_te, mapper):
        """Fit model with the train sets and validate on the validation set.
        Args:
            x_train (numpy.ndarray): The click matrix for the train set.
            x_valid (numpy.ndarray): The click matrix for the validation set.
            x_val_tr (numpy.ndarray): The click matrix for the validation set training part.
            x_val_te (numpy.ndarray): The click matrix for the validation set testing part.
            mapper (object): The mapper for converting click matrix to dataframe. It can be AffinityMatrix.
        """
        # initialise LossHistory used for saving loss of validation and train set per epoch
        history = LossHistory()

        # initialise Metrics  used for calculating NDCG@k per epoch
        # and saving the model weights with the highest NDCG@k value
        metrics = Metrics(
            model=self.model,
            val_tr=x_val_tr,
            val_te=x_val_te,
            mapper=mapper,
            k=self.k,
            save_path=self.save_path,
        )

        self.reduce_lr = ReduceLROnPlateau(
            monitor="val_loss", factor=0.2, patience=1, min_lr=0.0001
        )

        if self.annealing == True:
            print('annealing is not supported. Please set to False')
            return

        else:
            self.model.fit_generator(
                generator=self.nn_batch_generator(x_train),
                steps_per_epoch=self.number_of_batches,
                epochs=self.n_epochs,
                verbose=self.verbose,
                callbacks=[metrics, history, self.reduce_lr],
                validation_data=(x_valid, x_valid),
            )

        # save lists
        self.train_loss = history.losses
        self.val_loss = history.val_losses
        self.val_ndcg = metrics.get_data()

    def get_optimal_beta(self):
        """Returns the value of the optimal beta."""
        # find the epoch/index that had the highest NDCG@k value
        index_max_ndcg = np.argmax(self.val_ndcg)

        # using this index find the value that beta had at this epoch
        optimal_beta = self.ls_beta[index_max_ndcg]

        return optimal_beta

    def display_metrics(self):
        """Plots:
        1) Loss per epoch both for validation and train sets
        2) NDCG@k per epoch of the validation set
        """
        # Plot setup
        plt.figure(figsize=(14, 5))
        sns.set(style="whitegrid")

        # Plot loss on the left graph
        plt.subplot(1, 2, 1)
        plt.plot(self.train_loss, color="b", linestyle="-", label="Train")
        plt.plot(self.val_loss, color="r", linestyle="-", label="Val")
        plt.title("\n")
        plt.xlabel("Epochs", size=14)
        plt.ylabel("Loss", size=14)
        plt.legend(loc="upper left")

        # Plot NDCG on the right graph
        plt.subplot(1, 2, 2)
        plt.plot(self.val_ndcg, color="r", linestyle="-", label="Val")
        plt.title("\n")
        plt.xlabel("Epochs", size=14)
        plt.ylabel("NDCG@k", size=14)
        plt.legend(loc="upper left")

        # Add title
        plt.suptitle("TRAINING AND VALIDATION METRICS HISTORY", size=16)
        plt.tight_layout(pad=2)

    def recommend_k_items(self, x, k, remove_seen=True):
        """Returns the top-k items ordered by a relevancy score.
        Obtained probabilities are used as recommendation score.
        Args:
            x (numpy.ndarray): Input click matrix, with `int32` values.
            k (scalar): The number of items to recommend.
        Returns:
            numpy.ndarray: A sparse matrix containing the top_k elements ordered by their score.
        """

        # obtain scores
        score = self.model.predict(x)
        if remove_seen:
            # if true, it removes items from the train set by setting them to zero
            seen_mask = np.not_equal(x, 0)
            score[seen_mask] = 0
        # get the top k items
        top_items = np.argpartition(-score, range(k), axis=1)[:, :k]
        # get a copy of the score matrix
        score_c = score.copy()
        # set to zero the k elements
        score_c[np.arange(score_c.shape[0])[:, None], top_items] = 0
        # set to zeros all elements other then the k
        top_scores = score - score_c
        return top_scores

    def ndcg_per_epoch(self):
        """Returns the list of NDCG@k at each epoch."""

        return self.val_ndcg

DEFAULT_USER_COL = 'userId'
DEFAULT_ITEM_COL = 'movieId'
DEFAULT_RATING_COL = "rating"
DEFAULT_PREDICTION_COL = "prediction"
DEFAULT_K = 10
DEFAULT_THRESHOLD = 10

def ndcg_at_k(
        rating_true,
        rating_pred,
        col_user=DEFAULT_USER_COL,
        col_item=DEFAULT_ITEM_COL,
        col_rating=DEFAULT_RATING_COL,
        col_prediction=DEFAULT_PREDICTION_COL,
        relevancy_method="top_k",
        k=DEFAULT_K,
        threshold=DEFAULT_THRESHOLD,
):
    """Normalized Discounted Cumulative Gain (nDCG).

    Info: https://en.wikipedia.org/wiki/Discounted_cumulative_gain

    Args:
        rating_true (pandas.DataFrame): True DataFrame
        rating_pred (pandas.DataFrame): Predicted DataFrame
        col_user (str): column name for user
        col_item (str): column name for item
        col_rating (str): column name for rating
        col_prediction (str): column name for prediction
        relevancy_method (str): method for determining relevancy ['top_k', 'by_threshold', None]. None means that the
            top k items are directly provided, so there is no need to compute the relevancy operation.
        k (int): number of top k items per user
        threshold (float): threshold of top items per user (optional)
    Returns:
        float: nDCG at k (min=0, max=1).
    """

    df_hit, df_hit_count, n_users = merge_ranking_true_pred(
        rating_true=rating_true,
        rating_pred=rating_pred,
        col_user=col_user,
        col_item=col_item,
        col_rating=col_rating,
        col_prediction=col_prediction,
        relevancy_method=relevancy_method,
        k=k,
        threshold=threshold,
    )

    if df_hit.shape[0] == 0:
        return 0.0

    # calculate discounted gain for hit items
    df_dcg = df_hit.copy()
    # relevance in this case is always 1
    df_dcg["dcg"] = 1 / np.log1p(df_dcg["rank"])
    # sum up discount gained to get discount cumulative gain
    df_dcg = df_dcg.groupby(col_user, as_index=False, sort=False).agg({"dcg": "sum"})
    # calculate ideal discounted cumulative gain
    df_ndcg = pd.merge(df_dcg, df_hit_count, on=[col_user])
    df_ndcg["idcg"] = df_ndcg["actual"].apply(
        lambda x: sum(1 / np.log1p(range(1, min(x, k) + 1)))
    )

    # DCG over IDCG is the normalized DCG
    return (df_ndcg["dcg"] / df_ndcg["idcg"]).sum() / n_users


def merge_ranking_true_pred(
    rating_true,
    rating_pred,
    col_user,
    col_item,
    col_rating,
    col_prediction,
    relevancy_method,
    k=DEFAULT_K,
    threshold=DEFAULT_THRESHOLD,
):
    """Filter truth and prediction data frames on common users
    Args:
        rating_true (pandas.DataFrame): True DataFrame
        rating_pred (pandas.DataFrame): Predicted DataFrame
        col_user (str): column name for user
        col_item (str): column name for item
        col_rating (str): column name for rating
        col_prediction (str): column name for prediction
        relevancy_method (str): method for determining relevancy ['top_k', 'by_threshold', None]. None means that the
            top k items are directly provided, so there is no need to compute the relevancy operation.
        k (int): number of top k items per user (optional)
        threshold (float): threshold of top items per user (optional)
    Returns:
        pandas.DataFrame, pandas.DataFrame, int: DataFrame of recommendation hits, sorted by `col_user` and `rank`
        DataFrmae of hit counts vs actual relevant items per user number of unique user ids
    """

    # Make sure the prediction and true data frames have the same set of users
    common_users = set(rating_true[col_user]).intersection(set(rating_pred[col_user]))
    rating_true_common = rating_true[rating_true[col_user].isin(common_users)]
    rating_pred_common = rating_pred[rating_pred[col_user].isin(common_users)]
    n_users = len(common_users)

    # Return hit items in prediction data frame with ranking information. This is used for calculating NDCG and MAP.
    # Use first to generate unique ranking values for each item. This is to align with the implementation in
    # Spark evaluation metrics, where index of each recommended items (the indices are unique to items) is used
    # to calculate penalized precision of the ordered items.
    if relevancy_method == "top_k":
        top_k = k
    elif relevancy_method == "by_threshold":
        top_k = threshold
    elif relevancy_method is None:
        top_k = None
    else:
        raise NotImplementedError("Invalid relevancy_method")
    df_hit = get_top_k_items(
        dataframe=rating_pred_common,
        col_user=col_user,
        col_rating=col_prediction,
        k=top_k,
    )
    df_hit = pd.merge(df_hit, rating_true_common, on=[col_user, col_item])[
        [col_user, col_item, "rank"]
    ]

    # count the number of hits vs actual relevant items per user
    df_hit_count = pd.merge(
        df_hit.groupby(col_user, as_index=False)[col_user].agg({"hit": "count"}),
        rating_true_common.groupby(col_user, as_index=False)[col_user].agg(
            {"actual": "count"}
        ),
        on=col_user,
    )

    return df_hit, df_hit_count, n_users


def get_top_k_items(
        dataframe, col_user=DEFAULT_USER_COL, col_rating=DEFAULT_RATING_COL, k=DEFAULT_K
):
    """Get the input customer-item-rating tuple in the format of Pandas
    DataFrame, output a Pandas DataFrame in the dense format of top k items
    for each user.

    Note:
        If it is implicit rating, just append a column of constants to be
        ratings.
    Args:
        dataframe (pandas.DataFrame): DataFrame of rating data (in the format
        customerID-itemID-rating)
        col_user (str): column name for user
        col_rating (str): column name for rating
        k (int or None): number of items for each user; None means that the input has already been
        filtered out top k items and sorted by ratings and there is no need to do that again.
    Returns:
        pandas.DataFrame: DataFrame of top k items for each user, sorted by `col_user` and `rank`
    """
    # Sort dataframe by col_user and (top k) col_rating
    if k is None:
        top_k_items = dataframe
    else:
        top_k_items = (
            dataframe.groupby(col_user, as_index=False)
                .apply(lambda x: x.nlargest(k, col_rating))
                .reset_index(drop=True)
        )
    # Add ranks
    top_k_items["rank"] = top_k_items.groupby(col_user, sort=False).cumcount() + 1
    return top_k_items
