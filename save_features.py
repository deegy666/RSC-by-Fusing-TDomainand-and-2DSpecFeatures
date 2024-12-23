from copy import deepcopy
import os
import sys
import json
import warnings
warnings.filterwarnings("ignore")

import math
import time
import random
import pickle
import argparse
import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
from torchvision import transforms

from util.icbhi_dataset import ICBHIDataset
from util.icbhi_util import get_score
from util.augmentation import SpecAugment
from util.misc import adjust_learning_rate, warmup_learning_rate, set_optimizer, update_moving_average
from util.misc import AverageMeter, accuracy, save_model, update_json
from models import get_backbone_class, Projector
from method import PatchMixLoss, PatchMixConLoss


def parse_args():
    parser = argparse.ArgumentParser('argument for supervised training')

    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--print_freq', type=int, default=10)
    parser.add_argument('--save_freq', type=int, default=100)
    parser.add_argument('--save_dir', type=str, default='./save/my_split/')
    parser.add_argument('--tag', type=str, default='bs8_lr5e-5_ep50_seed1',
                        help='tag for experiment name')
    parser.add_argument('--resume', type=str, default=None,
                        help='path of model checkpoint to resume')
    parser.add_argument('--eval', action='store_true',
                        help='only evaluation with pretrained encoder and classifier')
    parser.add_argument('--two_cls_eval', action='store_true',
                        help='evaluate with two classes')
    
    # optimization
    parser.add_argument('--optimizer', type=str, default='adam')
    parser.add_argument('--epochs', type=int, default=1)
    parser.add_argument('--learning_rate', type=float, default=5e-5)
    parser.add_argument('--lr_decay_epochs', type=str, default='120,160')
    parser.add_argument('--lr_decay_rate', type=float, default=0.1)
    parser.add_argument('--weight_decay', type=float, default=1e-6)
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--cosine', action='store_true',default=True,
                        help='using cosine annealing')
    parser.add_argument('--warm', action='store_true',
                        help='warm-up for large batch training')
    parser.add_argument('--warm_epochs', type=int, default=0,
                        help='warmup epochs')
    parser.add_argument('--weighted_loss', action='store_true',
                        help='weighted cross entropy loss (higher weights on abnormal class)')
    parser.add_argument('--mix_beta', default=1.0, type=float,
                        help='patch-mix interpolation coefficient')
    parser.add_argument('--time_domain', action='store_true',
                        help='patchmix for the specific time domain')

    # dataset
    parser.add_argument('--dataset', type=str, default='icbhi')
    parser.add_argument('--data_folder', type=str, default='./data/icbhi_dataset/audio_test_data/')
    parser.add_argument('--batch_size', type=int, default=1)
    parser.add_argument('--num_workers', type=int, default=8)
    # icbhi dataset
    parser.add_argument('--class_split', type=str, default='lungsound',
                        help='lungsound: (normal, crackles, wheezes, both), diagnosis: (healthy, chronic diseases, non-chronic diseases)')
    parser.add_argument('--n_cls', type=int, default=4,
                        help='set k-way classification problem')
    parser.add_argument('--test_fold', type=str, default='official', choices=['official', '0', '1', '2', '3', '4'],
                        help='test fold to use official 60-40 split or 80-20 split from RespireNet')
    parser.add_argument('--weighted_sampler', action='store_true',
                        help='weighted sampler inversly proportional to class ratio')
    parser.add_argument('--stetho_id', type=int, default=-1, 
                        help='stethoscope device id, use only when finetuning on each stethoscope data')
    parser.add_argument('--sample_rate', type=int,  default=16000, 
                        help='sampling rate when load audio data, and it denotes the number of samples per one second')
    parser.add_argument('--butterworth_filter', type=int, default=None, 
                        help='apply specific order butterworth band-pass filter')
    parser.add_argument('--desired_length', type=int,  default=8, 
                        help='fixed length size of individual cycle')
    parser.add_argument('--nfft', type=int, default=1024,
                        help='the frequency size of fast fourier transform')
    parser.add_argument('--n_mels', type=int, default=128,
                        help='the number of mel filter banks')
    parser.add_argument('--concat_aug_scale', type=float,  default=0, 
                        help='to control the number (scale) of concatenation-based augmented samples')
    parser.add_argument('--pad_types', type=str,  default='repeat', 
                        help='zero: zero-padding, repeat: padding with duplicated samples, aug: padding with augmented samples')
    parser.add_argument('--resz', type=float, default=1, 
                        help='resize the scale of mel-spectrogram')
    parser.add_argument('--raw_augment', type=int, default=0, 
                        help='control how many number of augmented raw audio samples')
    parser.add_argument('--blank_region_clip', action='store_true', 
                        help='remove the blank region, high frequency region')
    parser.add_argument('--specaug_policy', type=str, default='icbhi_ast_sup', 
                        help='policy (argument values) for SpecAugment')
    parser.add_argument('--specaug_mask', type=str, default='mean', 
                        help='specaug mask value', choices=['mean', 'zero'])

    # model
    parser.add_argument('--model', type=str, default='ast')
    parser.add_argument('--pretrained', action='store_true')
    parser.add_argument('--pretrained_ckpt', type=str, default='./save/16/icbhi_ast_ce_bs8_lr5e-5_ep50_seed1/best.pth',
                        help='path to pre-trained encoder model')
    parser.add_argument('--from_sl_official', action='store_true',default=True,
                        help='load from supervised imagenet-pretrained model (official PyTorch)')
    parser.add_argument('--ma_update', action='store_true',default=True,
                        help='whether to use moving average update for model')
    parser.add_argument('--ma_beta', type=float, default=0.5,
                        help='moving average value')
    parser.add_argument('--patch_size', type=int, default=16,
                        help='size of patches in Transformer')
    # for AST
    parser.add_argument('--audioset_pretrained', action='store_true',default=True,
                        help='load from imagenet- and audioset-pretrained model')
    # for SSAST
    parser.add_argument('--ssast_task', type=str, default='ft_avgtok', 
                        help='pretraining or fine-tuning task', choices=['ft_avgtok', 'ft_cls'])
    parser.add_argument('--fshape', type=int, default=16, 
                        help='fshape of SSAST')
    parser.add_argument('--tshape', type=int, default=16, 
                        help='tshape of SSAST')
    parser.add_argument('--ssast_pretrained_type', type=str, default='Patch', 
                        help='pretrained ckpt version of SSAST model')

    parser.add_argument('--method', type=str, default='ce')
    # Patch-Mix CL loss
    parser.add_argument('--proj_dim', type=int, default=768)
    parser.add_argument('--temperature', type=float, default=0.06)
    parser.add_argument('--alpha', type=float, default=1.0)
    parser.add_argument('--negative_pair', type=str, default='all',
                        help='the method for selecting negative pair', choices=['all', 'diff_label'])
    parser.add_argument('--target_type', type=str, default='grad_block',
                        help='how to make target representation', choices=['grad_block', 'grad_flow', 'project_block', 'project_flow'])

    args = parser.parse_args()

    iterations = args.lr_decay_epochs.split(',')
    args.lr_decay_epochs = list([])
    for it in iterations:
        args.lr_decay_epochs.append(int(it))
    
    args.model_name = '{}_{}_{}'.format(args.dataset, args.model, args.method)
    if args.tag:
        args.model_name += '_{}'.format(args.tag)

    if args.method in ['patchmix', 'patchmix_cl']:
        assert args.model in ['ast', 'ssast']
    
    args.save_folder = os.path.join(args.save_dir, args.model_name)
    if not os.path.isdir(args.save_folder):
        os.makedirs(args.save_folder)

    if args.warm:
        args.warmup_from = args.learning_rate * 0.1
        args.warm_epochs = 10
        if args.cosine:
            eta_min = args.learning_rate * (args.lr_decay_rate ** 3)
            args.warmup_to = eta_min + (args.learning_rate - eta_min) * (
                    1 + math.cos(math.pi * args.warm_epochs / args.epochs)) / 2
        else:
            args.warmup_to = args.learning_rate

    if args.dataset == 'icbhi':
        if args.class_split == 'lungsound':
            if args.n_cls == 4:
                args.cls_list = ['normal', 'crackle', 'wheeze', 'both']
            elif args.n_cls == 2:
                args.cls_list = ['normal', 'abnormal']
        elif args.class_split == 'diagnosis':
            if args.n_cls == 3:
                args.cls_list = ['healthy', 'chronic_diseases', 'non-chronic_diseases']
            elif args.n_cls == 2:
                args.cls_list = ['healthy', 'unhealthy']
    else:
        raise NotImplementedError

    return args


def set_loader(args):
    if args.dataset == 'icbhi':
        # get rawo information and calculate mean and std for normalization
        # dataset = ICBHIDataset(train_flag=True, transform=transforms.Compose([transforms.ToTensor()]), args=args, print_flag=False, mean_std=True)
        # mean, std = get_mean_and_std(dataset)
        # args.h, args.w = dataset.h, dataset.w

        # print('*' * 20)
        # print('[Raw dataset information]')
        # print('Stethoscope device number: {}, and patience number without overlap: {}'.format(len(dataset.device_to_id), len(set(sum(dataset.device_id_to_patient.values(), []))) ))
        # for device, id in dataset.device_to_id.items():
        #     print('Device {} ({}): {} number of patience'.format(id, device, len(dataset.device_id_to_patient[id])))
        # print('Spectrogram shpae on ICBHI dataset: {} (height) and {} (width)'.format(args.h, args.w))
        # print('Mean and std of ICBHI dataset: {} (mean) and {} (std)'.format(round(mean.item(), 2), round(std.item(), 2)))
        
        args.h, args.w = 1024, 256
        train_transform = [transforms.ToTensor(),
                            SpecAugment(args),
                            transforms.Resize(size=(int(args.h * args.resz), int(args.w * args.resz)))]
        val_transform = [transforms.ToTensor(),
                        transforms.Resize(size=(int(args.h * args.resz), int(args.w * args.resz)))]                        
        # train_transform.append(transforms.Normalize(mean=mean, std=std))
        # val_transform.append(transforms.Normalize(mean=mean, std=std))
        
        train_transform = transforms.Compose(train_transform)
        val_transform = transforms.Compose(val_transform)

        train_dataset = ICBHIDataset(train_flag=True, transform=train_transform, args=args, print_flag=True)
        val_dataset = ICBHIDataset(train_flag=False, transform=val_transform, args=args, print_flag=True)

        # for weighted_loss
        args.class_nums = train_dataset.class_nums
    else:
        raise NotImplemented    
    
    if args.weighted_sampler:
        reciprocal_weights = []
        for idx in range(len(train_dataset)):
            reciprocal_weights.append(train_dataset.class_ratio[train_dataset.labels[idx]])
        weights = (1 / torch.Tensor(reciprocal_weights))
        sampler = torch.utils.data.sampler.WeightedRandomSampler(weights, len(train_dataset))
    else:
        sampler = None

    train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=args.batch_size, shuffle=sampler is None,
                                               num_workers=args.num_workers, pin_memory=True, sampler=sampler, drop_last=True)
    val_loader = torch.utils.data.DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False,
                                             num_workers=args.num_workers, pin_memory=True, sampler=None)

    return train_loader, val_loader, args


def set_model(args):    
    kwargs = {}
    if args.model == 'ast':
        kwargs['input_fdim'] = int(args.h * args.resz)
        kwargs['input_tdim'] = int(args.w * args.resz)
        kwargs['label_dim'] = args.n_cls
        kwargs['imagenet_pretrain'] = args.from_sl_official
        kwargs['audioset_pretrain'] = args.audioset_pretrained
        kwargs['mix_beta'] = args.mix_beta  # for Patch-MixCL
    elif args.model == 'ssast':
        kwargs['label_dim'] = args.n_cls
        kwargs['fshape'], kwargs['tshape'] = args.fshape, args.tshape
        kwargs['fstride'], kwargs['tstride'] = 10, 10
        kwargs['input_tdim'] = 798
        kwargs['task'] = args.ssast_task
        kwargs['pretrain_stage'] = not args.audioset_pretrained
        kwargs['load_pretrained_mdl_path'] = args.ssast_pretrained_type
        kwargs['mix_beta'] = args.mix_beta  # for Patch-MixCL

    model = get_backbone_class(args.model)(**kwargs)    
    classifier = nn.Sequential()

    if not args.weighted_loss:
        weights = None
        criterion = nn.CrossEntropyLoss()
    else:
        weights = torch.tensor(args.class_nums, dtype=torch.float32)
        weights = 1.0 / (weights / weights.sum())
        weights /= weights.sum()
        
        criterion = nn.CrossEntropyLoss(weight=weights)

    if args.model not in ['ast', 'ssast'] and args.from_sl_official:
        model.load_sl_official_weights()
        print('pretrained model loaded from PyTorch ImageNet-pretrained')

    # load SSL pretrained checkpoint for linear evaluation
    if args.pretrained and args.pretrained_ckpt is not None:
        ckpt = torch.load(args.pretrained_ckpt, map_location='cpu')
        state_dict = ckpt['model']

        # HOTFIX: always use dataparallel during SSL pretraining
        new_state_dict = {}
        for k, v in state_dict.items():
            if "module." in k:
                k = k.replace("module.", "")
            if "backbone." in k:
                k = k.replace("backbone.", "")

            new_state_dict[k] = v
        state_dict = new_state_dict

        # if ckpt.get('classifier', None) is not None:
        #     classifier.load_state_dict(ckpt['classifier'], strict=True)

        print('pretrained model loaded from: {}'.format(args.pretrained_ckpt))
        print(model.load_state_dict(state_dict, strict=False))

    projector = Projector(model.final_feat_dim, args.proj_dim) if args.method == 'patchmix_cl' else nn.Identity()

    if args.method == 'ce':
        criterion = [criterion.cuda()]
    elif args.method == 'patchmix':
        criterion = [criterion.cuda(), PatchMixLoss(criterion=criterion).cuda()]
    elif args.method == 'patchmix_cl':
        criterion = [criterion.cuda(), PatchMixConLoss(temperature=args.temperature).cuda()]

    if torch.cuda.device_count() > 1:
        model = torch.nn.DataParallel(model)
        
    model.cuda()
    classifier.cuda()
    projector.cuda()
    
    optim_params = list(model.parameters()) + list(classifier.parameters()) + list(projector.parameters())
    optimizer = set_optimizer(args, optim_params)

    return model, classifier, projector, criterion, optimizer


def main():
    args = parse_args()
    with open(os.path.join(args.save_folder, 'train_args.json'), 'w') as f:
        json.dump(vars(args), f, indent=4)

    # fix seed
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    cudnn.deterministic = True
    cudnn.benchmark = True
    
    best_model = None
    if args.dataset == 'icbhi':
        best_acc = [0, 0, 0]  # Specificity, Sensitivity, Score

    train_loader, val_loader, args = set_loader(args)
    model, classifier, projector, criterion, optimizer = set_model(args)

    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch'] 
            model.load_state_dict(checkpoint['model'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            args.start_epoch += 1
            print("=> loaded checkpoint '{}' (epoch {})".format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))
    else:
        args.start_epoch = 1

    # use mix_precision:
    scaler = torch.cuda.amp.GradScaler()
    
    print('*' * 20)

    # train for one epoch
    for idx, (images,labels, audio) in enumerate(train_loader):

        images = images.cuda()
        labels = labels.cuda()
        audio = audio.cuda()
        features = model(images,audio)
        save_dir = f"./features_train/{args.patch_size}/"
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        torch.save(features, f"{save_dir}/feature_train_{str(idx).zfill(5)}.pth")
        torch.save(labels, f"{save_dir}/label_train_{str(idx).zfill(5)}.pth")

    
    # eval for one epoch
    for idx, (images,labels,audio) in enumerate(val_loader):

        images = images.cuda()
        labels = labels.cuda()
        audio = audio.cuda()
        features = model(images,audio)
        save_dir = f"./features_test/{args.patch_size}/"
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        torch.save(features, f"{save_dir}/feature_test_{str(idx).zfill(5)}.pth")
        torch.save(labels, f"{save_dir}/label_test_{str(idx).zfill(5)}.pth")




if __name__ == '__main__':
    main()
