'''
Code adapted from: https://github.com/odegeasslbc/FastGAN-pytorch

'''

import os
import torch
from torch import nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data.dataloader import DataLoader
import torchvision
from torchvision import transforms
from torchvision import utils as vutils

import argparse
import random
from tqdm import tqdm

from models import weights_init, Discriminator, Generator
from operation import copy_G_params, load_params, get_dir, ImageFolder, InfiniteSamplerWrapper
from diffaug import DiffAugment
import lpips.utils as lpips
from utils import get_dataset_details

def crop_image_by_part(image, part):
    hw = image.shape[2]//2
    if part==0:
        return image[:,:,:hw,:hw]
    if part==1:
        return image[:,:,:hw,hw:]
    if part==2:
        return image[:,:,hw:,:hw]
    if part==3:
        return image[:,:,hw:,hw:]

def train_d(args, net, data, percept, label="real"):
    """Train function of discriminator"""
    if label=="real":
        part = random.randint(0, 3)
        pred, [rec_all, rec_small, rec_part] = net(data, label, part=part)

        err = F.relu(  torch.rand_like(pred) * 0.2 + 0.8 -  pred).mean() + \
            percept( rec_all, F.interpolate(data, rec_all.shape[2]) ).sum() +\
            percept( rec_small, F.interpolate(data, rec_small.shape[2]) ).sum() +\
            percept( rec_part, F.interpolate(crop_image_by_part(data, part), rec_part.shape[2]) ).sum()
        err.backward()
        return pred.mean().item(), rec_all, rec_small, rec_part
    else:
        pred = net(data, label)
        err = F.relu( torch.rand_like(pred) * 0.2 + 0.8 + pred).mean()
        err.backward()
        return pred.mean().item()
        

def train(args):
    total_iterations = args.iter
    batch_size = args.batch_size
    im_size = args.im_size
    ndf = 64
    ngf = 64
    nz = 256
    nlr = 0.0002
    nbeta1 = 0.5
    use_cuda = torch.cuda.is_available()
    multi_gpu = False
    dataloader_workers = 8
    current_iteration = args.start_iter
    save_interval = 1000
    saved_model_folder, saved_image_folder = get_dir(args)
    policy = 'color,translation'

    checkpoint = args.ckpt

    percept = lpips.PerceptualLoss(model='net-lin', net='vgg', model_path=args.ckpt, use_gpu=use_cuda)

    device = torch.device("cpu")
    if use_cuda:
        device = torch.device("cuda:0")

    transform_list = [
            transforms.Resize((int(im_size),int(im_size))),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])
        ]
    trans = transforms.Compose(transform_list)
    
    if 'lmdb' in args.path:
        from operation import MultiResolutionDataset
        dataset = MultiResolutionDataset(args.path, trans, 1024)
    else:
        dataset = ImageFolder(args.path, transform=trans)

   
    print(args.path, "length is", len(dataset), "model name is", args.model_name) 
    dataloader = iter(DataLoader(dataset, batch_size=batch_size, shuffle=False,
                      sampler=InfiniteSamplerWrapper(dataset), num_workers=dataloader_workers, pin_memory=True))
    
    #from model_s import Generator, Discriminator
    netG = Generator(ngf=ngf, nz=nz, im_size=im_size)
    netG.apply(weights_init)

    netD = Discriminator(ndf=ndf, im_size=im_size)
    netD.apply(weights_init)

    netG.to(device)
    netD.to(device)

    avg_param_G = copy_G_params(netG)
    
    optimizerG = optim.Adam(netG.parameters(), lr=nlr, betas=(nbeta1, 0.999))
    optimizerD = optim.Adam(netD.parameters(), lr=nlr, betas=(nbeta1, 0.999))

    if checkpoint is not None:
        print(f"Loading checkpoint from {checkpoint}")
        ckpt = torch.load(checkpoint)
        netG.load_state_dict({k.replace('module.', ''): v for k, v in ckpt['g'].items()}, strict=False)
        netD.load_state_dict({k.replace('module.', ''): v for k, v in ckpt['d'].items()}, strict=False)
        avg_param_G = ckpt['g_ema']
        optimizerG.load_state_dict(ckpt['opt_g'])
        optimizerD.load_state_dict(ckpt['opt_d'])
        del ckpt
        
    if multi_gpu:
        netG = nn.DataParallel(netG.to(device))
        netD = nn.DataParallel(netD.to(device))

    for iteration in tqdm(range(current_iteration, total_iterations+1)):
        real_image = next(dataloader)
        real_image = real_image.to(device)
        current_batch_size = real_image.size(0)
        noise = torch.Tensor(current_batch_size, nz).normal_(0, 1).to(device)

        fake_images = netG(noise)

        real_image = DiffAugment(real_image, policy=policy)
        fake_images = [DiffAugment(fake, policy=policy) for fake in fake_images]
        
        ## 2. train Discriminator
        netD.zero_grad()

        err_dr, rec_img_all, rec_img_small, rec_img_part = train_d(args, netD, real_image, percept, label="real")
        train_d(args, netD, [fi.detach() for fi in fake_images], percept, label="fake")
        optimizerD.step()
        
        ## 3. train Generator
        netG.zero_grad()
        pred_g = netD(fake_images, "fake")
        err_g = -pred_g.mean()

        err_g.backward()
        optimizerG.step()

        for p, avg_p in zip(netG.parameters(), avg_param_G):
            avg_p.mul_(0.999).add_(0.001 * p.data)

        if iteration % save_interval == 0:
            v = str(iteration) + " - GAN: loss d: %.5f    loss g: %.5f"%(err_dr, -err_g.item())
            print(str(v))
          
        if iteration % (save_interval*10) == 0:
            backup_para = copy_G_params(netG)
            load_params(netG, avg_param_G)
            load_params(netG, backup_para)

        if iteration > 0 and (iteration % (save_interval*50) == 0 or iteration == total_iterations):
            backup_para = copy_G_params(netG)
            load_params(netG, avg_param_G)
            # torch.save({'g':netG.state_dict(),'d':netD.state_dict()}, saved_model_folder+'/%d.pth'%iteration)
            load_params(netG, backup_para)
            torch.save({'g':netG.state_dict(),
                        'd':netD.state_dict(),
                        'g_ema': avg_param_G,
                        'opt_g': optimizerG.state_dict(),
                        'opt_d': optimizerD.state_dict()}, f'{saved_model_folder}/{args.model_name}')

def generate_images(args, images_path):
    ndf = 64
    ngf = 64
    nz = 256

    netG = Generator(ngf=ngf, nz=nz, im_size=args.im_size)
    netG.apply(weights_init)

    netD = Discriminator(ndf=ndf, im_size=args.im_size)
    netD.apply(weights_init)

    device = torch.device("cuda:0")
    netG.to(device)
    netD.to(device)

    model_path, _ = get_dir(args)

    print("Loading checkpoint")

    args.ckpt = f'{model_path}/{args.model_name}'
    ckpt = torch.load(args.ckpt)
    netG.load_state_dict({k.replace('module.', ''): v for k, v in ckpt['g'].items()})
    netD.load_state_dict({k.replace('module.', ''): v for k, v in ckpt['d'].items()})

    fixed_noise = torch.FloatTensor(25, nz).normal_(0, 1).to(device)#8 size of dataset to be generated

    print("Generating images...")

    for i, val in enumerate(netG(fixed_noise)[0].add(1).mul(0.5)):
        torchvision.utils.save_image(val, f'{images_path}/image_{iter}_{i}.jpg')

def do_gen_ai(args):
    parser = argparse.ArgumentParser(description='region gan')
    parser.add_argument('--path', type=str, default='imagenet_gan', help='path of resource dataset, should be a folder that has one or many sub image folders inside')
    parser.add_argument('--cuda', type=int, default=0, help='index of gpu to use')
    parser.add_argument('--model_name', type=str, default='test1', help='experiment name')
    parser.add_argument('--iter', type=int, default=50000, help='number of iterations')
    parser.add_argument('--start_iter', type=int, default=0, help='the iteration to start training')
    parser.add_argument('--batch_size', type=int, default=8, help='mini batch number of images')
    parser.add_argument('--im_size', type=int, default=1024, help='image resolution')
    parser.add_argument('--ckpt', type=str, default=None, help='checkpoint weight path if have one')
    gen_args = parser.parse_args()

    n_classes, ds_name, ds_path = get_dataset_details(args.target_dataset)

    gen_args.path = ds_path
    gen_args.model_name = f"{ds_name}_model.pth"

    train(gen_args)

    gen_images_path = os.path.join(args.dataset_dir, f'{args.gen_images_path}_{ds_name}')
    if not os.path.exists(gen_images_path):
        os.makedirs(gen_images_path)

    print(f"Generated images path {gen_images_path}")
    generate_images(gen_args, gen_images_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='fast-gan')
    parser.add_argument('--target_dataset', type=int, default=5, help='')
    parser.add_argument('--dataset_dir', type=str, default='./datasets', help='')
    parser.add_argument('--gen_images_path', type=str, default='generated', help='')
    args = parser.parse_args()

    do_gen_ai(args)
