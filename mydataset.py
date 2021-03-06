import os
import random
import torch
import numpy as np
from torch.utils.data import Dataset
from PIL import Image
from image import *
import torchvision.transforms.functional as F


class ListDataset(Dataset):
    def __init__(self,
                 root: list,
                 shape=None,
                 transform=None,
                 train: bool = False,
                 seen: int = 0,
                 batch_size: int = 1,
                 num_workers: int = 20,
                 dataset: str = 'shanghai',
                 shuffle: bool = True):

        if train and dataset == 'shanghai':
            root = root * 4
        if shuffle:
            random.shuffle(root)

        self.nSamples = len(root)
        self.lines = root
        self.transform = transform
        self.train = train
        self.shape = shape
        self.seen = seen
        self.batch_size = batch_size
        self.num_workers = num_workers

        self.dataset = dataset

    def __len__(self):
        return self.nSamples

    def __getitem__(self, index: int):
        assert index <= len(self), 'index range error'

        img_path = self.lines[index]

        if self.dataset == 'ucf_test':
            # test in UCF data
            img, target = load_ucf_ori_data(img_path)
        else:
            # test in shanghai data
            img, target = load_shanghai_data(img_path, self.train)

        if self.transform is not None:
            img = self.transform(img)
        return img, target
