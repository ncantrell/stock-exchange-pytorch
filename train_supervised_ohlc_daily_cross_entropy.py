import argparse
import datetime
import logging
import numpy as np
import os
import warnings
warnings.filterwarnings('ignore')

from sklearn.preprocessing import MinMaxScaler, MaxAbsScaler, Normalizer, QuantileTransformer
from sklearn.pipeline import FeatureUnion

import torch
import torch.nn as nn
from torch.utils.data import DataLoader

from ignite.engine import Events, \
                          create_supervised_trainer, \
                          create_supervised_evaluator

from supervised import load_dataframes, get_y_cols, TickerDataSimple, get_metrics
from download_daily_data import all_tickers


class LogisticRegressor(nn.Module):
    def __init__(self, input_size, final_output_size):
        super(LogisticRegressor, self).__init__()
        self.e1 = nn.Embedding(5, 2)
        self.e2 = nn.Embedding(7, 2)
        # +2 for embedding... 2+2-2
        self.l1 = nn.Linear(input_size + 2, 64)
        self.l2 = nn.Linear(64, 16)
        # self.l3 = nn.Linear(32, 16)
        self.l4 = nn.Linear(16, final_output_size)

    def forward(self, x):
        x1, x2, = x[:, -2].long(), x[:, -1].long()
        x1 = self.e1(x1)
        x2 = self.e2(x2)

        x = torch.cat([x[:, :-2], x1, x2], dim=1)
        x = torch.relu(self.l1(x))
        x = torch.tanh(self.l2(x))
        # x = torch.tanh(self.l3(x))
        return torch.sigmoid(self.l4(x))


class CustomLoss(torch.nn.Module):
    '''
    A simple version of Focal Loss
    '''

    def __init__(self):
        super(CustomLoss, self).__init__()

    def forward(self, y_pred, y_target):
        y_pred = y_pred.flatten()
        y_target = y_target.flatten()

        def log_p(pred, target):
            return -((1 - pred) * torch.log2(pred) * target)

        return (log_p(y_pred,     y_target) +
                log_p(1 - y_pred, 1 - y_target)).mean()


def get_transfomed_combiner(df):
    # Use only the ones worked well in autoencoder
    transfomer = [
        ('Data after min-max scaling',
         MinMaxScaler()),
        ('Data after max-abs scaling',
         MaxAbsScaler()),
        ('Data after quantile transformation (uniform pdf)',
         QuantileTransformer(output_distribution='uniform')),
        ('Data after sample-wise L2 normalizing',
         Normalizer()),
    ]

    combined = FeatureUnion(transfomer)
    _ = combined.fit(df)

    return combined


def binary_target(y_train, y_test):
    if args.threshold >= 0.0:
        binary_y_train = y_train > args.threshold
        binary_y_test  = y_test > args.threshold
    else:
        binary_y_train = y_train < args.threshold
        binary_y_test  = y_test < args.threshold

    return binary_y_train.astype(np.int), binary_y_test.astype(np.int)


def get_input_target(ticker):
    # messy code...
    train_df_original, test_df_original, numeric_cols, categoric_cols = load_dataframes(ticker)
    if train_df_original is None:
        return [None] * 6

    y_cols, not_interested = get_y_cols(numeric_cols)
    numeric_cols = list(set(numeric_cols) - set(y_cols) - set(not_interested))
    # Let's not do month embedding
    categoric_cols.remove('month')
    categorical_train_embeddings = train_df_original[categoric_cols].applymap(lambda x: int(x))
    categorical_test_embeddings = test_df_original[categoric_cols].applymap(lambda x: int(x))

    train_df, y_train = train_df_original[numeric_cols], train_df_original[y_cols]
    test_df, y_test = test_df_original[numeric_cols], test_df_original[y_cols]
    y_train.drop(y_train.columns[2:], axis=1, inplace=True)
    y_test.drop(y_test.columns[2:], axis=1, inplace=True)

    binary_y_train, binary_y_test = binary_target(y_train, y_test)

    combined = get_transfomed_combiner(train_df)

    x_train_transformed = combined.transform(train_df)
    x_test_transformed = combined.transform(test_df)
    x_train_numerical_transformed = torch.from_numpy(x_train_transformed).float()
    x_test_numerical_transformed = torch.from_numpy(x_test_transformed).float()

    x_train_categorical_cols = torch.from_numpy(categorical_train_embeddings.values).float()
    x_train_categorical_cols[:, 1] -= 9 # Should just be min of the column...
    x_test_categorical_cols = torch.from_numpy(categorical_test_embeddings.values).float()
    x_test_categorical_cols[:, 1] -= 9

    x_train_all = torch.cat([x_train_numerical_transformed, x_train_categorical_cols], dim=1)
    x_test_all  = torch.cat([x_test_numerical_transformed, x_test_categorical_cols], dim=1)

    return x_train_all, binary_y_train, x_test_all, binary_y_test, y_train, y_test


def register_evaluators(trainer, evaluator_train, evaluator_test):

    @trainer.on(Events.EPOCH_COMPLETED)
    def log_training_results(trainer):
        if trainer.state.epoch % args.print_every == 0:
            evaluator_train.run(train_dl)
            metrics = evaluator_train.state.metrics
            print("Training Results  - Epoch: {} Avg accuracy: {:.5f}, Avg BCE: {:.5f}, F1 Score: {:.5f}, ROC_AUC: {:.5f}".format(
                trainer.state.epoch, metrics['accuracy'], metrics['bce'], metrics['f1_score'], metrics['roc_auc'],))
            print("Training Results  - Epoch: {} Precision: {:.5f}, Recall: {:.5f}".format(
                trainer.state.epoch, metrics['precision'], metrics['recall'],))
            print("Training Results  - Epoch: {} Pred Positive Stat: {:.5f}, {:.5f}".format(
                trainer.state.epoch, metrics['positive_stat'][0], metrics['positive_stat'][1]))
            print("Training Results  - Epoch: {} Confusion Matrix: \n{}".format(
                trainer.state.epoch, metrics['conf_matrix'], ))

            bce_logger.info("Training Results  - Epoch: {} Avg accuracy: {:.5f}, Avg BCE: {:.5f}, F1 Score: {:.5f}, ROC_AUC: {:.5f}".format(
                trainer.state.epoch, metrics['accuracy'], metrics['bce'], metrics['f1_score'], metrics['roc_auc'],))
            bce_logger.info("Training Results  - Epoch: {} Precision: {:.5f}, Recall: {:.5f}".format(
                trainer.state.epoch, metrics['precision'], metrics['recall'],))
            stat_logger.info('{},{},train,{:.5f},{:.5f},{}'.format(
                ticker, trainer.state.epoch, metrics["positive_stat"][0], metrics["positive_stat"][1], metrics["conf_matrix"].ravel()))


    @trainer.on(Events.EPOCH_COMPLETED)
    def log_validation_results(trainer):
        if trainer.state.epoch % args.print_every == 0:
            evaluator_test.run(test_dl)
            metrics = evaluator_test.state.metrics
            print("Validation Results- Epoch: {} Avg accuracy: {:.5f}, Avg BCE: {:.5f}, F1 Score: {:.5f}, ROC_AUC: {:.5f}".format(
                trainer.state.epoch, metrics['accuracy'], metrics['bce'], metrics['f1_score'],metrics['roc_auc'],))
            print("Validation Results- Epoch: {} Precision: {:.5f}, Recall: {:.5f}".format(
                trainer.state.epoch, metrics['precision'], metrics['recall'],))
            print("Validation Results- Epoch: {} Pred Positive Stat: {:.5f}, {:.5f}".format(
                trainer.state.epoch, metrics['positive_stat'][0], metrics['positive_stat'][1]))
            print("Validation Results- Epoch: {} Confusion Matrix: \n{}".format(
                trainer.state.epoch, metrics['conf_matrix'], ))

            bce_logger.info("Validation Results- Epoch: {} Avg accuracy: {:.5f}, Avg BCE: {:.5f}, F1 Score: {:.5f}, ROC_AUC: {:.5f}".format(
                trainer.state.epoch, metrics['accuracy'], metrics['bce'], metrics['f1_score'],metrics['roc_auc'],))
            bce_logger.info("Validation Results- Epoch: {} Precision: {:.5f}, Recall: {:.5f}".format(
                trainer.state.epoch, metrics['precision'], metrics['recall'],))
            stat_logger.info('{},{},test,{:.5f},{:.5f},{}'.format(
                ticker, trainer.state.epoch, metrics["positive_stat"][0], metrics["positive_stat"][1], metrics["conf_matrix"].ravel()))


def print_and_log(msg, logger):
    print(f'{msg}')
    logger.info(f'{msg}')


parser = argparse.ArgumentParser(description='Hyper-parameters for the training')
parser.add_argument('--max_epoch',       default=50, type=int)
parser.add_argument('--print_every',     default=10, type=int)
parser.add_argument('--batch_size',      default=64, type=int)
parser.add_argument('--threshold',       default=0.003, type=float)

args = parser.parse_args()
args.tickers = list(all_tickers)

try:
    os.makedirs('logs')
except OSError:
    print('--- log folder exists')

FILE_NAME_BASIC_INFO = 'logs/training_bce_{}_{}.log'.format('_'.join(str(datetime.datetime.now()).split(' ')), args.threshold)
FILE_NAME_STAT = 'logs/training_bce_{}_stat_{}.log'.format('_'.join(str(datetime.datetime.now()).split(' ')), args.threshold)

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

bce_logger = logging.getLogger(__name__)
bce_logger.setLevel(logging.INFO)
stat_logger = logging.getLogger(__name__+'_stat')
stat_logger.setLevel(logging.INFO)

file_handler = logging.FileHandler(FILE_NAME_BASIC_INFO)
file_handler.setFormatter(formatter)
bce_logger.addHandler(file_handler)

file_handler_stat = logging.FileHandler(FILE_NAME_STAT)
stat_logger.addHandler(file_handler_stat)

device = 'cpu'
if torch.cuda.is_available():
    device = 'cuda'


if __name__ == '__main__':

    for ticker in args.tickers:

        print_and_log('--- Starting training for {}'.format(ticker), bce_logger)

        (x_train_all, binary_y_train, x_test_all, binary_y_test,
                            non_binary_y_train, non_binary_y_test) = get_input_target(ticker)
        # Not enough data
        if (x_train_all is None) or (len(x_train_all) < 10000): continue

        # This is really shitty way but for the time being, since memory is not an issue
        spy_dataset = TickerDataSimple(ticker, x_train_all, torch.from_numpy(binary_y_train.values).float())
        spy_testset = TickerDataSimple(ticker, x_test_all,  torch.from_numpy(binary_y_test.values).float())

        non_binary_y_train = torch.from_numpy(non_binary_y_train.values).float()
        non_binary_y_test  = torch.from_numpy(non_binary_y_test.values).float()

        train_dl = DataLoader(spy_dataset, num_workers=1, batch_size=args.batch_size)
        test_dl = DataLoader(spy_testset, num_workers=1, batch_size=args.batch_size)

        # Transformer has 4 different ways
        model = LogisticRegressor(x_train_all.shape[1], binary_y_train.shape[1])
        # criterion = CustomLoss()
        criterion = torch.nn.BCELoss()
        optimizer = torch.optim.Adam(model.parameters(), lr=5e-3, weight_decay=1e-6)

        trainer = create_supervised_trainer(model, optimizer, criterion, device=device)

        evaluator_train = create_supervised_evaluator(
            model, metrics=get_metrics(non_binary_y_train), device=device)
        evaluator_test = create_supervised_evaluator(
            model, metrics=get_metrics(non_binary_y_test), device=device)

        register_evaluators(trainer, evaluator_train, evaluator_test)

        trainer.run(train_dl, max_epochs=args.max_epoch)

        print_and_log('--- Ending training for {}'.format(ticker), bce_logger)

        del trainer, evaluator_train, evaluator_test, model, x_train_all, x_test_all, \
            binary_y_train, binary_y_test
