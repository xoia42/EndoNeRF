#!/bin/bash

## with this bash script, you can generate a bash file wich can perform
## bach processing for test images

data_dir='/dhc/home/<>/EndoNeRF/data1/robotic_surgery/right_images' # please input your private dir
imgs=`find ${data_dir} -name "*.png" |sort -g`
for img in ${imgs}; do
    echo ${img}
    filename=`basename ${img}`
    echo "python infer.py -c jshdr -i ${data_dir}/${filename} -r  '/dhc/home/<>/EndoNeRF/data1/robotic_surgery_preprocessed/right_images'" >> robotic_surgery_right_list_endonerf.sh
done
