import os
import imageio
import time
import math
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm, trange


from run_endonerf_helpers import *

from load_blender import load_blender_data
from load_llff import load_llff_data
try:
    from apex import amp
except ImportError:
    pass

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
np.random.seed(0)
DEBUG = True


def batchify(fn, chunk):
    """Constructs a version of 'fn' that applies to smaller batches.
    """
    if chunk is None:
        return fn
    def ret(inputs_pos, inputs_time):
        num_batches = inputs_pos.shape[0]

        out_list = []
        dx_list = []
        for i in range(0, num_batches, chunk):
            out, dx = fn(inputs_pos[i:i+chunk], [inputs_time[0][i:i+chunk], inputs_time[1][i:i+chunk]])
            out_list += [out]
            dx_list += [dx]

        return torch.cat(out_list, 0), torch.cat(dx_list, 0)
    return ret


def run_network(inputs, viewdirs, frame_time, fn, embed_fn, embeddirs_fn, embedtime_fn, netchunk=1024*64,
                embd_time_discr=True):
    """Prepares inputs and applies network 'fn'.
    inputs: N_rays x N_points_per_ray x 3
    viewdirs: N_rays x 3
    frame_time: N_rays x 1
    """

    assert len(torch.unique(frame_time)) == 1, "Only accepts all points from same time"
    cur_time = torch.unique(frame_time)[0]

    # embed position
    inputs_flat = torch.reshape(inputs, [-1, inputs.shape[-1]])
    embedded = embed_fn(inputs_flat)

    # embed time
    if embd_time_discr:
        B, N, _ = inputs.shape
        input_frame_time = frame_time[:, None].expand([B, N, 1])
        input_frame_time_flat = torch.reshape(input_frame_time, [-1, 1])
        embedded_time = embedtime_fn(input_frame_time_flat)
        embedded_times = [embedded_time, embedded_time]

    else:
        assert NotImplementedError

    # embed views
    if viewdirs is not None:
        input_dirs = viewdirs[:,None].expand(inputs.shape)
        input_dirs_flat = torch.reshape(input_dirs, [-1, input_dirs.shape[-1]])
        embedded_dirs = embeddirs_fn(input_dirs_flat)
        embedded = torch.cat([embedded, embedded_dirs], -1)

    outputs_flat, position_delta_flat = batchify(fn, netchunk)(embedded, embedded_times)
    outputs = torch.reshape(outputs_flat, list(inputs.shape[:-1]) + [outputs_flat.shape[-1]])
    position_delta = torch.reshape(position_delta_flat, list(inputs.shape[:-1]) + [position_delta_flat.shape[-1]])

    return outputs, position_delta


def batchify_rays(rays_flat,volumetric_function, chunk=1024*32, **kwargs):
    """Render rays in smaller minibatches to avoid OOM.
    """

    # if (torch.isnan(rays_flat).any() or torch.isinf(rays_flat).any()) and DEBUG:
    #     print(f"! [Numerical Error] rays_flat contains nan or inf.", flush=True)
    
    all_ret = {}
    for i in range(0, rays_flat.shape[0], chunk):
        ret = render_rays(rays_flat[i:i+chunk],volumetric_function=volumetric_function, **kwargs)
        for k in ret:
            if k not in all_ret:
                all_ret[k] = []
            all_ret[k].append(ret[k])

    all_ret = {k : torch.cat(all_ret[k], 0) for k in all_ret}
    return all_ret


def render(H, W, focal, volumetric_function, chunk=1024*32, rays=None, c2w=None, ndc=True,
                  near=0., far=1., frame_time=None,
                  use_viewdirs=False, c2w_staticcam=None,
                  **kwargs):
    """Render rays
    Args:
      H: int. Height of image in pixels.
      W: int. Width of image in pixels.
      focal: float. Focal length of pinhole camera.
      chunk: int. Maximum number of rays to process simultaneously. Used to
        control maximum memory usage. Does not affect final results.
      rays: array of shape [2, batch_size, 3]. Ray origin and direction for
        each example in batch.
      c2w: array of shape [3, 4]. Camera-to-world transformation matrix.
      ndc: bool. If True, represent ray origin, direction in NDC coordinates.
      near: float or array of shape [batch_size]. Nearest distance for a ray.
      far: float or array of shape [batch_size]. Farthest distance for a ray.
      use_viewdirs: bool. If True, use viewing direction of a point in space in model.
      c2w_staticcam: array of shape [3, 4]. If not None, use this transformation matrix for 
       camera while using other c2w argument for viewing directions.
    Returns:
      rgb_map: [batch_size, 3]. Predicted RGB values for rays.
      disp_map: [batch_size]. Disparity map. Inverse of depth.
      acc_map: [batch_size]. Accumulated opacity (alpha) along a ray.
      extras: dict with everything returned by render_rays().
    """
    
    if c2w is not None:
        # special case to render full image
        rays_o, rays_d = get_rays(H, W, focal, c2w)
    else:
        # use provided ray batch
        rays_o, rays_d = rays

    # if (torch.isnan(rays_o).any() or torch.isinf(rays_o).any()) and DEBUG:
    #     print(f"! [Numerical Error] rays_o in render 1 contains nan or inf.", flush=True)
    # if (torch.isnan(rays_d).any() or torch.isinf(rays_d).any()) and DEBUG:
    #     print(f"! [Numerical Error] rays_d in render 1 contains nan or inf.", flush=True)

    if use_viewdirs:
        # provide ray directions as input
        viewdirs = rays_d
        if c2w_staticcam is not None:
            # special case to visualize effect of viewdirs
            rays_o, rays_d = get_rays(H, W, focal, c2w_staticcam)
        viewdirs = viewdirs / (torch.norm(viewdirs, dim=-1, keepdim=True) + 1e-6)
        viewdirs = torch.reshape(viewdirs, [-1,3]).float()

    sh = rays_d.shape # [..., 3]
    if ndc:
        # for forward facing scenes
        rays_o, rays_d = ndc_rays(H, W, focal, 1., rays_o, rays_d)

    # if (torch.isnan(rays_o).any() or torch.isinf(rays_o).any()) and DEBUG:
    #     print(f"! [Numerical Error] rays_o in render 2 contains nan or inf.", flush=True)
    # if (torch.isnan(rays_d).any() or torch.isinf(rays_d).any()) and DEBUG:
    #     print(f"! [Numerical Error] rays_d in render 2 contains nan or inf.", flush=True)
    

    # Create ray batch
    rays_o = torch.reshape(rays_o, [-1,3]).float()
    rays_d = torch.reshape(rays_d, [-1,3]).float()

    if 'use_depth' in kwargs and kwargs['use_depth']:
        # near is the mean of depth, far is the std of depth
        near = near.unsqueeze(0).reshape(-1, 1)
        far = far * torch.ones_like(near)
    else:
        near, far = near * torch.ones_like(rays_d[...,:1]), far * torch.ones_like(rays_d[...,:1])
    frame_time = frame_time * torch.ones_like(rays_d[...,:1])
    rays = torch.cat([rays_o, rays_d, near, far, frame_time], -1)
    if use_viewdirs:
        rays = torch.cat([rays, viewdirs], -1)

    # Render and reshape
    all_ret = batchify_rays(rays,volumetric_function, chunk, **kwargs)
    for k in all_ret:
        k_sh = list(sh[:-1]) + list(all_ret[k].shape[1:])
        all_ret[k] = torch.reshape(all_ret[k], k_sh)

    k_extract = ['rgb_map', 'disp_map', 'acc_map']
    ret_list = [all_ret[k] for k in k_extract]
    ret_dict = {k : all_ret[k] for k in all_ret if k not in k_extract}
    return ret_list + [ret_dict]


def render_path(render_poses, render_times, hwf, chunk, volumetric_function, render_kwargs, gt_imgs=None, savedir=None,
                render_factor=0, save_also_gt=False, i_offset=0, save_depth=False, near_far=(0, 1)):

    H, W, focal = hwf

    if render_factor!=0:
        # Render downsampled for speed
        H = H//render_factor
        W = W//render_factor
        focal = focal/render_factor

    if savedir is not None:
        save_dir_estim = os.path.join(savedir, "estim")
        save_dir_gt = os.path.join(savedir, "gt")
        if not os.path.exists(save_dir_estim):
            os.makedirs(save_dir_estim)
        if save_also_gt and not os.path.exists(save_dir_gt):
            os.makedirs(save_dir_gt)

    rgbs = []
    disps = []

    for i, (c2w, frame_time) in enumerate(zip(tqdm(render_poses), render_times)):
        rgb, disp, acc, _ = render(H, W, focal, chunk=chunk, volumetric_function=volumetric_function,c2w=c2w[:3,:4], frame_time=frame_time, **render_kwargs)
        rgbs.append(rgb.cpu().numpy())
        disps.append(disp.cpu().numpy())

        if savedir is not None:
            rgb8_estim = to8b(rgbs[-1])
            filename = os.path.join(save_dir_estim, '{:03d}.rgb.png'.format(i+i_offset))
            imageio.imwrite(filename, rgb8_estim)
            
            if save_also_gt:
                rgb8_gt = to8b(gt_imgs[i])
                filename = os.path.join(save_dir_gt, '{:03d}.rgb.png'.format(i+i_offset))
                imageio.imwrite(filename, rgb8_gt)
            
            if save_depth:
                depth_estim = (1.0 / (disps[-1] + 1e-6)) * (near_far[1] - near_far[0])
                filename = os.path.join(save_dir_estim, '{:03d}.depth.npy'.format(i+i_offset))
                np.save(filename, depth_estim)
    
    rgbs = np.stack(rgbs, 0)
    disps = np.stack(disps, 0)

    # depth_maps = 1.0 / (disps + 1e-6)
    # close_depth, inf_depth = np.percentile(depth_maps, 3.0), np.percentile(depth_maps, 99.0)
    # if save_depth:
    #     for i, depth in enumerate(depth_maps):
    #         depth8_estim = to8b(depth / ((inf_depth - close_depth) + 1e-6))
    #         filename = os.path.join(save_dir_estim, '{:03d}.depth.png'.format(i+i_offset))
    #         imageio.imwrite(filename, depth8_estim)

    return rgbs, disps

def render_path_gpu(render_poses, render_times, hwf, chunk, render_kwargs, render_factor=0):

    H, W, focal = hwf

    if render_factor!=0:
        # Render downsampled for speed
        H = H//render_factor
        W = W//render_factor
        focal = focal/render_factor

    rgbs = []
    disps = []

    for i, (c2w, frame_time) in enumerate(zip(tqdm(render_poses), render_times)):
        rgb, disp, _, _ = render(H, W, focal, chunk=chunk, c2w=c2w[:3,:4], frame_time=frame_time, **render_kwargs)
        rgbs.append(rgb)
        disps.append(disp)

    rgbs = torch.stack(rgbs, 0)
    disps = torch.stack(disps, 0)

    return rgbs, disps


def create_nerf(args):
    """Instantiate NeRF's MLP model.
    """
    embed_fn, input_ch = get_embedder(args.multires, 3, args.i_embed)
    embedtime_fn, input_ch_time = get_embedder(args.multires, 1, args.i_embed)

    input_ch_views = 0
    embeddirs_fn = None
    if args.use_viewdirs:
        embeddirs_fn, input_ch_views = get_embedder(args.multires_views, 3, args.i_embed)

    output_ch = 5 if args.N_importance > 0 else 4
    skips = [args.netdepth // 2]
    model = NeRF.get_by_name(args.nerf_type, D=args.netdepth, W=args.netwidth,
                 input_ch=input_ch, output_ch=output_ch, skips=skips,
                 input_ch_views=input_ch_views, input_ch_time=input_ch_time,
                 use_viewdirs=args.use_viewdirs, embed_fn=embed_fn, embedtime_fn=embedtime_fn,
                 zero_canonical=not args.not_zero_canonical, time_window_size=args.time_window_size, time_interval=args.time_interval).to(device)
    grad_vars = list(model.parameters())

    model_fine = None
    if args.use_two_models_for_fine:
        model_fine = NeRF.get_by_name(args.nerf_type, D=args.netdepth_fine, W=args.netwidth_fine,
                          input_ch=input_ch, output_ch=output_ch, skips=skips,
                          input_ch_views=input_ch_views, input_ch_time=input_ch_time,
                          use_viewdirs=args.use_viewdirs, embed_fn=embed_fn, embedtime_fn=embedtime_fn,
                          zero_canonical=not args.not_zero_canonical, time_window_size=args.time_window_size, time_interval=args.time_interval).to(device)
        grad_vars += list(model_fine.parameters())

    network_query_fn = lambda inputs, viewdirs, ts, network_fn : run_network(inputs, viewdirs, ts, network_fn,
                                                                embed_fn=embed_fn,
                                                                embeddirs_fn=embeddirs_fn,
                                                                embedtime_fn=embedtime_fn,
                                                                netchunk=args.netchunk,
                                                                embd_time_discr=args.nerf_type!="temporal")

    # Create optimizer
    optimizer = torch.optim.Adam(params=grad_vars, lr=args.lrate, betas=(0.9, 0.999))

    if args.do_half_precision:
        print("Run model at half precision")
        if model_fine is not None:
            [model, model_fine], optimizers = amp.initialize([model, model_fine], optimizer, opt_level='O1')
        else:
            model, optimizers = amp.initialize(model, optimizer, opt_level='O1')

    # Extras
    extras = {
        'depth_maps': None,
        'ray_importance_maps': None
    }

    start = 0
    basedir = args.basedir
    expname = args.expname

    ##########################

    # Load checkpoints
    if args.ft_path is not None and args.ft_path!='None':
        ckpts = [args.ft_path]
    else:
        ckpts = [os.path.join(basedir, expname, f) for f in sorted(os.listdir(os.path.join(basedir, expname))) if 'tar' in f]

    print('Found ckpts', ckpts)
    if len(ckpts) > 0 and not args.no_reload:
        ckpt_path = ckpts[-1]
        print('Reloading from', ckpt_path)
        ckpt = torch.load(ckpt_path)

        start = ckpt['global_step'] + 1
        optimizer.load_state_dict(ckpt['optimizer_state_dict'])

        # Load model
        model.load_state_dict(ckpt['network_fn_state_dict'])
        if model_fine is not None:
            model_fine.load_state_dict(ckpt['network_fine_state_dict'])
        if args.do_half_precision:
            amp.load_state_dict(ckpt['amp'])

        # Load extras
        if 'depth_maps' in ckpt:
            extras['depth_maps'] = ckpt['depth_maps']
        if 'ray_importance_maps' in ckpt:
            extras['ray_importance_maps'] = ckpt['ray_importance_maps']


    ##########################

    render_kwargs_train = {
        'network_query_fn' : network_query_fn,
        'perturb' : args.perturb,
        'N_importance' : args.N_importance,
        'network_fine': model_fine,
        'N_samples' : args.N_samples,
        'network_fn' : model,
        'use_viewdirs' : args.use_viewdirs,
        'white_bkgd' : args.white_bkgd,
        'raw_noise_std' : args.raw_noise_std,
        'use_two_models_for_fine' : args.use_two_models_for_fine,
    }

    # NDC only good for LLFF-style forward facing data
    if args.dataset_type != 'llff' or args.no_ndc:
        render_kwargs_train['ndc'] = False
        render_kwargs_train['lindisp'] = args.lindisp

    render_kwargs_test = {k : render_kwargs_train[k] for k in render_kwargs_train}
    render_kwargs_test['perturb'] = False
    render_kwargs_test['raw_noise_std'] = 0.

    if args.use_depth and not args.no_depth_sampling:
        render_kwargs_train['use_depth'] = True

    return render_kwargs_train, render_kwargs_test, start, grad_vars, optimizer, extras


def raw2outputs(raw, z_vals, rays_d, raw_noise_std=0, white_bkgd=False, pytest=False, volumetric_function="exp"):
    """Transforms model's predictions to semantically meaningful values.
    Args:
        raw: [num_rays, num_samples along ray, 4]. Prediction from model.
        z_vals: [num_rays, num_samples along ray]. Integration time.
        rays_d: [num_rays, 3]. Direction of each ray.
    Returns:
        rgb_map: [num_rays, 3]. Estimated RGB color of a ray.
        disp_map: [num_rays]. Disparity map. Inverse of depth map.
        acc_map: [num_rays]. Sum of weights along each ray.
        weights: [num_rays, num_samples]. Weights assigned to each sampled color.
        depth_map: [num_rays]. Estimated distance to object.
    """
    
  

    dists = z_vals[...,1:] - z_vals[...,:-1]
    dists = torch.cat([dists, torch.Tensor([1e10]).expand(dists[...,:1].shape)], -1)  #[N_rays, N_samples]

    dists = dists * torch.norm(rays_d[...,None,:], dim=-1)


    if volumetric_function=="exp":
        raw2alpha = lambda raw, dists, act_fn=F.relu: 1.-torch.exp(-act_fn(raw)*dists)  ##equation 3
    elif volumetric_function=="weighted_gaussian":
        raw2alpha= lambda raw, dists, act_fn=F.relu: 1.-torch.exp(-((-act_fn(raw)*dists)-torch.mean((-act_fn(raw)*dists)))**2/(2*torch.std((-act_fn(raw)*dists))**2))
    elif volumetric_function =="gaussian":
        raw2alpha= lambda raw, dists, act_fn=F.relu: 1.-torch.exp(-torch.square(-act_fn(raw)*dists))
    elif volumetric_function=="sqaure":    
        raw2alpha = lambda raw, dists, act_fn=F.relu: 1.-torch.square(-act_fn(raw)*dists)
    elif volumetric_function=="tan":
        raw2alpha= lambda raw, dists, act_fn=F.relu: 1.-torch.tan(-act_fn(raw)*dists)
    elif volumetric_function=="tan_h":
        raw2alpha= lambda raw, dists, act_fn=F.relu: 1.-torch.tanh(-act_fn(raw)*dists)
    elif volumetric_function=="tan_pi":
        raw2alpha =lambda raw, dists, act_fn=F.relu: 1.-torch.tan(-act_fn(raw)*dists * torch.tensor(math.pi/2))

    rgb = torch.sigmoid(raw[...,:3])  # [N_rays, N_samples, 3]
    noise = 0.
    if raw_noise_std > 0.:
        noise = torch.randn(raw[...,3].shape) * raw_noise_std

        # Overwrite randomly sampled data if pytest
        if pytest:
            np.random.seed(0)
            noise = np.random.rand(*list(raw[...,3].shape)) * raw_noise_std
            noise = torch.Tensor(noise)

    alpha = raw2alpha(raw[...,3] + noise, dists)  # [N_rays, N_samples]
    ##equation 3
    weights = alpha * torch.cumprod(torch.cat([torch.ones((alpha.shape[0], 1)), 1.-alpha + 1e-10], -1), -1)[:, :-1]
    rgb_map = torch.sum(weights[...,None] * rgb, -2)  # [N_rays, 3]

    depth_map = torch.sum(weights * z_vals * torch.norm(rays_d[...,None,:], dim=-1), -1)
    disp_map = 1./torch.max(1e-10 * torch.ones_like(depth_map), depth_map / (torch.sum(weights, -1)  + 1e-6))
    acc_map = torch.sum(weights, -1)

    if white_bkgd:
        rgb_map = rgb_map + (1.-acc_map[...,None])
    return rgb_map, disp_map, acc_map, weights, depth_map


def render_rays(ray_batch,
                network_fn,
                network_query_fn,
                N_samples,
                volumetric_function,
                retraw=False,
                lindisp=False,
                perturb=0.,
                N_importance=0,
                network_fine=None,
                white_bkgd=False,
                raw_noise_std=0.,
                verbose=False,
                pytest=False,
                z_vals=None,
                use_two_models_for_fine=False,
                use_depth=False):
    """Volumetric rendering.
    Args:
      ray_batch: array of shape [batch_size, ...]. All information necessary
        for sampling along a ray, including: ray origin, ray direction, min
        dist, max dist, and unit-magnitude viewing direction.
      network_fn: function. Model for predicting RGB and density at each point
        in space.
      network_query_fn: function used for passing queries to network_fn.
      N_samples: int. Number of different times to sample along each ray.
      retraw: bool. If True, include model's raw, unprocessed predictions.
      lindisp: bool. If True, sample linearly in inverse depth rather than in depth.
      perturb: float, 0 or 1. If non-zero, each ray is sampled at stratified
        random points in time.
      N_importance: int. Number of additional times to sample along each ray.
        These samples are only passed to network_fine.
      network_fine: "fine" network with same spec as network_fn.
      white_bkgd: bool. If True, assume a white background.
      raw_noise_std: ...
      verbose: bool. If True, print more debugging info.
    Returns:
      rgb_map: [num_rays, 3]. Estimated RGB color of a ray. Comes from fine model.
      disp_map: [num_rays]. Disparity map. 1 / depth.
      acc_map: [num_rays]. Accumulated opacity along each ray. Comes from fine model.
      raw: [num_rays, num_samples, 4]. Raw predictions from model.
      rgb0: See rgb_map. Output for coarse model.
      disp0: See disp_map. Output for coarse model.
      acc0: See acc_map. Output for coarse model.
      z_std: [num_rays]. Standard deviation of distances along ray for each
        sample.
    """

    N_rays = ray_batch.shape[0]
    rays_o, rays_d = ray_batch[:,0:3], ray_batch[:,3:6] # [N_rays, 3] each
    viewdirs = ray_batch[:,-3:] if ray_batch.shape[-1] > 9 else None
    bounds = torch.reshape(ray_batch[...,6:9], [-1,1,3])
    near, far, frame_time = bounds[...,0], bounds[...,1], bounds[...,2] # [-1,1]
    z_samples = None
    rgb_map_0, disp_map_0, acc_map_0, position_delta_0 = None, None, None, None

    if z_vals is None:
        if not use_depth:
            t_vals = torch.linspace(0., 1., steps=N_samples)
            if not lindisp:
                z_vals = near * (1.-t_vals) + far * (t_vals)
            else:
                z_vals = 1./(1./near * (1.-t_vals) + 1./far * (t_vals))

            z_vals = z_vals.expand([N_rays, N_samples])
        else:
            mean = near.expand([N_rays, N_samples])
            std = far.expand([N_rays, N_samples])
            z_vals, _ = torch.sort(torch.normal(mean, std), dim=1)

        if perturb > 0.:
            # get intervals between samples
            mids = .5 * (z_vals[...,1:] + z_vals[...,:-1])
            upper = torch.cat([mids, z_vals[...,-1:]], -1)
            lower = torch.cat([z_vals[...,:1], mids], -1)
            # stratified samples in those intervals
            t_rand = torch.rand(z_vals.shape)

            # Pytest, overwrite u with numpy's fixed random numbers
            if pytest:
                np.random.seed(0)
                t_rand = np.random.rand(*list(z_vals.shape))
                t_rand = torch.Tensor(t_rand)

            z_vals = lower + (upper - lower) * t_rand

        pts = rays_o[...,None,:] + rays_d[...,None,:] * z_vals[...,:,None] # [N_rays, N_samples, 3]


        # if (torch.isnan(pts).any() or torch.isinf(pts).any()) and DEBUG:
        #     print(f"! [Numerical Error] pts contains nan or inf.", flush=True)

        # if (torch.isnan(rays_o).any() or torch.isinf(rays_o).any()) and DEBUG:
        #     print(f"! [Numerical Error] rays_o contains nan or inf.", flush=True)

        # if (torch.isnan(rays_d).any() or torch.isinf(rays_d).any()) and DEBUG:
        #     print(f"! [Numerical Error] rays_d contains nan or inf.", flush=True)

        # if (torch.isnan(z_vals).any() or torch.isinf(z_vals).any()) and DEBUG:
        #     print(f"! [Numerical Error] z_vals contains nan or inf.", flush=True)


        if N_importance <= 0:
            raw, position_delta = network_query_fn(pts, viewdirs, frame_time, network_fn)
            rgb_map, disp_map, acc_map, weights, depth_map = raw2outputs(raw, z_vals, rays_d, raw_noise_std, white_bkgd, pytest=pytest, volumetric_function=volumetric_function)

        else:
            if use_two_models_for_fine:
                raw, position_delta_0 = network_query_fn(pts, viewdirs, frame_time, network_fn)
                rgb_map_0, disp_map_0, acc_map_0, weights, _ = raw2outputs(raw, z_vals, rays_d, raw_noise_std, white_bkgd, pytest=pytest, volumetric_function=volumetric_function)

            else:
                with torch.no_grad():
                    raw, _ = network_query_fn(pts, viewdirs, frame_time, network_fn)
                    _, _, _, weights, _ = raw2outputs(raw, z_vals, rays_d, raw_noise_std, white_bkgd, pytest=pytest, volumetric_function=volumetric_function)

            z_vals_mid = .5 * (z_vals[...,1:] + z_vals[...,:-1])
            z_samples = importance_sampling_ray(z_vals_mid, weights[...,1:-1], N_importance, det=(perturb==0.), pytest=pytest)
            z_samples = z_samples.detach()
            z_vals, _ = torch.sort(torch.cat([z_vals, z_samples], -1), -1)

    pts = rays_o[...,None,:] + rays_d[...,None,:] * z_vals[...,:,None] # [N_rays, N_samples + N_importance, 3]
    run_fn = network_fn if network_fine is None else network_fine
    raw, position_delta = network_query_fn(pts, viewdirs, frame_time, run_fn)
    rgb_map, disp_map, acc_map, weights, _ = raw2outputs(raw, z_vals, rays_d, raw_noise_std, white_bkgd, pytest=pytest, volumetric_function=volumetric_function)

   #print("rgb_map",rgb_map)
    ret = {'rgb_map' : rgb_map, 'disp_map' : disp_map, 'acc_map' : acc_map, 'z_vals' : z_vals,
           'position_delta' : position_delta}
    #print("ret",ret)
    if retraw:
        ret['raw'] = raw
    if N_importance > 0:
        if rgb_map_0 is not None:
            ret['rgb0'] = rgb_map_0
        if disp_map_0 is not None:
            ret['disp0'] = disp_map_0
        if acc_map_0 is not None:
            ret['acc0'] = acc_map_0
        if position_delta_0 is not None:
            ret['position_delta_0'] = position_delta_0
        if z_samples is not None:
            ret['z_std'] = torch.std(z_samples, dim=-1, unbiased=False)  # [N_rays]

    # for k in ret:
    #     if (torch.isnan(ret[k]).any() or torch.isinf(ret[k]).any()) and DEBUG:
    #         print(f"! [Numerical Error] {k} contains nan or inf.", flush=True)

    return ret


def config_parser():

    import configargparse
    parser = configargparse.ArgumentParser()
    parser.add_argument('--config', is_config_file=True, 
                        help='config file path')
    parser.add_argument("--expname", type=str, 
                        help='experiment name')
    parser.add_argument("--basedir", type=str, default='./logs/', 
                        help='where to store ckpts and logs')
    parser.add_argument("--datadir", type=str, default='./data/llff/fern', 
                        help='input data directory')

    # training options
    parser.add_argument("--nerf_type", type=str, default="original",
                        help='nerf network type')
    parser.add_argument("--N_iter", type=int, default=100000,
                        help='num training iterations')
    parser.add_argument("--netdepth", type=int, default=8, 
                        help='layers in network')
    parser.add_argument("--netwidth", type=int, default=256, 
                        help='channels per layer')
    parser.add_argument("--netdepth_fine", type=int, default=8, 
                        help='layers in fine network')
    parser.add_argument("--netwidth_fine", type=int, default=256, 
                        help='channels per layer in fine network')
    parser.add_argument("--N_rand", type=int, default=32*32*4, 
                        help='batch size (number of random rays per gradient step)')
    parser.add_argument("--do_half_precision", action='store_true',
                        help='do half precision training and inference')
    parser.add_argument("--lrate", type=float, default=5e-4, 
                        help='learning rate')
    parser.add_argument("--lrate_decay", type=int, default=250, 
                        help='exponential learning rate decay (in 1000 steps)')
    parser.add_argument("--chunk", type=int, default=1024*32, 
                        help='number of rays processed in parallel, decrease if running out of memory')
    parser.add_argument("--netchunk", type=int, default=1024*64, 
                        help='number of pts sent through network in parallel, decrease if running out of memory')
    parser.add_argument("--no_batching", action='store_true', 
                        help='only take random rays from 1 image at a time')
    parser.add_argument("--no_reload", action='store_true', 
                        help='do not reload weights from saved ckpt')
    parser.add_argument("--ft_path", type=str, default=None, 
                        help='specific weights npy file to reload for coarse network')
    

    # rendering options
    parser.add_argument("--N_samples", type=int, default=64, 
                        help='number of coarse samples per ray')
    parser.add_argument("--not_zero_canonical", action='store_true',
                        help='if set zero time is not the canonic space')
    parser.add_argument("--N_importance", type=int, default=0,
                        help='number of additional fine samples per ray')
    parser.add_argument("--perturb", type=float, default=1.,
                        help='set to 0. for no jitter, 1. for jitter')
    parser.add_argument("--use_viewdirs", action='store_true', 
                        help='use full 5D input instead of 3D')
    parser.add_argument("--i_embed", type=int, default=0,
                        help='set 0 for default positional encoding, -1 for none')
    parser.add_argument("--multires", type=int, default=10, 
                        help='log2 of max freq for positional encoding (3D location)')
    parser.add_argument("--multires_views", type=int, default=4, 
                        help='log2 of max freq for positional encoding (2D direction)')
    parser.add_argument("--raw_noise_std", type=float, default=0., 
                        help='std dev of noise added to regularize sigma_a output, 1e0 recommended')
    parser.add_argument("--use_two_models_for_fine", action='store_true',
                        help='use two models for fine results')
                        
    parser.add_argument("--time_window_size", type=int, default=3, 
                        help='the size of time window in recurrent temporal nerf')
    parser.add_argument("--time_interval", type=float, default=-1, 
                        help='the time interval between two adjacent frames')

    parser.add_argument("--render_only", action='store_true', 
                        help='do not optimize, reload weights and render out render_poses path')
    parser.add_argument("--render_test", action='store_true', 
                        help='render the test set instead of render_poses path')
    parser.add_argument("--render_factor", type=int, default=0, 
                        help='downsampling factor to speed up rendering, set 4 or 8 for fast preview')
    parser.add_argument("--volumetric_function",type=str, default="exp", help="function used for weights in volumetric rendering")
    # training trick options
    parser.add_argument("--precrop_iters", type=int, default=0,
                        help='number of steps to train on central crops')
    parser.add_argument("--precrop_iters_time", type=int, default=0,
                        help='number of steps to train on central time')
    parser.add_argument("--precrop_frac", type=float,
                        default=.5, help='fraction of img taken for central crops')
    parser.add_argument("--add_tv_loss", action='store_true',
                        help='evaluate tv loss')
    parser.add_argument("--tv_loss_weight", type=float,
                        default=1.e-4, help='weight of tv loss')
    parser.add_argument("--use_fgmask", action='store_true',
                        help='use foreground masks?')
    parser.add_argument("--no_mask_raycast", action='store_true',
                        help='disable tool mask-guided ray-casting')
    parser.add_argument("--mask_loss", action='store_true',
                        help='enable erasing loss for masked pixels')
    parser.add_argument("--use_depth", action='store_true',
                        help='use depth?')
    parser.add_argument("--no_depth_sampling", action='store_true',
                        help='disable depth-guided ray sampling?')
    parser.add_argument("--depth_sampling_sigma", type=float, default=5.0,
                        help='std of depth-guided sampling')
    parser.add_argument("--depth_loss_weight", type=float, default=1.0,
                        help='weight of depth loss')
    parser.add_argument("--no_depth_refine", action='store_true',
                        help='disable depth refinement') 
    parser.add_argument("--depth_refine_period", type=int, default=4000,
                        help='number of iters to refine depth maps') 
    parser.add_argument("--depth_refine_rounds", type=int, default=4,
                        help='number of rounds of depth map refinement') 
    parser.add_argument("--depth_refine_quantile", type=float, default=0.3,
                        help='proportion of pixels to be updated during depth refinement')           


    # dataset options
    parser.add_argument("--dataset_type", type=str, default='llff', 
                        help='options: llff / blender / deepvoxels')
    parser.add_argument("--testskip", type=int, default=2,
                        help='will load 1/N images from test/val sets, useful for large datasets like deepvoxels')
    parser.add_argument("--davinci_endoscopic", action='store_true',
                        help='is Da Vinci endoscopic surgical fields?')
    parser.add_argument("--skip_frames", nargs='+', type=int, default=[], 
                        help='skip frames for training')

    ## deepvoxels flags (unused)
    parser.add_argument("--shape", type=str, default='greek', 
                        help='options : armchair / cube / greek / vase')

    ## blender flags (unused)
    parser.add_argument("--white_bkgd", action='store_true', 
                        help='set to render synthetic data on a white bkgd (always use for dvoxels)')
    parser.add_argument("--half_res", action='store_true', 
                        help='load blender synthetic data at 400x400 instead of 800x800')

    ## llff flags
    parser.add_argument("--factor", type=int, default=8, 
                        help='downsample factor for LLFF images')
    parser.add_argument("--no_ndc", action='store_true', 
                        help='do not use normalized device coordinates (set for non-forward facing scenes)')
    parser.add_argument("--lindisp", action='store_true', 
                        help='sampling linearly in disparity rather than depth')
    parser.add_argument("--spherify", action='store_true', 
                        help='set for spherical 360 scenes')
    parser.add_argument("--llffhold", type=int, default=8, 
                        help='will take every 1/N images as LLFF test set, paper uses 8')
    parser.add_argument("--llff_renderpath", type=str, default='spiral', 
                        help='options: spiral, fixidentity, zoom')
                                                
    # logging/saving options
    parser.add_argument("--i_print",   type=int, default=1000,
                        help='frequency of console printout and metric loggin')
    parser.add_argument("--i_img",     type=int, default=10000,
                        help='frequency of tensorboard image logging')
    parser.add_argument("--i_weights", type=int, default=100000,
                        help='frequency of weight ckpt saving')
    parser.add_argument("--i_testset", type=int, default=200000,
                        help='frequency of testset saving')
    parser.add_argument("--i_video",   type=int, default=200000,
                        help='frequency of render_poses video saving')
    parser.add_argument("--video_fps",  type=int, default=30,
                        help='FPS of render_poses video')
    

    return parser

def preprocess_image_specularity(images):
    result = []
    t = 0.12 #t in (0,0.5)
    for img in images: # also for i -> img[0,0,0,0] = first images, (0,0) r value
        height,width = img.shape
        img_mask = np.zeros(img.shape,img.dtype)

        for j in range(1,height-1):
            for i in range(1,width-1):
                new_pixel_value = img[j,i,0]*img[j,i,1]*img[j,i,2]
                #test for WU threshold

                img_mask[j,i] =  1 if new_pixel_value> t else 0
        result.append(img_mask)
    
        #save spec_masks in dir

    return result

def train():

    parser = config_parser()
    args = parser.parse_args()


    # Load data


    if args.dataset_type == 'blender':
        raise NotImplementedError

    elif args.dataset_type == 'llff':
        images, masks, depth_maps, edges_masks,poses, times, bds, render_poses, render_times, i_test = load_llff_data(args.datadir, args.factor,
                                                                  recenter=True, bd_factor=.75, spherify=args.spherify, fg_mask=args.use_fgmask, use_depth=args.use_depth,
                                                                  render_path=args.llff_renderpath, davinci_endoscopic=args.davinci_endoscopic)

        hwf = poses[0,:3,-1]
        poses = poses[:,:3,:4]
        print('Loaded llff', images.shape, render_poses.shape, hwf, args.datadir)

        if not isinstance(i_test, list):
            i_test = [i_test]

        if args.llffhold > 0:
            print('Auto LLFF holdout,', args.llffhold)
            i_test = np.arange(images.shape[0])[1:-1:args.llffhold]

        i_val = i_test
        i_train = np.array([i for i in np.arange(int(images.shape[0])) if (i not in args.skip_frames)])  # use all frames for reconstruction
        # i_train = np.array([i for i in np.arange(int(images.shape[0])) if (i not in i_test and i not in i_val and i not in args.skip_frames)])  # leave out test/val frames

        print('DEFINING BOUNDS')
        
        close_depth, inf_depth = np.ndarray.min(bds) * .9, np.ndarray.max(bds) * 1.

        if args.no_ndc:
            near = np.ndarray.min(bds) * .9
            far = np.ndarray.max(bds) * 1.            
        else:
            near = 0.
            far = 1.
        print('NEAR FAR', near, far)

        if args.time_interval < 0:
            args.time_interval = 1 / (images.shape[0] - 1)

    else:
        print('Unknown dataset type', args.dataset_type, 'exiting')
        return

    min_time, max_time = times[i_train[0]], times[i_train[-1]]
    assert min_time == 0., "time must start at 0"
    assert max_time == 1., "max time must be 1"

    # Cast intrinsics to right types
    H, W, focal = hwf
    H, W = int(H), int(W)
    hwf = [H, W, focal]

    if args.render_test:
        render_poses = np.array(poses[i_test])
        render_times = np.array(times[i_test])

    # Create log dir and copy the config file
    basedir = args.basedir
    expname = args.expname
    os.makedirs(os.path.join(basedir, expname), exist_ok=True)
    f = os.path.join(basedir, expname, 'args.txt')
    with open(f, 'w') as file:
        for arg in sorted(vars(args)):
            attr = getattr(args, arg)
            file.write('{} = {}\n'.format(arg, attr))
    if args.config is not None:
        f = os.path.join(basedir, expname, 'config.txt')
        with open(f, 'w') as file:
            file.write(open(args.config, 'r').read())
    print('Log directory:', os.path.join(basedir, expname))

    # Create nerf model
    render_kwargs_train, render_kwargs_test, start, grad_vars, optimizer, nerf_model_extras = create_nerf(args)
    global_step = start

    bds_dict = {
        'near' : near + 1e-6,
        'far' : far,
    }
    render_kwargs_train.update(bds_dict)
    render_kwargs_test.update(bds_dict)

    # Move testing data to GPU
    render_poses = torch.Tensor(render_poses).to(device)
    render_times = torch.Tensor(render_times).to(device)

    if depth_maps is not None:
        close_depth, inf_depth = np.percentile(depth_maps, 3.0), np.percentile(depth_maps, 99.9)

    # Short circuit if only rendering out from trained model
    if args.render_only:
        print('RENDER ONLY')
        with torch.no_grad():
            if args.render_test:
                # render_test switches to test poses
                images = images[i_test]

                save_gt = True
            else:
                # Default is smoother render_poses path
                images = None

                save_gt = False

            testsavedir = os.path.join(basedir, expname, 'renderonly_{}_{:06d}'.format('test' if args.render_test else ('path_%s' % args.llff_renderpath), start))
            os.makedirs(testsavedir, exist_ok=True)

            rgbs, _ = render_path(render_poses, render_times, hwf, args.chunk, args.volumetric_function, render_kwargs_test, gt_imgs=images,
                                  savedir=testsavedir, render_factor=args.render_factor, save_also_gt=save_gt, save_depth=True, near_far=(close_depth, inf_depth))
            print('Done rendering', testsavedir)
            imageio.mimwrite(os.path.join(testsavedir, 'video.mp4'), to8b(rgbs), fps=args.video_fps, quality=8)

            return

    N_rand = args.N_rand
    use_batching = not args.no_batching

    # Prepare ray batch tensor if batching random rays
    # if use_batching:
    #     # For random ray batching
    #     print('get rays')
    #     rays = np.stack([get_rays_np(H, W, focal, p) for p in poses[:,:3,:4]], 0) # [N, ro+rd, H, W, 3]
    #     print('done, concats')
    #     rays_rgb = np.concatenate([rays, images[:,None]], 1) # [N, ro+rd+rgb, H, W, 3]
    #     rays_rgb = np.transpose(rays_rgb, [0,2,3,1,4]) # [N, H, W, ro+rd+rgb, 3]
    #     rays_rgb = np.stack([rays_rgb[i] for i in i_train], 0) # train images only
    #     rays_rgb = np.reshape(rays_rgb, [-1,3,3]) # [(N-1)*H*W, ro+rd+rgb, 3]
    #     rays_rgb = rays_rgb.astype(np.float32)
    #     print('shuffle rays')
    #     np.random.shuffle(rays_rgb)

    #     print('done')
    #     # i_batch = 0

    # Move training data to GPU
    images = torch.Tensor(images).to(device)
    poses = torch.Tensor(poses).to(device)
    times = torch.Tensor(times).to(device)

    if edges_masks is not None:
        edges_masks = torch.Tensor(edges_masks).to(device)

    if masks is not None:
        masks = torch.Tensor(masks).to(device)
        if nerf_model_extras['ray_importance_maps'] is None:
            ray_importance_maps = ray_sampling_importance_from_masks(masks)

            ray_importance_maps =ray_sampling_importance_only_edges(masks,edges_masks)
            #ray_importance_maps = ray_sampling_importance_from_multiple_masks(masks,edges_masks)
        else:
            ray_importance_maps = torch.Tensor(nerf_model_extras['ray_importance_maps']).to(device)
    if depth_maps is not None:
        if nerf_model_extras['depth_maps'] is None:
            depth_maps = torch.Tensor(depth_maps).to(device)
        else:
            depth_maps = torch.Tensor(nerf_model_extras['depth_maps']).to(device)

    # if use_batching:
    #     rays_rgb = torch.Tensor(rays_rgb).to(device)

    print('images shape', images.shape)
    print('poses shape', poses.shape)
    print('times shape', times.shape)
    if masks is not None:
        print('masks shape', masks.shape)
    if depth_maps is not None:
        print('depth shape', depth_maps.shape)
        print('close depth:', close_depth, 'inf depth:', inf_depth)
    N_iters = args.N_iter + 1
    print('Begin')

    # Summary writers
    writer = SummaryWriter(os.path.join(basedir, 'summaries', expname))
    
    start = start + 1
    for i in trange(start, N_iters):
        torch.cuda.empty_cache()
        ##### Sample random ray batch #####
        if use_batching:
            raise NotImplementedError("Not implemented")

            # Random over all images
            # batch = rays_rgb[i_batch:i_batch+N_rand] # [B, 2+1, 3*?]
            # batch = torch.transpose(batch, 0, 1)
            # batch_rays, target_s = batch[:2], batch[2]

            # i_batch += N_rand
            # if i_batch >= rays_rgb.shape[0]:
            #     print("Shuffle data after an epoch!")
            #     rand_idx = torch.randperm(rays_rgb.shape[0])
            #     rays_rgb = rays_rgb[rand_idx]
            #     i_batch = 0

        else:
            # Random from one image
            if i >= args.precrop_iters_time:
                img_i = np.random.choice(i_train)
            else:
                skip_factor = i / float(args.precrop_iters_time) * len(i_train)
                max_sample = max(int(skip_factor), 3)
                img_i = np.random.choice(i_train[:max_sample])

            # target = torch.Tensor(images[img_i]).to(device)
            target = images[img_i]
            pose = poses[img_i, :3, :4]
            frame_time = times[img_i]

            if masks is not None:
                mask = masks[img_i]
                ray_importance_map = ray_importance_maps[img_i]
            if depth_maps is not None:
                depth_map = depth_maps[img_i]
            if edges_masks is not None:
                edges_mask = edges_masks[img_i]

            if N_rand is not None:
                rays_o, rays_d = get_rays(H, W, focal, torch.Tensor(pose))  # (H, W, 3), (H, W, 3)

                if i < args.precrop_iters:
                    dH = int(H//2 * args.precrop_frac)
                    dW = int(W//2 * args.precrop_frac)
                    coords = torch.stack(
                        torch.meshgrid(
                            torch.linspace(H//2 - dH, H//2 + dH - 1, 2*dH),
                            torch.linspace(W//2 - dW, W//2 + dW - 1, 2*dW)
                        ), -1)
                    if i == start:
                        print(f"[Config] Center cropping of size {2*dH} x {2*dW} is enabled until iter {args.precrop_iters}")                
                else:
                    coords = torch.stack(torch.meshgrid(torch.linspace(0, H-1, H), torch.linspace(0, W-1, W)), -1)  # (H, W, 2)

                coords = torch.reshape(coords, [-1,2])  # (H * W, 2)
                if masks is None or args.no_mask_raycast:
                    select_inds = np.random.choice(coords.shape[0], size=[N_rand], replace=False)  # (N_rand,)
                elif masks is not None:
                    select_inds, _, cdf = importance_sampling_coords(ray_importance_map[coords[:, 0].long(), coords[:, 1].long()].unsqueeze(0), N_rand)
                    select_inds = torch.max(torch.zeros_like(select_inds), select_inds)
                    select_inds = torch.min((coords.shape[0] - 1) * torch.ones_like(select_inds), select_inds)
                    select_inds = select_inds.squeeze(0)
                    
                select_coords = coords[select_inds].long()  # (N_rand, 2)
                rays_o = rays_o[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                rays_d = rays_d[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                batch_rays = torch.stack([rays_o, rays_d], 0)
                target_s = target[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                if depth_maps is not None:
                    depth_s = depth_map[select_coords[:, 0], select_coords[:, 1]]
                    if not args.no_ndc:
                        depth_s = depth_s / ((inf_depth - close_depth) + 1e-6)

                    # Apply depth-guided ray sampling
                    if not args.no_depth_sampling:
                        bds_dict = {
                            'near' : depth_s.detach().clone() + 1e-6,
                            'far' : args.depth_sampling_sigma,
                        }
                        render_kwargs_train.update(bds_dict)
                if masks is not None and args.mask_loss:
                    mask_s = mask[select_coords[:, 0], select_coords[:, 1]]
                    mask_s = mask_s.unsqueeze(-1)
                else:
                    mask_s = None

        #####  Core optimization loop  #####
        rgb, disp, acc, extras = render(H, W, focal, chunk=args.chunk, rays=batch_rays, frame_time=frame_time,
                                                verbose=i < 10, retraw=True,
                                                **render_kwargs_train)

        if args.add_tv_loss:
            frame_time_prev = times[img_i - 1] if img_i > 0 else None
            frame_time_next = times[img_i + 1] if img_i < times.shape[0] - 1 else None

            if frame_time_prev is not None and frame_time_next is not None:
                if np.random.rand() > .5:
                    frame_time_prev = None
                else:
                    frame_time_next = None

            if frame_time_prev is not None:
                rand_time_prev = frame_time_prev + (frame_time - frame_time_prev) * torch.rand(1)[0]
                _, _, _, extras_prev = render(H, W, focal, chunk=args.chunk, rays=batch_rays, frame_time=rand_time_prev,
                                                verbose=i < 10, retraw=True, z_vals=extras['z_vals'].detach(),
                                                **render_kwargs_train)

            if frame_time_next is not None:
                rand_time_next = frame_time + (frame_time_next - frame_time) * torch.rand(1)[0]
                _, _, _, extras_next = render(H, W, focal, chunk=args.chunk, rays=batch_rays, frame_time=rand_time_next,
                                                verbose=i < 10, retraw=True, z_vals=extras['z_vals'].detach(),
                                                **render_kwargs_train)

        optimizer.zero_grad()
        if mask_s is not None:
            rgb = rgb * mask_s
            target_s = target_s * mask_s
        
        img_loss = img2mse(rgb, target_s)
        psnr = mse2psnr(img_loss)

        tv_loss = 0
        if args.add_tv_loss:
            if frame_time_prev is not None:
                tv_loss += ((extras['position_delta'] - extras_prev['position_delta']).pow(2)).sum()
                if 'position_delta_0' in extras:
                    tv_loss += ((extras['position_delta_0'] - extras_prev['position_delta_0']).pow(2)).sum()
            if frame_time_next is not None:
                tv_loss += ((extras['position_delta'] - extras_next['position_delta']).pow(2)).sum()
                if 'position_delta_0' in extras:
                    tv_loss += ((extras['position_delta_0'] - extras_next['position_delta_0']).pow(2)).sum()
            tv_loss = tv_loss * args.tv_loss_weight

        loss = img_loss + tv_loss

        if depth_maps is not None:
            if args.depth_loss_weight > 1e-16:
                pred_depth = 1.0 / (disp + 1e-6)
                if mask_s is not None:
                    pred_depth = pred_depth * mask_s
                    depth_s = depth_s * mask_s
                depth_loss = F.huber_loss(pred_depth, depth_s, delta=0.2)
                loss = loss + args.depth_loss_weight * depth_loss
            else:
                depth_loss = torch.Tensor([-1.0]).to(device)

        if 'rgb0' in extras:
            img_loss0 = img2mse(extras['rgb0'], target_s)
            loss = loss + img_loss0
            psnr0 = mse2psnr(img_loss0)

        if args.do_half_precision:
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        optimizer.step()

        # NOTE: IMPORTANT!
        ###   update learning rate   ###
        decay_rate = 0.1
        decay_steps = args.lrate_decay * 1000
        new_lrate = args.lrate * (decay_rate ** (global_step / decay_steps))
        for param_group in optimizer.param_groups:
            param_group['lr'] = new_lrate

        ##### Refine depth maps and ray importance maps ##### section 2.1
        refinement_round = i // args.depth_refine_period
        if not args.no_depth_refine and depth_maps is not None and i % args.depth_refine_period == 0 and refinement_round <= args.depth_refine_rounds:
            print('Render RGB and depth maps for refinement...')
            
            refinement_save_path = os.path.join(basedir, expname, 'refinement{:04d}'.format(refinement_round))
            if not os.path.exists(refinement_save_path):
                os.makedirs(refinement_save_path)
            depth_prev_save_path = os.path.join(refinement_save_path, 'depth_prev')
            depth_refined_save_path = os.path.join(refinement_save_path, 'depth_refined')
            if not os.path.exists(depth_prev_save_path):
                os.makedirs(depth_prev_save_path)
            if not os.path.exists(depth_refined_save_path):
                os.makedirs(depth_refined_save_path)
            # importance_maps_prev_save_path = os.path.join(refinement_save_path, 'importance_maps_prev')
            # importance_maps_refined_save_path = os.path.join(refinement_save_path, 'importance_maps_refined')
            # if not os.path.exists(importance_maps_prev_save_path):
            #     os.makedirs(importance_maps_prev_save_path)
            # if not os.path.exists(importance_maps_refined_save_path):
            #     os.makedirs(importance_maps_refined_save_path)

            with torch.no_grad():
                rgbs_t, disps_t = render_path_gpu(poses[i_train], times[i_train], hwf, args.chunk, render_kwargs_test)

                masks_gt = masks[i_train] # [N_train, H, W]

                # Refine depth maps
                depth_t = (1.0 / (disps_t + 1e-6)) * (inf_depth - close_depth)
                depth_gt = depth_maps[i_train]

                max_depth = depth_maps[i_train].max()
                for j in i_train:
                    imageio.imwrite(os.path.join(depth_prev_save_path, 'depth_{:0d}.png'.format(j)), to8b((depth_maps[j] / max_depth).cpu().numpy()))

                depth_diff = torch.pow(depth_t - depth_gt, 2) * masks_gt # [N_train, H, W]
                depth_diff = depth_diff.reshape(depth_diff.shape[0], -1) # [N_train, H x W]
                quantile = torch.quantile(depth_diff, 1.0 - args.depth_refine_quantile, dim=1, keepdim=True) # [N_train, 1]
                depth_to_refine = (depth_diff > quantile).reshape(*depth_t.shape) # [N_train, H, W]
                depth_gt[depth_to_refine] = depth_t[depth_to_refine]
                depth_maps[i_train] = depth_gt

                max_depth = depth_maps[i_train].max()
                for j in i_train:
                    imageio.imwrite(os.path.join(depth_refined_save_path, 'depth_{:0d}.png'.format(j)), to8b((depth_maps[j] / max_depth).cpu().numpy()))

                save_dict = {
                    'rounds': refinement_round,
                    'quantile': quantile.cpu().numpy(),
                    'depth_diff': depth_diff.cpu().numpy(),
                    'depth_to_refine': depth_to_refine.cpu().numpy()
                }
                torch.save(save_dict, os.path.join(refinement_save_path, 'depth_refine_info.tar'))

                del disps_t, depth_t, depth_gt, depth_to_refine, depth_diff, quantile

                # Refine ray importance maps
                # max_importance = ray_importance_maps[i_train].max()
                # for j in i_train:
                #     imageio.imwrite(os.path.join(importance_maps_prev_save_path, 'importance_{:0d}.png'.format(j)), to8b((ray_importance_maps[j] / max_importance).cpu().numpy()))

                # rgbs_gt = images[i_train] # [N_train, H, W, 3]
                # rgb_mse = torch.mean(torch.pow(rgbs_t - rgbs_gt, 2), dim=-1) * masks_gt # [N_train, H, W]
                # rgb_psnr = mse2psnr(rgb_mse)
                # new_importance_maps = torch.nan_to_num(1.0 - F.softmax(rgb_psnr / 10.0, dim=0)) # [N_train, H, W]
                # ray_importance_maps[i_train] = ray_importance_maps[i_train] * (1.0 + new_importance_maps)

                # max_importance = ray_importance_maps[i_train].max()
                # for j in i_train:
                #     imageio.imwrite(os.path.join(importance_maps_refined_save_path, 'importance_{:0d}.png'.format(j)), to8b((ray_importance_maps[j] / max_importance).cpu().numpy()))

                # save_dict = {
                #     'rounds': refinement_round,
                #     'rgb_mse': rgb_mse.cpu().numpy(),
                #     'rgb_psnr': rgb_psnr.cpu().numpy(),
                #     'new_importance_maps': new_importance_maps.cpu().numpy()
                # }
                # torch.save(save_dict, os.path.join(refinement_save_path, 'importance_map_info.tar'))

                # del rgbs_gt, rgb_mse, rgb_psnr, new_importance_maps

                del rgbs_t, masks_gt

                print('\nRefinement finished, intermediate results saved at', refinement_save_path)


        ################################
        # Rest is logging

        if i%args.i_weights==0:
            path = os.path.join(basedir, expname, '{:06d}.tar'.format(i))
            save_dict = {
                'global_step': global_step,
                'network_fn_state_dict': render_kwargs_train['network_fn'].state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'ray_importance_maps': ray_importance_maps.cpu().numpy(),
                'depth_maps': depth_maps.cpu().numpy() if depth_maps is not None else None
            }
            if render_kwargs_train['network_fine'] is not None:
                save_dict['network_fine_state_dict'] = render_kwargs_train['network_fine'].state_dict()

            if args.do_half_precision:
                save_dict['amp'] = amp.state_dict()
            torch.save(save_dict, path)
            print('Saved checkpoints at', path)

        if i % args.i_print == 0:
            tqdm_txt = f"[TRAIN] Iter: {i} Img Loss: {img_loss.item()} PSNR: {psnr.item()}"
            if args.add_tv_loss:
                tqdm_txt += f" TV: {tv_loss.item()}"
            if depth_maps is not None:
                tqdm_txt += f" Depth Loss: {depth_loss.item()}"
            tqdm.write(tqdm_txt)

            writer.add_scalar('loss', img_loss.item(), i)
            writer.add_scalar('psnr', psnr.item(), i)
            if 'rgb0' in extras:
                writer.add_scalar('loss0', img_loss0.item(), i)
                writer.add_scalar('psnr0', psnr0.item(), i)
            if args.add_tv_loss:
                writer.add_scalar('tv', tv_loss.item(), i)
            if depth_maps is not None:
                writer.add_scalar('depth', depth_loss.item(), i)

        del loss, img_loss, psnr, target_s
        if 'rgb0' in extras:
            del img_loss0, psnr0
        if args.add_tv_loss:
            del tv_loss
        if depth_maps is not None:
            del depth_loss, depth_s
        del rgb, disp, acc, extras

        if i%args.i_img==0:
            torch.cuda.empty_cache()
            # Log a rendered validation view to Tensorboard
            img_i=np.random.choice(i_val)
            target = images[img_i]
            pose = poses[img_i, :3,:4]
            frame_time = times[img_i]
            with torch.no_grad():
                rgb, disp, acc, extras = render(H, W, focal, chunk=args.chunk, c2w=pose, frame_time=frame_time,
                                                    **render_kwargs_test)

            psnr = mse2psnr(img2mse(rgb, target))
            writer.add_image('gt', to8b(target.cpu().numpy()), i, dataformats='HWC')
            writer.add_image('rgb', to8b(rgb.cpu().numpy()), i, dataformats='HWC')
            writer.add_image('disp', disp.cpu().numpy(), i, dataformats='HW')
            writer.add_image('acc', acc.cpu().numpy(), i, dataformats='HW')

            if 'rgb0' in extras:
                writer.add_image('rgb_rough', to8b(extras['rgb0'].cpu().numpy()), i, dataformats='HWC')
            if 'disp0' in extras:
                writer.add_image('disp_rough', extras['disp0'].cpu().numpy(), i, dataformats='HW')
            if 'z_std' in extras:
                writer.add_image('acc_rough', extras['z_std'].cpu().numpy(), i, dataformats='HW')

            print("finish summary")
            writer.flush()

        if i%args.i_video==0:
            torch.cuda.empty_cache()
            # Turn on testing mode
            print("Rendering video...")
            with torch.no_grad():
                savedir = os.path.join(basedir, expname, 'frames_{}_{}_{:06d}_time/'.format(expname, args.llff_renderpath, i))
                rgbs, disps = render_path(render_poses, render_times, hwf, args.chunk, args.volumetric_function, render_kwargs_test, savedir=savedir)
            print('Done, saving', rgbs.shape, disps.shape)
            moviebase = os.path.join(basedir, expname, '{}_{}_{:06d}_'.format(expname, args.llff_renderpath, i))
            imageio.mimwrite(moviebase + 'rgb.mp4', to8b(rgbs), fps=args.video_fps, quality=8)
            imageio.mimwrite(moviebase + 'disp.mp4', to8b(disps / np.max(disps)), fps=args.video_fps, quality=8)

            # if args.use_viewdirs:
            #     render_kwargs_test['c2w_staticcam'] = render_poses[0][:3,:4]
            #     with torch.no_grad():
            #         rgbs_still, _ = render_path(render_poses, hwf, args.chunk, render_kwargs_test)
            #     render_kwargs_test['c2w_staticcam'] = None
            #     imageio.mimwrite(moviebase + 'rgb_still.mp4', to8b(rgbs_still), fps=30, quality=8)

        if i%args.i_testset==0:
            testsavedir = os.path.join(basedir, expname, 'testset_{:06d}'.format(i))
            print('Testing poses shape...', poses[i_test].shape)
            with torch.no_grad():
                render_path(torch.Tensor(poses[i_test]).to(device), torch.Tensor(times[i_test]).to(device),
                            hwf, args.chunk,args.volumetric_function, render_kwargs_test, gt_imgs=torch.Tensor(images[i_test]).to(device), savedir=testsavedir)
            print('Saved test set')

        global_step += 1


if __name__=='__main__':
    torch.set_default_tensor_type('torch.cuda.FloatTensor')

    train()
