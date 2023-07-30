import configargparse
import cv2
import glob
import numpy as np
import os


def get_number_pictures(path):
    print(len(glob.glob(f"{path}/images/*.png")))
    return len(glob.glob(f"{path}/images/*.png"))

def get_bounds_for_pictures(path):
    near,far = [],[]
    image_files = os.listdir(f"{path}/depth/")
    print("N_image_files", len(image_files))
    sorted_image_files = sorted(image_files)
    print("N_sorted", len(sorted_image_files))
    for image_file in sorted_image_files:
        image_path = os.path.join(f"{path}/depth/",image_file)
        print("image_path", image_path)
        image = cv2.imread(image_path,cv2.IMREAD_GRAYSCALE)
        image_near = np.min(image)
        image_far = np.max(image)
        print("near,far", image_near, image_far )
        near = np.append(near,image_near)
        far = np.append(far,image_far)

    return near,far



""" 
The poses_bounds.npy calculation instructions can be found here https://github.com/Fyusion/LLFF#using-your-own-poses-without-running-colmap

The focal length is retrieved from the camera_calibration.txt for the left camera 
(Camera-0-F: 1080.36 1080.18 // left camera x,y focal dist in pixels) taking the mean from the x,y focal dist.
in EndoNeRF they assume that x,y dist is the same
"""
FOCAL = 1080.27
D = 17
HEIGHT = 1024
WIDTH = 1280

parser = configargparse.ArgumentParser()
parser.add_argument('--path', help='picture data path')
args = parser.parse_args()

path_to_pictures= args.path #"/dhc/home/<user_name>/EndoNeRF/data1/robotic_surgery_preprocessed"
number_pictures = get_number_pictures(path_to_pictures)

result=np.empty((number_pictures,D))

identity_matrix = np.eye(3,4)
camera_vector = np.array([WIDTH,HEIGHT,FOCAL]).reshape(-1,1)

matrix = np.concatenate((identity_matrix, camera_vector),axis=1)
flat = matrix.flatten()

# I checked that they use the min/max values from their depth maps as near/far bounds
near, far = get_bounds_for_pictures(path_to_pictures)

for n in range(number_pictures):
    result_vector = np.append(flat,[near[n],far[n]])
    result=np.vstack((result,result_vector))

np.save(f"{path_to_pictures}/poses_bounds.npy",result)
print("poses_bounds.npy saved to ",path_to_pictures")





