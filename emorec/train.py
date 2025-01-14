import torch
import torch.nn as nn
import glob
import os
import numpy as np
import argparse
import json
import random
import timeit
from tensorboardX import SummaryWriter
import pase
from random import shuffle
from pase.dataset import *
from pase.models.frontend import wf_builder
import pase.models.classifiers as pmods
from pase.transforms import SingleChunkWav
from pase.utils import kfold_data
from torch.utils.data import DataLoader
import torch.optim as optim
from torch.optim.lr_scheduler import ReduceLROnPlateau
#torch.backends.cudnn.benchmark = False


def retrieve_model_and_datasets(encoder_cfg,
                                model_cfg, data_cfg,
                                train_list, valid_list,
                                test_list):

    with open(model_cfg, 'r') as cfg_f:
        model_cfg = json.load(cfg_f)

    with open(encoder_cfg, 'r') as cfg_f:
        encoder_cfg = json.load(cfg_f)

    with open(data_cfg, 'r') as cfg_f:
        data_cfg = json.load(cfg_f)

    name = encoder_cfg.pop('name')
    cls_name = model_cfg.pop('name')
    # prepare the three datasets; train, valid and test
    splits = [train_list, valid_list, test_list]

    if name == 'pase' or name == 'PASE':
        if 'ckpt' in encoder_cfg:
            ckpt = encoder_cfg.pop('ckpt')
        else:
            ckpt = None
        encoder = wf_builder(encoder_cfg)
        if ckpt is not None:
            encoder.load_pretrained(ckpt,
                                    load_last=True,
                                    verbose=True)
        model_cfg['frontend'] = encoder
        chunker = SingleChunkWav(**data_cfg.pop('chunk_cfg'))
        data_cfg['chunker'] = chunker
        dataset = WavClassDataset
    elif name == 'tdnn' or name == 'TDNN':
        model_cfg['xvector'] = True
        encoder = TDNN(**encoder_cfg)
        model_cfg['frontend'] = encoder
        dataset = FbankSpkDataset
    else:
        raise ValueError('Unrecognized model: ', name)
    model = getattr(pmods, cls_name)(**model_cfg)
    datasets = []
    for si, split in enumerate(splits, start=1):
        data_cfg['split_list'] = split
        if si >= len(splits) - 1 and 'chunker' in data_cfg:
            # remove the chunker for test split
            del data_cfg['chunker']
        datasets.append(dataset(**data_cfg))
    return model, datasets


def accuracy(Y_, Y):
    # Get rid of temporal resolution here,
    # average likelihood in time and then
    # compute argmax and accuracy
    Y__avg = torch.mean(Y_, 2)
    pred = Y__avg.max(1, keepdim=True)[1]
    acc = pred.eq(Y[:, 0].view_as(pred)).float().mean().item()
    return acc

def valid_round(dloader, model, writer, epoch, device='cpu'):
    model.eval()
    with torch.no_grad():
        val_loss = []
        val_acc = []
        for bi, batch in enumerate(dloader, start=1):
            X, Y = batch
            X = X.unsqueeze(1)
            X = X.to(device)
            Y = Y.to(device)
            y = model(X)
            loss = F.nll_loss(y, Y)
            acc = accuracy(y, Y)
            val_loss.append(loss.item())
            val_acc.append(acc)
        mval = np.mean(val_loss)
        macc = np.mean(val_acc)
        print('EVAL: Epoch {} mloss: {:.2f}, macc: {:.2f}'.format(epoch,
                                                                  mval, macc))
        writer.add_scalar('eval/loss', mval, epoch)
        writer.add_scalar('eval/acc', macc, epoch)
        return mval, macc

def main(opts):
    CUDA = True if torch.cuda.is_available() and not opts.no_cuda else False
    device = 'cuda' if CUDA else 'cpu'
    num_devices = 1
    np.random.seed(opts.seed)
    random.seed(opts.seed)
    torch.manual_seed(opts.seed)
    if CUDA:
        torch.cuda.manual_seed_all(opts.seed)
        num_devices = torch.cuda.device_count()
        print('[*] Using CUDA {} devices'.format(num_devices))
    else:
        print('[!] Using CPU')
    print('Seeds initialized to {}'.format(opts.seed))
    # check num of classes by loading the utt2class file
    with open(opts.utt2class, 'r') as u2c_f:
        utt2class = json.load(u2c_f)
        num_classes = len(set(utt2class.values()))
        data_list = list(utt2class.keys())
    if opts.folds is not None:
        with open(opts.folds, 'r') as ffs:
            kfolds = json.load(ffs)
    else:
        kfolds = kfold_data(data_list, utt2class, opts.k_fold, opts.valid_p)
    for ki, kfold in enumerate(kfolds, start=1):
        train_list, valid_list, test_list = kfold
        model, dset = retrieve_model_and_datasets(opts.encoder_cfg,
                                                  opts.model_cfg,
                                                  opts.data_cfg,
                                                  train_list, valid_list,
                                                  test_list)
        model.describe_params()
        if opts.ckpt is not None:
            # load ckpt for the model on previous state
            model.load_pretrained(opts.ckpt)
        if num_devices > 1:
            model_dp = nn.DataParallel(model)
        else:
            model_dp = model
        model_dp.to(device)
        # Build DataLoaders: train, valid and test
        dloader = DataLoader(dset[0], opts.batch_size,
                             shuffle=True, 
                             pin_memory=CUDA)
        va_dloader = DataLoader(dset[1], 1, shuffle=False,
                                pin_memory=CUDA)
        save_path = os.path.join(opts.save_path, 'fold-{}'.format(ki))
        if not os.path.exists(save_path):
            os.makedirs(save_path)
        writer = SummaryWriter(save_path)
        # Build optimizer and scheduler
        if opts.opt == 'adam':
            opt = optim.Adam(model.parameters(), opts.lr)
        else:
            opt = optim.SGD(model.parameters(), opts.lr)
        sched = ReduceLROnPlateau(opt, 'max', factor=0.5, patience=3)
        best_val_acc = 0
        estop_pat = opts.early_stop_patience
        beg_t = timeit.default_timer()
        for giter in range(1, opts.iters + 1):
            X, Y = next(dloader.__iter__())
            X = X.unsqueeze(1)
            #X = random_slice_X(X)
            X = X.to(device)
            Y = Y.to(device)
            y = model_dp(X)
            loss = F.nll_loss(y, Y)
            loss.backward()
            acc = accuracy(y, Y)
            opt.step()
            opt.zero_grad()
            end_t = timeit.default_timer()
            if giter % opts.log_freq == 0:
                print('Iter {:5d}/{:5d} loss: {:.2f}, acc: {:.2f} '
                      'btime: {:.1f} s'.format(giter, opts.iters,
                                               loss.item(),
                                               acc,
                                               end_t - beg_t))
                writer.add_scalar('train/loss', loss.item(), giter)
                writer.add_scalar('train/acc', acc, giter)
            beg_t = timeit.default_timer()
            if giter % opts.save_freq == 0:
                val, acc = valid_round(va_dloader, model, writer, giter, device)
                sched.step(acc)
                if best_val_acc < acc:
                    best_val_acc = acc
                    estop_pat = opts.early_stop_patience
                    model.save(save_path,
                               giter, best_val=True)
                else:
                    estop_pat -= 1
                    if estop_pat <= 0:
                        print('BREAKING TRAINING LOOP AFTER {} VAL STEPS '
                              'WITHOUT '
                              'IMPROVEMENT'.format(opts.early_stop_patience))
                        break
                model.train()
        # TEST THIS FOLD AND STORE THE VALUE IN A LOG IN ROOT SAVE_PATH
        te_files = dset[2].split_list
        data_root = dset[2].data_root
        # TODO: Load last best ckpt
        best_ckpt = get_best_ckpt(save_path)
        if best_ckpt is None:
            print('ERROR in test: skipping this fold for '
                  'did not find a ckpt')
            continue
        print('Loading ckpt for test: ', best_ckpt)
        model.load_pretrained(best_ckpt, load_last=True)
        with torch.no_grad():
            model.eval()
            res = {}
            for test_file in te_files:
                lab = utt2class[test_file]
                wav, rate = sf.read(os.path.join(data_root, test_file))
                wav = torch.FloatTensor(wav).view(1, 1, -1)
                wav = wav.to(device)
                y = model(wav)
                res[test_file] = {'prediction':y.max(1, keepdim=True)[1].item(),
                                  'lab':lab}
            with open(os.path.join(save_path, 'predictions.json'), 'w') as f:
                f.write(json.dumps(res, indent=2))

def get_best_ckpt(load_path):
    ckpt_track = glob.glob(os.path.join(load_path, '*-checkpoints'))
    if len(ckpt_track) == 0:
        return None
    ckpt_track = ckpt_track[0]
    with open(ckpt_track, 'r') as f:
        ckpts = json.load(f)
        curr_ckpt = 'weights_{}'.format(ckpts['current'])
        return os.path.join(load_path, curr_ckpt)

def random_slice_X(X, lens=[32000, 96000]):
    sz = random.choice(list(range(lens[0], lens[1])))
    idxs = list(range(X.shape[-1] - sz))
    beg_i = random.choice(idxs)
    X = X[:, :, beg_i:beg_i + sz]
    return X


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data_root', type=str,
                        default='data/IEMOCAP_ahsn_leave-two-speaker-out')
    parser.add_argument('--ckpt', type=str, default=None)
    parser.add_argument('--k_fold', type=int, default=10,
                        help='Number of folds (Def: 10).')
    parser.add_argument('--folds', type=str, default=None,
                        help='JSON file containing the fold splits')
    parser.add_argument('--valid_split', type=float, default=0.1,
                        help='Validation split inside the training fold' \
                             ' (Def: 0.1).')
    parser.add_argument('--utt2class', type=str, 
                        default='data/utt2class.json')
    parser.add_argument('--batch_size', type=int, default=32)
    parser.add_argument('--iters', type=int, default=200000)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--no-cuda', action='store_true', default=False)
    parser.add_argument('--save_path', type=str, default='IEMOCAP_ckpt')
    parser.add_argument('--encoder_cfg', type=str, default=None)
    parser.add_argument('--model_cfg', type=str, default=None)
    parser.add_argument('--data_cfg', type=str, default=None)
    parser.add_argument('--log_freq', type=int, default=200)
    parser.add_argument('--save_freq', type=int, default=2500)
    parser.add_argument('--early_stop_patience', type=int, default=9)
    parser.add_argument('--lr', type=float, default=0.1)
    parser.add_argument('--valid_p', type=float, default=0.1)
    parser.add_argument('--opt', type=str, default='adam')
    parser.add_argument('--iter_sched', action='store_true', default=False)

    opts = parser.parse_args()

    main(opts)
