from pase.models.core import Waveminionet
from pase.models.modules import VQEMA
from pase.dataset import PairWavDataset, DictCollater
#from torchvision.transforms import Compose
from pase.transforms import *
from pase.losses import *
from pase.utils import pase_parser
from torch.utils.data import DataLoader
import torch
import pickle
import torch.nn as nn
import numpy as np
import argparse
import os
import json
import random


def make_transforms(opts, minions_cfg):
    trans = [ToTensor()]
    keys = ['totensor']
    # go through all minions first to check whether
    # there is MI or not to make chunker
    mi = False
    for minion in minions_cfg:
        if 'mi' in minion['name']:
            mi = True
    if mi:
        trans.append(MIChunkWav(opts.chunk_size, random_scale=opts.random_scale))
    else:
        trans.append(SingleChunkWav(opts.chunk_size, random_scale=opts.random_scale))

    znorm = False
    for minion in minions_cfg:
        name = minion['name']
        if name == 'mi' or name == 'cmi' or name == 'spc':
            continue
        elif name == 'lps':
            znorm = True
            trans.append(LPS(opts.nfft, hop=160, win=400))
        elif name == 'mfcc':
            znorm = True
            trans.append(MFCC(hop=160))
        elif name == 'prosody':
            znorm = True
            trans.append(Prosody(hop=160, win=400))
        elif name == 'chunk':
            znorm = True
        else:
            raise TypeError('Unrecognized module \"{}\"'
                            'whilst building transfromations'.format(name))
        keys.append(name)
    if znorm:
        trans.append(ZNorm(opts.stats))
        keys.append('znorm')
    if opts.trans_cache is None:
        trans = Compose(trans)
    else:
        trans = CachedCompose(trans, keys, opts.trans_cache)
    return trans


def train(opts):
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

    # ---------------------
    # Build Model
    if opts.fe_cfg is not None:
        with open(opts.fe_cfg, 'r') as fe_cfg_f:
            fe_cfg = json.load(fe_cfg_f)
            print(fe_cfg)
    else:
        fe_cfg = None
    minions_cfg = pase_parser(opts.net_cfg)
    make_transforms(opts, minions_cfg)
    model = Waveminionet(minions_cfg=minions_cfg,
                         adv_loss=opts.adv_loss,
                         num_devices=num_devices,
                         pretrained_ckpt=opts.pretrained_ckpt,
                         frontend_cfg=fe_cfg
                        )
    
    print(model)
    if opts.net_ckpt is not None:
        model.load_pretrained(opts.net_ckpt, load_last=True,
                              verbose=True)
    print('Frontend params: ', model.frontend.describe_params())
    model.to(device)
    trans = make_transforms(opts, minions_cfg)
    print(trans)
    # Build Dataset(s) and DataLoader(s)
    dset = PairWavDataset(opts.data_root, opts.data_cfg, 'train',
                         transform=trans,
                         preload_wav=opts.preload_wav)
    dloader = DataLoader(dset, batch_size=opts.batch_size,
                         shuffle=True, collate_fn=DictCollater(),
                         num_workers=opts.num_workers,
                         pin_memory=CUDA)
    # Compute estimation of bpe. As we sample chunks randomly, we
    # should say that an epoch happened after seeing at least as many
    # chunks as total_train_wav_dur // chunk_size
    bpe = (dset.total_wav_dur // opts.chunk_size) // opts.batch_size
    opts.bpe = bpe
    if opts.do_eval:
        va_dset = PairWavDataset(opts.data_root, opts.data_cfg,
                                 'valid', transform=trans,
                                 preload_wav=opts.preload_wav)
        va_dloader = DataLoader(va_dset, batch_size=opts.batch_size,
                                shuffle=False, collate_fn=DictCollater(),
                                num_workers=opts.num_workers,
                                pin_memory=CUDA)
        va_bpe = (va_dset.total_wav_dur // opts.chunk_size) // opts.batch_size
        opts.va_bpe = va_bpe
    else:
        va_dloader = None
    # fastet lr to MI
    #opts.min_lrs = {'mi':0.001}
    model.train_(dloader, vars(opts), device=device, va_dloader=va_dloader)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data_root', type=str, 
                        default='data/LibriSpeech/Librispeech_spkid_sel')
    parser.add_argument('--data_cfg', type=str, 
                        default='data/librispeech_data.cfg')
    parser.add_argument('--net_ckpt', type=str, default=None,
                        help='Ckpt to initialize the full network '
                             '(Def: None).')
    parser.add_argument('--net_cfg', type=str,
                        default=None)
    parser.add_argument('--fe_cfg', type=str, default=None)
    parser.add_argument('--do_eval', action='store_true', default=False)
    parser.add_argument('--stats', type=str, default='data/librispeech_stats.pkl')
    parser.add_argument('--pretrained_ckpt', type=str, default=None)
    parser.add_argument('--save_path', type=str, default='ckpt')
    parser.add_argument('--trans_cache', type=str,
                        default=None)
    parser.add_argument('--num_workers', type=int, default=2)
    parser.add_argument('--seed', type=int, default=2)
    parser.add_argument('--no-cuda', action='store_true', default=False)
    parser.add_argument('--random_scale', action='store_true', default=False)
    parser.add_argument('--chunk_size', type=int, default=16000)
    parser.add_argument('--log_freq', type=int, default=100)
    parser.add_argument('--epoch', type=int, default=1000)
    parser.add_argument('--nfft', type=int, default=2048)
    parser.add_argument('--batch_size', type=int, default=100)
    parser.add_argument('--hidden_size', type=int, default=256)
    parser.add_argument('--hidden_layers', type=int, default=2)
    parser.add_argument('--fe_opt', type=str, default='Adam')
    parser.add_argument('--min_opt', type=str, default='Adam')
    parser.add_argument('--lrdec_step', type=int, default=15,
                        help='Number of epochs to scale lr (Def: 15).')
    parser.add_argument('--lrdecay', type=float, default=0,
                        help='Learning rate decay factor with '
                             'cross validation. After patience '
                             'epochs, lr decays this amount in '
                             'all optimizers. ' 
                             'If zero, no decay is applied (Def: 0).')
    parser.add_argument('--dout', type=float, default=0.2)
    parser.add_argument('--fe_lr', type=float, default=0.0001)
    parser.add_argument('--min_lr', type=float, default=0.0004)
    parser.add_argument('--z_lr', type=float, default=0.0004)
    parser.add_argument('--rndmin_train', action='store_true',
                        default=False)
    parser.add_argument('--adv_loss', type=str, default='BCE',
                        help='BCE or L2')
    parser.add_argument('--warmup', type=int, default=1000000000,
                        help='Epoch to begin applying z adv '
                             '(Def: 1000000000 to not apply it).')
    parser.add_argument('--zinit_weight', type=float, default=1)
    parser.add_argument('--zinc', type=float, default=0.0002)
    parser.add_argument('--vq_K', type=int, default=50,
                        help='Number of K embeddings in VQ-enc. '
                             '(Def: 50).')
    parser.add_argument('--vq', action='store_true', default=False,
                        help='Do VQ quantization of enc output (Def: False).')
    parser.add_argument('--preload_wav', action='store_true', default=False,
                        help='Preload wav files in Dataset (Def: False).')

    opts = parser.parse_args()
    if opts.net_cfg is None:
        raise ValueError('Please specify a net_cfg file')

    if not os.path.exists(opts.save_path):
        os.makedirs(opts.save_path)


    with open(os.path.join(opts.save_path, 'train.opts'), 'w') as opts_f:
        opts_f.write(json.dumps(vars(opts), indent=2))
    train(opts)
