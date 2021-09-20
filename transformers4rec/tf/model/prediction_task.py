import logging
from types import SimpleNamespace
from typing import Dict, List, Optional, Text, Type, Union

import tensorflow as tf
from tensorflow.keras.layers import Layer
from tensorflow.python.keras.utils import generic_utils
from transformers.modeling_tf_utils import TFSequenceSummary

from ..block.mlp import MLPBlock
from ..ranking_metric import AvgPrecisionAt, NDCGAt, RecallAt
from ..typing import Head, Model
from ..utils.tf_utils import (
    LossMixin,
    MetricsMixin,
    maybe_deserialize_keras_objects,
    maybe_serialize_keras_objects,
)


def name_fn(name, inp):
    return "/".join([name, inp]) if name else None


MetricOrMetricClass = Union[tf.keras.metrics.Metric, Type[tf.keras.metrics.Metric]]


LOG = logging.getLogger("transformers4rec")


class PredictionTask(Layer, LossMixin, MetricsMixin):
    def __init__(
        self,
        loss: Optional[tf.keras.losses.Loss],
        target_name: Optional[str] = None,
        task_name: Optional[str] = None,
        metrics: Optional[List[MetricOrMetricClass]] = None,
        pre: Optional[Layer] = None,
        task_block: Optional[Layer] = None,
        prediction_metrics: Optional[List[tf.keras.metrics.Metric]] = None,
        label_metrics: Optional[List[tf.keras.metrics.Metric]] = None,
        loss_metrics: Optional[List[tf.keras.metrics.Metric]] = None,
        name: Optional[Text] = None,
        summary_type="last",
        **kwargs,
    ) -> None:
        """Initializes the task.

        Parameters
        ----------
        loss:
            Loss function. Defaults to BinaryCrossentropy.
        metrics:
            List of Keras metrics to be evaluated.
        prediction_metrics:
            List of Keras metrics used to summarize the predictions.
        label_metrics:
            List of Keras metrics used to summarize the labels.
        loss_metrics:
            List of Keras metrics used to summarize the loss.
        name:
            Optional task name.
        """

        super().__init__(name=name, **kwargs)
        self.target_name = target_name
        self.sequence_summary = TFSequenceSummary(
            SimpleNamespace(summary_type=summary_type)
        )  # noqa
        self.pre = pre
        self.task_block = task_block
        self.loss = loss
        self._task_name = task_name

        create_metrics = self._create_metrics
        self.eval_metrics = create_metrics(metrics) if metrics else []
        self.prediction_metrics = create_metrics(prediction_metrics) if prediction_metrics else []
        self.label_metrics = create_metrics(label_metrics) if label_metrics else []
        self.loss_metrics = create_metrics(loss_metrics) if loss_metrics else []

    def build(self, input_shape, inputs=None):
        return super().build(input_shape)

    def call(self, inputs, **kwargs):
        x = inputs

        if len(x.shape) == 3:
            x = self.sequence_summary(x)

        if self.task_block:
            x = self.task_block(x)

        if self.pre:
            x = self.pre(x)

        return x

    def _create_metrics(self, metrics: List[MetricOrMetricClass]) -> List[tf.keras.metrics.Metric]:
        outputs = []
        for metric in metrics:
            if not isinstance(metric, tf.keras.metrics.Metric):
                metric = metric(name=self.child_name(generic_utils.to_snake_case(metric.__name__)))
            outputs.append(metric)

        return outputs

    @property
    def task_name(self):
        if self._task_name:
            return self._task_name

        base_name = generic_utils.to_snake_case(self.__class__.__name__)

        return name_fn(self.target_name, base_name) if self.target_name else base_name

    def child_name(self, name):
        return name_fn(self.task_name, name)

    def compute_loss(
        self,
        inputs,
        targets,
        compute_metrics=True,
        sample_weight: Optional[tf.Tensor] = None,
        **kwargs,
    ) -> tf.Tensor:
        if isinstance(targets, dict) and self.target_name:
            targets = targets[self.target_name]

        predictions = self(inputs, **kwargs)
        loss = self.loss(y_true=targets, y_pred=predictions, sample_weight=sample_weight)

        if compute_metrics:
            update_ops = self.calculate_metrics(predictions, targets, forward=False, loss=loss)

            update_ops = [x for x in update_ops if x is not None]

            with tf.control_dependencies(update_ops):
                return tf.identity(loss)

        return loss

    def repr_add(self):
        return [("loss", self.loss)]

    def calculate_metrics(self, predictions, targets, sample_weight=None, forward=True, loss=None):
        if isinstance(targets, dict) and self.target_name:
            targets = targets[self.target_name]

        if forward:
            predictions = self(predictions)

        update_ops = []

        for metric in self.eval_metrics:
            update_ops.append(
                metric.update_state(y_true=targets, y_pred=predictions, sample_weight=sample_weight)
            )

        for metric in self.prediction_metrics:
            update_ops.append(metric.update_state(predictions, sample_weight=sample_weight))

        for metric in self.label_metrics:
            update_ops.append(metric.update_state(targets, sample_weight=sample_weight))

        for metric in self.loss_metrics:
            if not loss:
                loss = self.loss(y_true=targets, y_pred=predictions, sample_weight=sample_weight)
            update_ops.append(metric.update_state(loss, sample_weight=sample_weight))

        return update_ops

    def metric_results(self, mode: str = None):
        return {metric.name: metric.result() for metric in self.metrics}

    def reset_metrics(self):
        for metric in self.metrics:
            metric.reset()

    def to_head(self, body, inputs=None, **kwargs) -> Head:
        from .head import Head as _Head

        return _Head(body, self, inputs=inputs, **kwargs)

    def to_model(self, body, inputs=None, **kwargs) -> Model:
        from .head import Head as _Head
        from .model import Model as _Model

        return _Model(_Head(body, self, inputs=inputs, **kwargs), **kwargs)

    @classmethod
    def from_config(cls, config):
        config = maybe_deserialize_keras_objects(
            config,
            {
                "pre": tf.keras.layers.deserialize,
                "loss": tf.keras.losses.deserialize,
                "metrics": tf.keras.metrics.deserialize,
                "prediction_metrics": tf.keras.metrics.deserialize,
                "label_metrics": tf.keras.metrics.deserialize,
                "loss_metrics": tf.keras.metrics.deserialize,
            },
        )

        return super().from_config(config)

    def get_config(self):
        config = super().get_config()
        config = maybe_serialize_keras_objects(
            self,
            config,
            ["metrics", "prediction_metrics", "label_metrics", "loss_metrics", "loss", "pre"],
        )

        config["summary_type"] = self.sequence_summary.summary_type
        if self.target_name:
            config["target_name"] = self.target_name
        if self._task_name:
            config["task_name"] = self._task_name

        return config


@tf.keras.utils.register_keras_serializable(package="transformers4rec")
class BinaryClassificationTask(PredictionTask):
    DEFAULT_LOSS = tf.keras.losses.BinaryCrossentropy()
    DEFAULT_METRICS = (
        tf.keras.metrics.Precision,
        tf.keras.metrics.Recall,
        tf.keras.metrics.BinaryAccuracy,
        tf.keras.metrics.AUC,
    )

    def __init__(
        self,
        target_name: Optional[str] = None,
        task_name: Optional[str] = None,
        task_block: Optional[Layer] = None,
        loss=DEFAULT_LOSS,
        metrics: List[MetricOrMetricClass] = DEFAULT_METRICS,
        summary_type="first",
        **kwargs,
    ):
        super().__init__(
            loss=loss,
            metrics=metrics,
            target_name=target_name,
            task_name=task_name,
            summary_type=summary_type,
            task_block=task_block,
            **kwargs,
        )
        self.pre = tf.keras.layers.Dense(1, activation="sigmoid", name=self.child_name("logit"))


@tf.keras.utils.register_keras_serializable(package="transformers4rec")
class RegressionTask(PredictionTask):
    DEFAULT_LOSS = tf.keras.losses.MeanSquaredError()
    DEFAULT_METRICS = (tf.keras.metrics.RootMeanSquaredError,)

    def __init__(
        self,
        target_name: Optional[str] = None,
        task_name: Optional[str] = None,
        task_block: Optional[Layer] = None,
        loss=DEFAULT_LOSS,
        metrics=DEFAULT_METRICS,
        summary_type="first",
        **kwargs,
    ):
        super().__init__(
            loss=loss,
            metrics=metrics,
            target_name=target_name,
            task_name=task_name,
            summary_type=summary_type,
            task_block=task_block,
            **kwargs,
        )
        self.pre = tf.keras.layers.Dense(1, name=self.child_name("logit"))


@tf.keras.utils.register_keras_serializable(package="transformers4rec")
class NextItemPredictionTask(PredictionTask):
    """Next-item prediction task.

    Parameters
    ----------
    loss:
        Loss function. SparseCategoricalCrossentropy()
    metrics:
        List of RankingMetrics to be evaluated.
    prediction_metrics:
        List of Keras metrics used to summarize the predictions.
    label_metrics:
        List of Keras metrics used to summarize the labels.
    loss_metrics:
        List of Keras metrics used to summarize the loss.
    name:
        Optional task name.
    target_dim: int
        Dimension of the target.
    weight_tying: bool
        The item id embedding table weights are shared with the prediction network layer.
    item_embedding_table: tf.Variable
        Variable of embedding table for the item.
    softmax_temperature: float
        Softmax temperature, used to reduce model overconfidence, so that softmax(logits / T).
        Value 1.0 reduces to regular softmax.
    """

    DEFAULT_LOSS = tf.keras.losses.SparseCategoricalCrossentropy()
    DEFAULT_METRICS = (
        # default metrics suppose labels are int encoded
        NDCGAt(top_ks=[10, 20], labels_onehot=True),
        AvgPrecisionAt(top_ks=[10, 20], labels_onehot=True),
        RecallAt(top_ks=[10, 20], labels_onehot=True),
    )

    def __init__(
        self,
        loss=DEFAULT_LOSS,
        metrics=DEFAULT_METRICS,
        target_name: Optional[str] = None,
        task_name: Optional[str] = None,
        task_block: Optional[Layer] = None,
        weight_tying: bool = False,
        target_dim: int = None,
        softmax_temperature: float = 1,
        **kwargs,
    ):
        super().__init__(
            loss=loss,
            metrics=metrics,
            target_name=target_name,
            task_name=task_name,
            task_block=task_block,
            **kwargs,
        )
        self.weight_tying = weight_tying
        self.target_dim = target_dim
        self.softmax_temperature = softmax_temperature

    def build(self, input_shape, body, inputs=None):
        # Retrieve the embedding module to get the name of itemid col and its related table
        if not len(input_shape) == 3 or isinstance(input_shape, dict):
            raise ValueError(
                "NextItemPredictionTask needs a 3-dim vector as input, found:" f"{input_shape}"
            )
        if not inputs:
            inputs = body.inputs
        if not getattr(inputs, "item_id", None):
            raise ValueError(
                "For Item Prediction task a categorical_module "
                "including an item_id column is required."
            )
        self.embeddings = inputs.categorical_layer
        if not self.target_dim:
            self.target_dim = self.embeddings.item_embedding_table.shape[0]
        if self.weight_tying:
            self.item_embedding_table = self.embeddings.item_embedding_table
            item_dim = self.item_embedding_table.shape[1]
            if input_shape[-1] != item_dim and not self.task_block:
                LOG.warning(
                    f"Projecting inputs of NextItemPredictionTask to'{item_dim}' "
                    f"As weight tying requires the input dimension '{input_shape[-1]}' "
                    f"to be equal to the item-id embedding dimension '{item_dim}'"
                )
                # project input tensors to same dimension as item-id embeddings
                self.task_block = MLPBlock([item_dim])

        # Retrieve the masking if used in the model block
        self.masking = inputs.masking
        if self.masking:
            self.padding_idx = self.masking.padding_idx

        self.pre = _NextItemPredictionTask(
            target_dim=self.target_dim,
            weight_tying=self.weight_tying,
            item_embedding_table=self.item_embedding_table,
            softmax_temperature=self.softmax_temperature,
        )
        return super().build(input_shape)

    def call(self, inputs):
        if isinstance(inputs, (tuple, list)):
            inputs = inputs[0]

        x = inputs

        if self.task_block:
            x = self.task_block(x)

        # retrieve labels from masking
        if self.masking:
            labels = self.masking.masked_targets
        else:
            labels = self.embeddings.item_seq

        # remove vectors of padded items
        trg_flat = tf.reshape(labels, -1)
        non_pad_mask = trg_flat != self.padding_idx
        x = self.remove_pad_3d(x, non_pad_mask)

        # compute predictions probs
        x = self.pre(x)
        return x

    def remove_pad_3d(self, inp_tensor, non_pad_mask):
        # inp_tensor: (n_batch x seqlen x emb_dim)
        inp_tensor = tf.reshape(inp_tensor, (-1, inp_tensor.shape[-1]))
        inp_tensor_fl = tf.boolean_mask(
            inp_tensor, tf.broadcast_to(tf.expand_dims(non_pad_mask, 1), inp_tensor.shape)
        )
        out_tensor = tf.reshape(inp_tensor_fl, (-1, inp_tensor.shape[1]))
        return out_tensor

    def compute_loss(
        self,
        inputs,
        targets=None,
        compute_metrics=True,
        sample_weight: Optional[tf.Tensor] = None,
    ) -> tf.Tensor:
        if isinstance(targets, dict) and self.target_name:
            targets = targets[self.target_name]

        predictions = self(inputs)
        # retrieve labels from masking
        if self.masking:
            targets = self.masking.masked_targets
            # flatten labels and remove padding index
            targets = tf.reshape(targets, -1)
            non_pad_mask = targets != self.padding_idx
            targets = tf.boolean_mask(targets, non_pad_mask)

        loss = self.loss(y_true=targets, y_pred=predictions, sample_weight=sample_weight)

        if compute_metrics:
            update_ops = self.calculate_metrics(predictions, targets, forward=False, loss=loss)

            update_ops = [x for x in update_ops if x is not None]

            with tf.control_dependencies(update_ops):
                return tf.identity(loss)

        return loss

    def calculate_metrics(
        self, predictions, targets=None, sample_weight=None, forward=True, loss=None
    ):
        if isinstance(targets, dict) and self.target_name:
            targets = targets[self.target_name]

        if forward:
            predictions = self(predictions)
            # retrieve labels from masking
            if self.masking:
                targets = self.masking.masked_targets
            # flatten labels and remove padding index
            targets = tf.reshape(targets, -1)
            non_pad_mask = targets != self.padding_idx
            targets = tf.boolean_mask(targets, non_pad_mask)

        update_ops = []

        for metric in self.eval_metrics:
            update_ops.append(
                metric.update_state(y_true=targets, y_pred=predictions, sample_weight=sample_weight)
            )

        for metric in self.prediction_metrics:
            update_ops.append(metric.update_state(predictions, sample_weight=sample_weight))

        for metric in self.label_metrics:
            update_ops.append(metric.update_state(targets, sample_weight=sample_weight))

        for metric in self.loss_metrics:
            if not loss:
                loss = self.loss(y_true=targets, y_pred=predictions, sample_weight=sample_weight)
            update_ops.append(metric.update_state(loss, sample_weight=sample_weight))

        return update_ops

    def metric_results(self, mode: str = None) -> Dict[str, tf.Tensor]:
        metrics = {metric.name: metric.result() for metric in self.eval_metrics}
        topks = {metric.name: metric.top_ks for metric in self.eval_metrics}
        # explode metrics for each cut-off in top_ks
        results = {}
        for name, metric in metrics.items():
            for measure, k in zip(metric, topks[name]):
                results[f"{name}_{k}"] = measure
        return results


class _NextItemPredictionTask(tf.keras.layers.Layer):
    """Predict the interacted item-id probabilities.
    - During inference, the task consists of predicting the next item.
    - During training, the class supports the following Language modeling tasks:
        Causal LM and Masked LM.
        p.s: we are planning to support Permutation LM and Replacement Token Detection
        in future release.
    Parameters:
    -----------
    target_dim: int
        Dimension of the target.
    weight_tying: bool
        The item id embedding table weights are shared with the prediction network layer.
    item_embedding_table: tf.Variable
        Variable of embedding table for the item.
    softmax_temperature: float
        Softmax temperature, used to reduce model overconfidence, so that softmax(logits / T).
        Value 1.0 reduces to regular softmax.
    """

    def __init__(
        self,
        target_dim: int,
        weight_tying: bool = False,
        item_embedding_table: Optional[tf.Variable] = None,
        softmax_temperature: float = 0,
    ):
        super().__init__()
        self.target_dim = target_dim
        self.weight_tying = weight_tying
        self.item_embedding_table = item_embedding_table
        self.softmax_temperature = softmax_temperature

        if self.weight_tying:
            if item_embedding_table is None:
                raise ValueError(
                    "For Item Prediction task with weight tying "
                    "the embedding table of item_id is required ."
                )
            self.output_layer_bias = self.add_weight(
                name="output_layer_bias",
                shape=(self.target_dim,),
                initializer=tf.keras.initializers.Zeros(),
            )

        else:
            self.output_layer = tf.keras.layers.Dense(
                units=self.target_dim,
                kernel_initializer="random_normal",
                bias_initializer="zeros",
                name="logits",
            )

    def call(self, inputs: tf.Tensor):
        if self.weight_tying:
            logits = tf.matmul(inputs, tf.transpose(self.item_embedding_table))
            logits = tf.nn.bias_add(logits, self.output_layer_bias)
        else:
            logits = self.output_layer(inputs)

        if self.softmax_temperature:
            # Softmax temperature to reduce model overconfidence
            # and better calibrate probs and accuracy
            logits = logits / self.softmax_temperature

        predictions = tf.nn.log_softmax(logits, axis=-1)
        return predictions

    def _get_name(self) -> str:
        return "NextItemPredictionTask"