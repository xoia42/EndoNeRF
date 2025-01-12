# Neural Rendering for Stereo 3D Reconstruction of Deformable Tissues in Robotic Surgery

This repo is based on the Implementation for MICCAI 2022 paper **[Neural Rendering for Stereo 3D Reconstruction of Deformable Tissues in Robotic Surgery](https://arxiv.org/abs/2206.15255)** by [Yuehao Wang](http://yuehaolab.com/), Yonghao Long, Siu Hin Fan, and [Qi Dou](http://www.cse.cuhk.edu.hk/~qdou/).
A NeRF-based framework for Stereo Endoscopic Surgery Scene Reconstruction (EndoNeRF).

**[\[Paper\]](https://arxiv.org/abs/2206.15255) [\[Website\]](https://med-air.github.io/EndoNeRF/) [\[Sample Dataset\]](https://forms.gle/1VAqDJTEgZduD6157)**

For more information on our adjustments see the README.pdf.

Adjustments we made can be found in the run_endonerf.py, rund_endonerf_helpers.py, load_llff, preprocessing folder and others. Our work focused on improving EndoNeRFs results in reconstrucction via:

+ Preprocessing: Specularity removal (https://github.com/fu123456/SHIQ)
+ Alteration of ray sampling: taking edges into account
+ Alteration of volume rendering functions from exp() to other (square etc.)

