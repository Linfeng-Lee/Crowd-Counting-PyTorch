import sys
import os
import argparse
import json
import time

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
import torch.functional as F
from torch.autograd import Variable
from torchvision import datasets, transforms

import mydataset
from models.model_teacher_vgg import CSRNet as CSRNet_teacher
from models.model_student_vgg import CSRNet as CSRNet_student
from models.distillation import cosine_similarity, scale_process, cal_dense_fsp
from utils import save_checkpoint, cal_para
from utils import AverageMeter

parser = argparse.ArgumentParser(description='CSRNet-SKT distillation')
parser.add_argument('--train_json', metavar='TRAIN', default='./preprocess/A_train.json',
                    help='path to train json')
parser.add_argument('--val_json', metavar='VAL', default='./preprocess/A_val.json',
                    help='path to val json')
parser.add_argument('--test_json', metavar='TEST', default='./preprocess/A_test.json',
                    help='path to test json')

parser.add_argument('--lr', default=0.0004, type=float,
                    help='learning rate')

parser.add_argument('--teacher_ckpt', '-tc', default='./CSRNet_models_weights/partA_teacher.pth.tar', type=str,
                    help='teacher checkpoint')
parser.add_argument('--student_ckpt', '-sc', default='./CSRNet_models_weights/partA_student.pth.tar', type=str,
                    help='student checkpoint')

parser.add_argument('--lamb_fsp', '-laf', type=float, default=0.5,
                    help='weight of dense fsp loss')
parser.add_argument('--lamb_cos', '-lac', type=float, default=0.5,
                    help='weight of cos loss')
parser.add_argument('--gpu', metavar='GPU', type=str, default='0',
                    help='GPU id to use')
parser.add_argument('--out', metavar='OUTPUT', type=str, default='./save',
                    help='path to output')
parser.add_argument('--use_gpu', '-ug', type=bool, default=False,
                    help='use gpu training ot not')

args = parser.parse_args()

CUDA = True if args.use_gpu and torch.cuda.is_available() else False


def main(args):
    if CUDA:
        print('Use GPU Train')
    else:
        print("Not Use GPU Train")

    global mae_best_prec1, mse_best_prec1

    mae_best_prec1 = 1e6
    mse_best_prec1 = 1e6

    args.batch_size = 1  # args.batch
    args.momentum = 0.95
    args.decay = 5 * 1e-4
    args.start_epoch = 0
    args.epochs = 100
    args.workers = 0
    args.seed = time.time()
    args.print_freq = 400
    with open(args.train_json, 'r') as outfile:
        train_list = json.load(outfile)
    with open(args.val_json, 'r') as outfile:
        val_list = json.load(outfile)
    with open(args.test_json, 'r') as outfile:
        test_list = json.load(outfile)

    print('===Read Train Test Val json file===')

    if CUDA:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
        torch.cuda.manual_seed(int(args.seed))

    teacher = CSRNet_teacher()
    student = CSRNet_student(ratio=4)
    cal_para(student)  # include 1x1 conv transform parameters

    teacher.regist_hook()  # use hook to get teacher's features

    # if torch.cuda.is_available() and args.use_gpu:
    if CUDA:
        teacher = teacher.cuda()
        student = student.cuda()

    criterion = nn.MSELoss(size_average=False).cuda()

    optimizer = torch.optim.Adam(student.parameters(), args.lr, weight_decay=args.decay)

    if os.path.isdir(args.out) is False:
        os.makedirs(args.out.decode('utf-8'))

    if args.teacher_ckpt:
        if os.path.isfile(args.teacher_ckpt):
            print("=> loading checkpoint '{}'".format(args.teacher_ckpt))
            checkpoint = torch.load(args.teacher_ckpt)
            teacher.load_state_dict(checkpoint['state_dict'])

            print("=> loaded checkpoint '{}' (epoch {})".format(args.teacher_ckpt, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.teacher_ckpt))

    if args.student_ckpt:
        if os.path.isfile(args.student_ckpt):
            print("=> loading checkpoint '{}'".format(args.student_ckpt))
            checkpoint = torch.load(args.student_ckpt)
            args.start_epoch = checkpoint['epoch']
            if 'best_prec1' in checkpoint.keys():
                mae_best_prec1 = checkpoint['best_prec1']
            else:
                mae_best_prec1 = checkpoint['mae_best_prec1']

            if 'mse_best_prec1' in checkpoint.keys():
                mse_best_prec1 = checkpoint['mse_best_prec1']

            student.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])

            print("=> loaded checkpoint '{}' (epoch {})".format(args.student_ckpt, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.student_ckpt))

    for epoch in range(args.start_epoch, args.epochs):

        train(train_list, teacher, student, criterion, optimizer, epoch)
        mae_prec1, mse_prec1 = val(val_list, student)

        mae_is_best = mae_prec1 < mae_best_prec1
        mae_best_prec1 = min(mae_prec1, mae_best_prec1)
        mse_is_best = mse_prec1 < mse_best_prec1
        mse_best_prec1 = min(mse_prec1, mse_best_prec1)
        print('Best val * MAE {mae:.3f} * MSE {mse:.3f}'.format(mae=mae_best_prec1, mse=mse_best_prec1))
        save_checkpoint({
            'epoch': epoch + 1,
            'arch': args.student_ckpt,
            'state_dict': student.state_dict(),
            'mae_best_prec1': mae_best_prec1,
            'mse_best_prec1': mse_best_prec1,
            'optimizer': optimizer.state_dict(),
        }, mae_is_best, mse_is_best, args.out)

        if mae_is_best or mse_is_best:
            test(test_list, student)


def train(train_list: list, teacher, student, criterion, optimizer, epoch):
    losses_h = AverageMeter()
    losses_s = AverageMeter()
    losses_fsp = AverageMeter()
    losses_cos = AverageMeter()
    batch_time = AverageMeter()
    data_time = AverageMeter()

    transform = transforms.Compose([transforms.ToTensor(),
                                    transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                                         std=[0.229, 0.224, 0.225])])
    dataset = mydataset.ListDataset(train_list,
                                    transform=transform,
                                    train=True,
                                    seen=student.seen)

    train_loader = DataLoader(dataset,
                              num_workers=args.workers,
                              shuffle=True,
                              batch_size=args.batch_size)
    print('epoch %d, lr %.10f %s' % (epoch, args.lr, args.out))

    teacher.eval()
    student.train()
    end = time.time()

    for i, (img, target) in enumerate(train_loader):
        data_time.update(time.time() - end)

        img = img.cuda() if CUDA else img
        img = Variable(img)

        target = target.type(torch.FloatTensor)
        target = target.cuda() if CUDA else target
        target = Variable(target)

        with torch.no_grad():
            teacher_output = teacher(img)
            teacher.features.append(teacher_output)
            teacher_fsp_features = [scale_process(teacher.features)]
            teacher_fsp = cal_dense_fsp(teacher_fsp_features)

        student_features = student(img)
        student_output = student_features[-1]
        student_fsp_features = [scale_process(student_features)]
        student_fsp = cal_dense_fsp(student_fsp_features)

        loss_h = criterion(student_output, target)
        loss_s = criterion(student_output, teacher_output)

        loss_fsp = torch.tensor([0.], dtype=torch.float).cuda()
        if args.lamb_fsp:
            loss_f = []
            assert len(teacher_fsp) == len(student_fsp)
            for t in range(len(teacher_fsp)):
                loss_f.append(criterion(teacher_fsp[t], student_fsp[t]))
            loss_fsp = sum(loss_f) * args.lamb_fsp

        loss_cos = torch.tensor([0.], dtype=torch.float).cuda()
        if args.lamb_cos:
            loss_c = []
            for t in range(len(student_features) - 1):
                loss_c.append(cosine_similarity(student_features[t], teacher.features[t]))
            loss_cos = sum(loss_c) * args.lamb_cos

        loss = loss_h + loss_s + loss_fsp + loss_cos

        losses_h.update(loss_h.item(), img.size(0))
        losses_s.update(loss_s.item(), img.size(0))
        losses_fsp.update(loss_fsp.item(), img.size(0))
        losses_cos.update(loss_cos.item(), img.size(0))
        optimizer.zero_grad()
        torch.cuda.empty_cache()
        loss.backward()
        optimizer.step()
        batch_time.update(time.time() - end)
        end = time.time()
        if i % args.print_freq == (args.print_freq - 1):
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.avg:.3f}  '
                  'Data {data_time.avg:.3f}  '
                  'Loss_h {loss_h.avg:.4f}  '
                  'Loss_s {loss_s.avg:.4f}  '
                  'Loss_fsp {loss_fsp.avg:.4f}  '
                  'Loss_cos {loss_kl.avg:.4f}  '
                .format(
                epoch, i, len(train_loader), batch_time=batch_time,
                data_time=data_time, loss_h=losses_h, loss_s=losses_s,
                loss_fsp=losses_fsp, loss_kl=losses_cos))


def val(val_list: list, model):
    print('begin val')
    transform = transforms.Compose([transforms.ToTensor(),
                                    transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                                         std=[0.229, 0.224, 0.225])])
    dataset = mydataset.ListDataset(val_list,
                                    transform=transform,
                                    train=False)
    val_loader = DataLoader(dataset,
                            num_workers=args.workers,
                            shuffle=False,
                            batch_size=args.batch_size)

    model.eval()

    mae = 0
    mse = 0

    for i, (img, target) in enumerate(val_loader):
        # if torch.cuda.is_available() and args.use_gpu:
        img = img.cuda() if CUDA else img
        img = Variable(img)

        with torch.no_grad():
            output = model(img)

        target = target.sum().type(torch.FloatTensor)

        # if torch.cuda.is_available() and args.use_gpu:
        target = target.cuda() if CUDA else target

        mae += abs(output.data.sum() - target)
        mse += (output.data.sum() - target).pow(2)

    N = len(val_loader)
    mae = mae / N
    mse = torch.sqrt(mse / N)
    print('Val * MAE {mae:.3f} * MSE {mse:.3f}'.format(mae=mae, mse=mse))

    return mae, mse


def test(test_list: list, model):
    print('testing current model...')
    transform = transforms.Compose([transforms.ToTensor(),
                                    transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                                         std=[0.229, 0.224, 0.225]), ])
    dataset = mydataset.ListDataset(test_list,
                                    transform=transform, train=False)
    test_loader = DataLoader(dataset,
                             num_workers=args.workers,
                             shuffle=False,
                             batch_size=args.batch_size)

    model.eval()

    mae = 0
    mse = 0

    for i, (img, target) in enumerate(test_loader):
        # if torch.cuda.is_available() and args.use_gpu:
        img = img.cuda() if CUDA else img
        img = Variable(img)

        with torch.no_grad():
            output = model(img)

        target = target.sum().type(torch.FloatTensor)
        # if torch.cuda.is_available() and args.use_gpu:
        target = target.cuda if CUDA else target

        mae += abs(output.data.sum() - target)
        mse += (output.data.sum() - target).pow(2)

    N = len(test_loader)
    mae = mae / N
    mse = torch.sqrt(mse / N)
    print('Test * MAE {mae:.3f} * MSE {mse:.3f} '.format(mae=mae, mse=mse))


if __name__ == '__main__':
    main(args)
