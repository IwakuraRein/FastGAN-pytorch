import torch
from torch import nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data.dataloader import DataLoader
from torchvision import transforms
from torchvision import utils as vutils

import argparse
import random
from tqdm import tqdm
import os

from models import weights_init, Discriminator, Generator
from operation import copy_G_params, load_params, get_dir
from operation import ImageFolder, InfiniteSamplerWrapper
from diffaug import DiffAugment, FakeAugment
import lpips


#torch.backends.cudnn.benchmark = True


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

def train_d(net, percept, data, label="real"):
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

    # percept = lpips.PerceptualLoss(model='net-lin', net='vgg', use_gpu=True)
    data_root = args.path
    total_iterations = args.iter
    checkpoint = args.ckpt
    batch_size = args.batch_size
    im_size = args.im_size
    enhence_policy = args.data_augment
    ndf = 64
    ngf = 64
    nz = 256
    nlr = 0.0002
    nbeta1 = 0.5
    use_cuda = True
    multi_gpu = args.multi_gpu
    dataloader_workers = 4
    current_iteration = 0
    save_interval = args.save_interval
    saved_model_folder, saved_image_folder = get_dir(args)
    
    device = torch.device("cpu")
    if use_cuda:
        device = torch.device("cuda")

    percept = lpips.LPIPS(net='vgg')
    percept.to(device)
    transform_list = [
            transforms.Resize((int(im_size),int(im_size))),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])
        ]
    trans = transforms.Compose(transform_list)
    
    if 'lmdb' in data_root:
        from operation import MultiResolutionDataset
        dataset = MultiResolutionDataset(data_root, trans, 1024)
    else:
        dataset = ImageFolder(root=data_root, transform=trans)

    dataloader = iter(DataLoader(dataset, batch_size=batch_size,
                      sampler=InfiniteSamplerWrapper(dataset), num_workers=dataloader_workers, pin_memory=True))
    '''
    loader = MultiEpochsDataLoader(dataset, batch_size=batch_size, 
                               shuffle=True, num_workers=dataloader_workers, 
                               pin_memory=True)
    dataloader = CudaDataLoader(loader, 'cuda')
    '''
    
    
    #from model_s import Generator, Discriminator
    netG = Generator(ngf=ngf, nz=nz, im_size=im_size)
    netG.apply(weights_init)

    netD = Discriminator(ndf=ndf, im_size=im_size)
    netD.apply(weights_init)

    netG.to(device)
    netD.to(device)

    avg_param_G = copy_G_params(netG)

    fixed_noise = torch.FloatTensor(32, nz).normal_(0, 1).to(device)

    optimizerG = optim.Adam(netG.parameters(), lr=nlr, betas=(nbeta1, 0.999))
    optimizerD = optim.Adam(netD.parameters(), lr=nlr, betas=(nbeta1, 0.999))
    
    if checkpoint != 'None':
        if '.pth' not in checkpoint:
            checkpoint = saved_model_folder+'/all_'+checkpoint+'.pth'
        ckpt = torch.load(checkpoint)

        # in my case the saved weights' keys don't match the network's defines. I had to drop the "module." in the keys.
        try:
            netG.load_state_dict(ckpt['g'])
        except RuntimeError as e:
            old_keys = list(ckpt['g'].keys())
            for i in old_keys:
                if i.startswith('module.'):
                    ckpt['g'][i.replace('module.', '')]=ckpt['g'].pop(i)
            netG.load_state_dict(ckpt['g'])
        try:
            netD.load_state_dict(ckpt['d'])
        except RuntimeError as e:
            old_keys = list(ckpt['d'].keys())
            for i in old_keys:
                if i.startswith('module.'):
                    ckpt['d'][i.replace('module.', '')]=ckpt['d'].pop(i)
            netD.load_state_dict(ckpt['d'])

        avg_param_G = ckpt['g_ema']
        optimizerG.load_state_dict(ckpt['opt_g'])
        optimizerD.load_state_dict(ckpt['opt_d'])
        current_iteration = int(checkpoint.split('_')[-1].split('.')[0])
        del ckpt
        
    if multi_gpu:
        netG = nn.DataParallel(netG.to(device))
        netD = nn.DataParallel(netD.to(device))
    

    with tqdm(total=total_iterations,initial=current_iteration) as pbar:
        for iteration in range(current_iteration, total_iterations+1):
            pbar.set_description('iter: {}/{}'.format(iteration + 1, total_iterations))
            real_image = next(dataloader).to(device)
            
            current_batch_size = real_image.size(0)
            noise = torch.Tensor(current_batch_size, nz).normal_(0, 1).to(device)

            fake_images = netG(noise)

            real_image = DiffAugment(real_image, policy=enhence_policy)
            fake_images = [DiffAugment(fake, policy=enhence_policy) for fake in fake_images]
            fake_images.extend([FakeAugment(real_image, policy=enhence_policy)])
            
            ## 2. train Discriminator
            netD.zero_grad()

            err_dr, rec_img_all, rec_img_small, rec_img_part = train_d(netD, percept, real_image, label="real")
            train_d(netD, percept, [fi.detach() for fi in fake_images], label="fake")
            optimizerD.step()
            
            ## 3. train Generator
            netG.zero_grad()
            pred_g = netD(fake_images, "fake")
            err_g = -pred_g.mean()

            err_g.backward()
            optimizerG.step()

            for p, avg_p in zip(netG.parameters(), avg_param_G):
                avg_p.mul_(0.999).add_(0.001 * p.data)

            pbar.set_postfix({"loss d": "%.5f"%(err_dr), "loss g": "%.5f"%(-err_g.item())})

            # if iteration % (save_interval) == 0:
            if iteration % 1000 == 0:
                backup_para = copy_G_params(netG)
                load_params(netG, avg_param_G)
                with torch.no_grad():
                    vutils.save_image(netG(fixed_noise)[0].add(1).mul(0.5), saved_image_folder+'/%d.jpg'%iteration, nrow=4)
                    vutils.save_image( torch.cat([
                            F.interpolate(real_image, 128), 
                            rec_img_all, rec_img_small,
                            rec_img_part]).add(1).mul(0.5), saved_image_folder+'/rec_%d.jpg'%iteration )
                load_params(netG, backup_para)

            if iteration != 0 and (iteration % (save_interval) == 0 or iteration == total_iterations):
                backup_para = copy_G_params(netG)
                load_params(netG, avg_param_G)
                torch.save({'g':netG.state_dict(),'d':netD.state_dict()}, saved_model_folder+'/%d.pth'%iteration)
                load_params(netG, backup_para)
                torch.save({'g':netG.state_dict(),
                            'd':netD.state_dict(),
                            'g_ema': avg_param_G,
                            'opt_g': optimizerG.state_dict(),
                            'opt_d': optimizerD.state_dict()}, saved_model_folder+'/all_%d.pth'%iteration)
            
            pbar.update(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='region gan')

    parser.add_argument('--path', type=str, default='../lmdbs/art_landscape_1k', help='path of resource dataset, should be a folder that has one or many sub image folders inside')
    parser.add_argument('--name', type=str, default='test1', help='experiment name')
    parser.add_argument('--iter', type=int, default=50000, help='number of iterations')
    parser.add_argument('--start_iter', type=int, default=0, help='the iteration to start training')
    parser.add_argument('--batch_size', type=int, default=8, help='mini batch number of images')
    parser.add_argument('--im_size', type=int, default=1024, help='image resolution')
    parser.add_argument('--ckpt', type=str, default='None', help='checkpoint weight path. can be either a number or "xx/xx.pth"')
    parser.add_argument('--multi_gpu', type=bool, default=False, help='wether use multiple gpus. you can specify gpus via "export CUDA_VISIBLE_DEVICE=0,1,2,3"')
    parser.add_argument('--save_interval', type=int, default=5000, help='how many iterations before saving a model')
    parser.add_argument('--data_augment', type=str, default='color,translation,flip,ghost', help='can be one or more of {color, translation, cutout, flip}')
    parser.add_argument('--save_path', type=str, default='train_results', help='path to save models and images')

    args = parser.parse_args()
    print(args)

    train(args)
