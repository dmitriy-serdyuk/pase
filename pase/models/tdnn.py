import torch
import torch.nn as nn
import torch.nn.functional as F
try:
    from .modules import *
except ImportError:
    from modules import *


class StatisticalPooling(nn.Module):

    def forward(self, x):
        # x is 3-D with axis [B, feats, T]
        mu = x.mean(dim=2, keepdim=True)
        std = x.std(dim=2, keepdim=True)
        return torch.cat((mu, std), dim=1)

class TDNN(Model):
    # Architecture taken from x-vectors extractor
    # https://www.danielpovey.com/files/2018_icassp_xvectors.pdf
    def __init__(self, num_inputs, num_outputs, 
                 xvector=False,
                 name='TDNN'):
        super().__init__(name=name)
        self.model = nn.Sequential(
            nn.Conv1d(num_inputs, 512, 5, padding=2),
            nn.ReLU(inplace=True),
            nn.Conv1d(512, 512, 3, dilation=2, padding=2),
            nn.ReLU(inplace=True),
            nn.Conv1d(512, 512, 3, dilation=3, padding=3),
            nn.ReLU(inplace=True),
            nn.Conv1d(512, 512, 1),
            nn.ReLU(inplace=True),
            nn.Conv1d(512, 1500, 1),
            nn.ReLU(inplace=True),
            StatisticalPooling(),
            nn.Conv1d(3000, 512, 1),
            nn.ReLU(inplace=True),
            nn.Conv1d(512, 512, 1),
            nn.ReLU(inplace=True),
            nn.Conv1d(512, num_outputs, 1),
            nn.LogSoftmax(dim=1)
        )
        if xvector:
            # get output features at affine after stats pooling
            self.model = nn.Sequential(*list(self.model.children())[:-5])
        self.xvector = xvector

    def forward(self, x):
        return self.model(x)

    def load_pretrained(self, ckpt_path, verbose=True):
        if self.xvector:
            # remove last layers from dict first
            ckpt = torch.load(ckpt_path, 
                              map_location=lambda storage, loc: storage)
            sdict = ckpt['state_dict']
            curr_keys = list(dict(self.named_parameters()).keys())
            del_keys = [k for k in sdict.keys() if k not in curr_keys]
            # delete other keys from ckpt
            for k in del_keys:
                del sdict[k]
            # now load the weights remaining as feat extractor
            self.load_state_dict(sdict)
        else:
            # load everything
            super().load_pretrained(ckpt_path, load_last=True,
                                    verbose=verbose)

if __name__ == '__main__':
    sp = StatisticalPooling()
    x = torch.randn(1, 100, 1000)
    y = sp(x)
    print('y size: ', y.size())
    tdnn = TDNN(24, 1200, xvector=True)
    x = torch.randn(1, 24, 27000)
    y = tdnn(x)
    print('y size: ', y.size())
    tdnn.load_pretrained('/tmp/xvector.ckpt')
