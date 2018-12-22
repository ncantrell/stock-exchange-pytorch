from functools import partial
from ignite.metrics import BinaryAccuracy, EpochMetric, Loss, Precision, Recall
import sklearn.metrics as sk_metrics

import torch
import torch.nn as nn


def sk_metric_fn(y_pred, y_targets, sk_metrics, activation=None):
    y_true = y_targets.flatten().numpy()
    y_pred = y_pred.flatten().numpy()
    if activation is not None:
        y_pred = activation(y_pred)

    return sk_metrics(y_true, y_pred)


class ROC_AUC(EpochMetric):
    def __init__(self, activation=None, output_transform=lambda x: x):
        super(ROC_AUC, self).__init__(
            partial(sk_metric_fn,
                    sk_metrics=sk_metrics.roc_auc_score,
                    activation=activation),
            output_transform=output_transform)


class F1_Score(EpochMetric):
    def __init__(self, activation=None, output_transform=lambda x: x):
        super(F1_Score, self).__init__(
            partial(sk_metric_fn,
                    sk_metrics=sk_metrics.f1_score,
                    activation=activation),
            output_transform=output_transform)


class BinaryAccuracy(EpochMetric):
    def __init__(self, activation=None, output_transform=lambda x: x):
        super(BinaryAccuracy, self).__init__(
            partial(sk_metric_fn,
                    sk_metrics=sk_metrics.accuracy_score,
                    activation=activation),
            output_transform=output_transform)


class Precision(EpochMetric):
    def __init__(self, activation=None, output_transform=lambda x: x):
        super(Precision, self).__init__(
            partial(sk_metric_fn,
                    sk_metrics=sk_metrics.precision_score,
                    activation=activation),
            output_transform=output_transform)


class Recall(EpochMetric):
    def __init__(self, activation=None, output_transform=lambda x: x):
        super(Recall, self).__init__(
            partial(sk_metric_fn,
                    sk_metrics=sk_metrics.recall_score,
                    activation=activation),
            output_transform=output_transform)


class ConfusionMatrix(EpochMetric):
    def __init__(self, activation=None, output_transform=lambda x: x):
        super(ConfusionMatrix, self).__init__(
            partial(sk_metric_fn,
                    sk_metrics=sk_metrics.confusion_matrix,
                    activation=activation),
            output_transform=output_transform)


class PositiveStatistics(EpochMetric):
    def __init__(self, non_binary_y, output_transform=lambda x: x, threshold=0.5):
        super(PositiveStatistics, self).__init__(
            self.compute_stats, output_transform=output_transform)
        self.non_binary_y = non_binary_y
        self.threshold = threshold

    def compute_stats(self, pred, target):
        mask = pred.ge(self.threshold)
        relevant_pred = torch.masked_select(pred, mask)
        if relevant_pred.nelement() == 0:
            return 0.0, -1.0

        assert self.non_binary_y.shape == pred.shape, 'y.shape: {}, pred.shape: {}'.format(
                                                        self.non_binary_y.shape, pred.shape)

        y_value = torch.masked_select(self.non_binary_y, mask)
        distribution = relevant_pred * y_value

        return distribution.mean(), distribution.std()


def zero_one(y_preds):
    return y_preds > 0.5


def zero_one_transform(output):
    return (zero_one(output[0])).long(), output[1].long()


def get_metrics(non_binary_y_target):
    metrics = {
                'accuracy':         BinaryAccuracy(output_transform=zero_one_transform),
                'bce':              Loss(nn.modules.loss.BCELoss()),
                'f1_score':         F1_Score(output_transform=zero_one_transform),
                'roc_auc':          ROC_AUC(),
                'precision':        Precision(output_transform=zero_one_transform),
                'recall':           Recall(output_transform=zero_one_transform),
                'conf_matrix':      ConfusionMatrix(output_transform=zero_one_transform),
                'positive_stat':    PositiveStatistics(non_binary_y_target),
    }
    return metrics

