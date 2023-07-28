import numpy as np
import glob
import os
import cv2


def get_number_pictures(path):
    print(len(glob.glob(f"{path}/images/*.png")))
    return len(glob.glob(f"{path}/images/*.png"))

def get_bounds_for_pictures(path):
    """
    images = os.listdir(f"{path}/depth")
    extention = ".png"
    image_files = [images for images in images if any(images.lower().endswith(extention))]
    sorted_images = sorted(image_files)
    """
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


HEIGHT = 1024
WIDTH = 1280
FOCAL = 1080.27
D = 17

path_to_pictures= "/dhc/home/franziska.hradilak/EndoNeRF/data1/robotic_surgery_preprocessed"
number_pictures = get_number_pictures(path_to_pictures)

result=np.empty((number_pictures,D))

identity_matrix = np.eye(3,4)
camera_vector = np.array([WIDTH,HEIGHT,FOCAL]).reshape(-1,1)

matrix = np.concatenate((identity_matrix, camera_vector),axis=1)
flat = matrix.flatten()
near, far = get_bounds_for_pictures(path_to_pictures)
print(near)
print(near.size)
print(far)
for n in range(number_pictures):
    result_vector = np.append(flat,[near[n],far[n]])
    result=np.vstack((result,result_vector))

np.save(f"{path_to_pictures}/poses_bounds.npy",result)





