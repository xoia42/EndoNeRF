
from PIL import Image
import glob
import os
import imageio

def get_concat_h(im1, im2):
    dst = Image.new('RGB', (im1.width + im2.width, im1.height))
    dst.paste(im1, (0, 0))
    dst.paste(im2, (im1.width, 0))
    return dst


data_dir1 ="/dhc/home/<>/EndoNeRF/logs/preprocessed_specularity_training/frames_preprocessed_specularity_training_fixidentity_100000_time/estim"
data_dir2 = "/dhc/home/<>/EndoNeRF/logs/preprocessed_specularity_training_gaussian/frames_preprocessed_specularity_training_gaussian_fixidentity_100000_time/estim"
save_dir = "/dhc/home/<>/EndoNeRF/comparison_videos/preprocessed_gaussian_pulling"
imgs_pre=[]
imgs=[]

def number(filename):
    #return int(filename[1::])
    filename = os.path.basename(filename)
    print(filename)
    
    number,ext = filename.split('.rgb')
    print(number)
    return(int(number))

def number2(filename):
    #return int(filename[1::])
    filename = os.path.basename(filename)
    #word,rest = filename.split('_')
    number,ext = filename.split('.rgb')
    return(int(number))
    
for filename in sorted(glob.glob(f'{data_dir1}/*.png'), key=number):
    print(filename)
    im=Image.open(filename)
    imgs_pre.append(im)
    print(im)

for filename in sorted(glob.glob(f'{data_dir2}/*.png'),key=number2):
    im=Image.open(filename)
    imgs.append(im)
    print(im)

new_imgs=[]

for img_pre,img in zip(imgs_pre,imgs):
    new_img = get_concat_h(img_pre,img)
    new_imgs.append(new_img)
    filename= os.path.join(save_dir,f'{number(img_pre.filename)}_{number2(img.filename)}.png')
    imageio.imwrite(filename,new_img)

imageio.mimwrite(f'{save_dir}.cutting.mp4',new_imgs,quality=8)