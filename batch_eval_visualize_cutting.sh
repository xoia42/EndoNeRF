#!/bin/bash -eux
#SBATCH --job-name=cutting_titan
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=franziska.hradilak@student.hpi.de
#SBATCH --partition=gpu
#SBATCH --gpus=titan:1
#SBATCH --output=cutting_titan_%j.log

date
pwd
hostname -f
nproc
nvidia-smi

# Activate the desired Python environment (if necessary)
#conda activate endonerf3

# Change to the directory containing your Python script
cd EndoNeRF

# Run your Python command
python vis_pc.py --pc_dir logs/example_training/reconstructed_pcds_100000
