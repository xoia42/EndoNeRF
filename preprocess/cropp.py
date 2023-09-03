
from pylab import *
#import cv2
import imageio
import argparse
import glob
import os


parser = argparse.ArgumentParser()

parser.add_argument('--input_dir')
parser.add_argument('--output_dir')
args = parser.parse_args()

input_dir = args.input_dir
output_dir = args.output_dir

folder_dir =input_dir #"/dhc/home/<>/EndoNeRF/data1/robotic_surgery/images"

for image in sorted(glob.glob(f'{folder_dir}/*')):
    if(image.endswith(".png")):
        
        img = imageio.imread(image)
        old_h, old_w, _ = img.shape
        h_start,h_end, w_start,w_end = 37,1047, 328, 1592
        img = img[h_start: h_end, w_start:w_end]
        name = os.path.basename(image)
        imageio.imwrite(f"{output_dir}/{name}",img)
        print(name)
