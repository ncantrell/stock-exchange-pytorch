import argparse
import datetime
import logging
import numpy as np
import os
import pandas as pd
import torch
from torch.utils.data import DataLoader
from functools import partial, wraps
from ignite.engine import Events, create_supervised_trainer, create_supervised_evaluator
from supervised import get_metrics, Classifier, TickersData, device, ConvBlockWrapper
from supervised.utils_ignite import prepare_batch_all, prepare_batch_empty_label, get_binary_target
import warnings
warnings.filterwarnings('ignore')


def get_tickers(args):
    from download_daily_data import my_list, russell_ticker_set

    all_tickers = my_list | russell_ticker_set

    my_list = list(all_tickers)[:args.max_num_tickers]
    pickle_files = list(map(lambda x: x.split('_')[0], os.listdir('data/ohlc_processed/')))
    valid_tickers = [item for item in my_list if item in pickle_files]

    return valid_tickers


def compute_threshold_mask(given_tickers, args):

    temp_set = TickersData(given_tickers, '_train.pickle')
    original_y = pd.DataFrame(temp_set.y)
    len_given_tickers = len(given_tickers)
    assert original_y.shape[1] % len_given_tickers == 0

    y_dim_per_ticker = original_y.shape[1] // len_given_tickers

    duplicated_tickers = np.repeat(given_tickers, y_dim_per_ticker)

    half_length = len(original_y) // 2
    original_y.drop(pd.RangeIndex(half_length, len(original_y)), inplace=True)
    original_y.set_axis(duplicated_tickers, axis=1)

    thresholds = original_y.quantile(args.percentile)

    del temp_set, original_y

    return thresholds


def get_data_loaders_etc(args):
    def get_shift_data_point_transform_dims():
        # + 1 comes from today's shift
        # args.shift_increment should replace with new changes
        # Refer to train_supervised_ohlc_per_day_corss_entropy_no_transform
        #   and changes in supervised.utils.delta_dataframe_with_y_columns_new
        shift_dim = len(list(range(-args.max_shift_forward,
                                   -args.min_shift_forward,
                                   args.max_shift_forward // args.shift_increment))) + 1
        # for a new version: ----> args.shift_increment))) + 1

        # Usually, ohlc+volume = 5
        data_point_dim = args.data_point_dim
        # Number of different sklearn.pipelines.FeatureUnion, found in process_and_save_ohlc_daily.py
        transform_dim = args.transform_dim

        expected_dim = shift_dim * data_point_dim * transform_dim * len_valid_tickers
        example_row_dim = train_set[0][0].shape[0]
        output_dim = train_set[0][2].shape[0]

        assert example_row_dim == expected_dim, '{} vs {}'.format(example_row_dim, expected_dim)

        return shift_dim, data_point_dim, transform_dim, output_dim

    tentative_tickers = get_tickers(args)
    thresholds = compute_threshold_mask(tentative_tickers, args)
    binary_transform_fn = partial(get_binary_target, threshold=thresholds, args=args)

    train_set = TickersData(tentative_tickers, '_train.pickle', y_transform=binary_transform_fn)
    test_set = TickersData(tentative_tickers, '_test.pickle', y_transform=binary_transform_fn)
    train_dl = DataLoader(train_set, num_workers=1, batch_size=args.batch_size, shuffle=True)
    test_dl = DataLoader(test_set, num_workers=1, batch_size=args.batch_size)

    for ticker in train_set.unused_tickers_y:
        tentative_tickers.remove(ticker)

    len_valid_tickers = len(tentative_tickers)

    dimension_args = get_shift_data_point_transform_dims()

    return train_dl, test_dl, len_valid_tickers, dimension_args


def compute_return_distribution_on_pred(model, data_loader, threshold=0.5):
    '''
    :param model: original model
    :param data_loader: iterable of x, y, y_transformed ### Should re-write this
    :param threshold: default to 0.5 (e.g. output >= 0.5)
    :return: mean and stdev of actual outcomes from predicted true by the model

    It concats outputs altogether first, then runs the stat
    This is not an efficient way of using memory, but EpochMetric handles
    things similarly, so leave it as is for now...
    '''
    so_far = torch.tensor([], dtype=torch.float32, device=device)

    with torch.no_grad():
        for batch in data_loader:
            x, y, y_transformed = prepare_batch_all(batch, device=device)
            out = model(x)

            mask = out.ge(threshold)
            masked_out = torch.masked_select(out, mask)

            if masked_out.nelement() > 0:
                masked_y = torch.masked_select(y, mask)
                distribution = masked_out * masked_y
                so_far = torch.cat([so_far, distribution], dim=0)

    return so_far.mean(), so_far.std()


def wrap_model_in_eval_mode(model):
    def _wrap_model_in_eval_mode(func):
        @wraps(func)
        def _wrapper(*args, **kwargs):
            model.eval()
            func(*args, **kwargs)
            model.train()
        return _wrapper
    return _wrap_model_in_eval_mode


def register_evaluators(trainer,
                        evaluator_train,
                        evaluator_test,
                        train_dl,
                        test_dl,
                        model,
                        args,
                        bce_logger):

    @trainer.on(Events.EPOCH_COMPLETED)
    @wrap_model_in_eval_mode(model)
    def log_training_results(trainer):
        if trainer.state.epoch % args.print_every == 0:

            mean_stat, std_stat = compute_return_distribution_on_pred(model, train_dl)
            evaluator_train.run(train_dl)
            metrics = evaluator_train.state.metrics

            msg1 = "Training Results  - Epoch:{}, Accuracy:{:.5f}, BCE:{:.5f}, F1 Score:{:.5f}, ROC_AUC:{:.5f}, ".format(
                trainer.state.epoch, metrics['accuracy'], metrics['bce'], metrics['f1_score'], metrics['roc_auc'],)
            msg2 = "Precision:{:.5f}, Recall:{:.5f}, ".format(metrics['precision'], metrics['recall'],)
            msg3 = 'Mean:{:.5f}, Stdev:{:.5f}, ConfusionMatrix:{}, '.format(mean_stat, std_stat, metrics["conf_matrix"].ravel())

            print_and_log(msg1+msg2+msg3, bce_logger)
            print("Training Results  - Epoch: {} Confusion Matrix: \n{}".format(
                trainer.state.epoch, metrics['conf_matrix'], ))

    @trainer.on(Events.EPOCH_COMPLETED)
    @wrap_model_in_eval_mode(model)
    def log_validation_results(trainer):
        if trainer.state.epoch % args.print_every == 0:

            mean_stat, std_stat = compute_return_distribution_on_pred(model, test_dl)
            evaluator_test.run(test_dl)
            metrics = evaluator_test.state.metrics

            msg1 = "Validation Results  - Epoch:{}, Accuracy:{:.5f}, BCE:{:.5f}, F1 Score:{:.5f}, ROC_AUC:{:.5f}, ".format(
                trainer.state.epoch, metrics['accuracy'], metrics['bce'], metrics['f1_score'], metrics['roc_auc'],)
            msg2 = "Precision:{:.5f}, Recall:{:.5f}, ".format(metrics['precision'], metrics['recall'],)
            msg3 = 'Mean:{:.5f}, Stdev:{:.5f}, ConfusionMatrix:{}, '.format(mean_stat, std_stat, metrics["conf_matrix"].ravel())

            print_and_log(msg1+msg2+msg3, bce_logger)
            print("Validation Results  - Epoch: {} Confusion Matrix: \n{}".format(
                trainer.state.epoch, metrics['conf_matrix'], ))


def print_and_log(msg, logger):
    print(f'{msg}')
    logger.info(f'{msg}')


def main(args):
    bce_logger, file_handler = get_logger(args)

    print_and_log('--- Starting training:{}, Parameters:{}'.format(datetime.datetime.now(), args), bce_logger)

    train_dl, test_dl, num_tickers, dimensions = get_data_loaders_etc(args)
    shift_dim, data_point_dim, transform_dim, output_dim = dimensions

    # model = Classifier(num_tickers,
    #                    data_point_dim=data_point_dim,
    #                    shift_dim=shift_dim,
    #                    transform_dim=transform_dim,
    #                    output_dim=output_dim)
    model = ConvBlockWrapper(num_tickers,
                             data_point_dim=data_point_dim,
                             shift_dim=shift_dim,
                             transform_dim=transform_dim,
                             output_dim=output_dim,
                             args=args)
    criterion = torch.nn.BCELoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate, weight_decay=1e-6)

    trainer = create_supervised_trainer(model,
                                        optimizer,
                                        criterion,
                                        device=device,
                                        prepare_batch=prepare_batch_empty_label)
    evaluator_train = create_supervised_evaluator(model,
                                                  metrics=get_metrics(None),
                                                  device=device,
                                                  prepare_batch=prepare_batch_empty_label)
    evaluator_test = create_supervised_evaluator(model,
                                                 metrics=get_metrics(None),
                                                 device=device,
                                                 prepare_batch=prepare_batch_empty_label)

    register_evaluators(trainer,
                        evaluator_train,
                        evaluator_test,
                        train_dl,
                        test_dl,
                        model,
                        args,
                        bce_logger,)

    trainer.run(train_dl, max_epochs=args.max_epoch)

    print_and_log('--- Ending training: {}'.format(datetime.datetime.now()), bce_logger)

    bce_logger.removeHandler(file_handler)
    del bce_logger, file_handler, train_dl, test_dl, model, optimizer, criterion, evaluator_train, evaluator_test


def get_args():
    parser = argparse.ArgumentParser(description='Hyper-parameters for the training')
    parser.add_argument('--max_epoch',       default=32, type=int)
    parser.add_argument('--max_num_tickers', default=800, type=int)
    parser.add_argument('--print_every',     default=1, type=int)
    parser.add_argument('--batch_size',      default=64, type=int)
    parser.add_argument('--data_point_dim',  default=5, type=int)
    parser.add_argument('--transform_dim',   default=4, type=int)
    parser.add_argument('--shift_increment', default=3, type=int)
    parser.add_argument('--block_depth',     default=4, type=int)
    parser.add_argument('--const_factor',    default=4, type=int, help='arbitrary constant used for computing output dim')
    parser.add_argument('--linear_dim',      default=4, type=int)
    parser.add_argument('--min_shift_forward',      default=3,  type=int)
    parser.add_argument('--max_shift_forward',      default=10, type=int)
    parser.add_argument('--learning_rate',   default=0.01,  type=float)
    parser.add_argument('--percentile',      default=0.9,   type=float, help='percentile from a distribution')
    args = parser.parse_args()
    return args


def get_logger(args):
    try:
        os.makedirs('logs')
    except OSError:
        print('--- log folder exists')

    FILE_NAME_BASIC_INFO = 'logs/training_bce_{}_{}.log'.format(
        '_'.join(str(datetime.datetime.now()).split(' ')), args.percentile)

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    bce_logger = logging.getLogger(__name__)
    bce_logger.setLevel(logging.INFO)

    file_handler = logging.FileHandler(FILE_NAME_BASIC_INFO)
    file_handler.setFormatter(formatter)
    bce_logger.addHandler(file_handler)

    return bce_logger, file_handler


if __name__ == '__main__':

    args = get_args()
    main(args)
