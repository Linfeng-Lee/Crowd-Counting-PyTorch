"""
For training CSRNet teacher
"""
import numpy as np
import torch.nn as nn
from torchvision import models

_frontend_feat = [64, 64, 'M', 128, 128, 'M', 256, 256, 256, 'M', 512, 512, 512]
_backend_feat = [512, 512, 512, 256, 128, 64]


class CSRNet(nn.Module):
    def __init__(self, pretrained: bool = False):
        super(CSRNet, self).__init__()
        self.seen = 0

        self.frontend = _make_layers(_frontend_feat)
        # cal_para(self.frontend)
        self.backend = _make_layers(_backend_feat, in_channels=512, dilation=True)

        self.output_layer = nn.Conv2d(64, 1, kernel_size=(1, 1))

        if pretrained:
            self._initialize_weights(mode='normal')
            
            vgg = models.vgg16(pretrained)
            pretrain_keys = list(vgg.state_dict().keys())
            state_keys = list(self.frontend.state_dict().keys())

            # loading vgg pretrained weights
            for i in range(len(self.frontend.state_dict().items())):
                self.frontend.state_dict()[state_keys[i]].data = vgg.state_dict()[pretrain_keys[i]].data
        else:
            self._initialize_weights(mode='kaiming')

    def forward(self, x):
        # front relates to VGG
        x = self.frontend(x)
        # backend relates to dilated convolution
        x = self.backend(x)
        x = self.output_layer(x)
        return x

    def _initialize_weights(self, mode: str):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                if mode == 'normal':
                    nn.init.normal_(m.weight, std=0.01)
                elif mode == 'kaiming':
                    nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)


def _make_layers(cfg: list, in_channels: int = 3, batch_norm: bool = False, dilation: bool = False):
    d_rate = 2 if dilation else 1
    layers = []
    for v in cfg:
        if v == 'M':
            layers += [nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)]
        else:
            conv2d = nn.Conv2d(in_channels, v, kernel_size=(3, 3), padding=d_rate, dilation=(d_rate, d_rate))
            if batch_norm:
                layers += [conv2d, nn.BatchNorm2d(v), nn.ReLU(inplace=True)]
            else:
                layers += [conv2d, nn.ReLU(inplace=True)]
            in_channels = v
    return nn.Sequential(*layers)


if __name__ == '__main__':
    net = CSRNet()
    print(net)
    # with open('_vgg.txt', 'w') as f:
    #     f.write(str(net))
    # net_frontend_dict = net.frontend.state_dict()
    # net_backend_dict = net.backend.state_dict()
    # for k, v in net_frontend_dict.items():
    #     print(k, "-->", np.shape(v))
    # print()
    # for k, v in net_backend_dict.items():
    #     print(k, "-->", np.shape(v))
#