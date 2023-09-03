
import glob

path_dataset= "/dhc/home/<>/EndoNeRF/data1/cutting_tissues_twice/images/*png"
path_pc = "/dhc/home/<>/EndoNeRF/logs/example_training/reconstructed_pcds_100000/*.ply"
pictures = sorted(glob.glob(path_dataset))
pointclouds = sorted(glob.glob(path_pc))

print(pictures)
print(pointclouds)
with open('config/cutting/og.txt','w') as file:
    for e,f in zip(pictures,pointclouds):
        file.write(e)
        file.write(" ")
        file.write(f)
        file.write('\n')

