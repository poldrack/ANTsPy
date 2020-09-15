"""
Joint Label Fusion algorithm
"""

__all__ = ["joint_label_fusion"]

import os
import numpy as np
import warnings
from pathlib import Path
from tempfile import mktemp
import glob
import re

from .. import utils
from ..core import ants_image as iio
from ..core import ants_image_io as iio2


def joint_label_fusion(
    target_image,
    target_image_mask,
    atlas_list,
    beta=4,
    rad=2,
    label_list=None,
    rho=0.01,
    usecor=False,
    r_search=3,
    nonnegative=False,
    no_zeroes=False,
    max_lab_plus_one=False,
    output_prefix=None,
    verbose=False,
):
    """
    A multiple atlas voting scheme to customize labels for a new subject.
    This function will also perform intensity fusion. It almost directly
    calls the C++ in the ANTs executable so is much faster than other
    variants in ANTsR.

    One may want to normalize image intensities for each input image before
    passing to this function. If no labels are passed, we do intensity fusion.
    Note on computation time: the underlying C++ is multithreaded.
    You can control the number of threads by setting the environment
    variable ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS e.g. to use all or some
    of your CPUs. This will improve performance substantially.
    For instance, on a macbook pro from 2015, 8 cores improves speed by about 4x.

    ANTsR function: `jointLabelFusion`

    Arguments
    ---------
    target_image : ANTsImage
        image to be approximated

    target_image_mask : ANTsImage
        mask with value 1

    atlas_list : list of ANTsImage types
        list containing intensity images

    beta : scalar
        weight sharpness, default to 2

    rad : scalar
        neighborhood radius, default to 2

    label_list : list of ANTsImage types (optional)
        list containing images with segmentation labels

    rho : scalar
        ridge penalty increases robustness to outliers but also makes image converge to average

    usecor : boolean
        employ correlation as local similarity

    r_search : scalar
        radius of search, default is 3

    nonnegative : boolean
        constrain weights to be non-negative

    no_zeroes : boolean
        this will constrain the solution only to voxels that are always non-zero in the label list

    max_lab_plus_one : boolean
        this will add max label plus one to the non-zero parts of each label where the target mask is greater than one

    output_prefix: string
        file prefix for storing output probabilityimages to disk

    verbose : boolean
        whether to show status updates

    Returns
    -------
    dictionary w/ following key/value pairs:
        `segmentation` : ANTsImage
            segmentation image

        `intensity` : ANTsImage
            intensity image

        `probabilityimages` : list of ANTsImage types
            probability map image for each label

    Example
    -------
    >>> import ants
    >>> ref = ants.image_read( ants.get_ants_data('r16'))
    >>> ref = ants.resample_image(ref, (50,50),1,0)
    >>> ref = ants.iMath(ref,'Normalize')
    >>> mi = ants.image_read( ants.get_ants_data('r27'))
    >>> mi2 = ants.image_read( ants.get_ants_data('r30'))
    >>> mi3 = ants.image_read( ants.get_ants_data('r62'))
    >>> mi4 = ants.image_read( ants.get_ants_data('r64'))
    >>> mi5 = ants.image_read( ants.get_ants_data('r85'))
    >>> refmask = ants.get_mask(ref)
    >>> refmask = ants.iMath(refmask,'ME',2) # just to speed things up
    >>> ilist = [mi,mi2,mi3,mi4,mi5]
    >>> seglist = [None]*len(ilist)
    >>> for i in range(len(ilist)):
    >>>     ilist[i] = ants.iMath(ilist[i],'Normalize')
    >>>     mytx = ants.registration(fixed=ref , moving=ilist[i] ,
    >>>         typeofTransform = ('Affine') )
    >>>     mywarpedimage = ants.apply_transforms(fixed=ref,moving=ilist[i],
    >>>             transformlist=mytx['fwdtransforms'])
    >>>     ilist[i] = mywarpedimage
    >>>     seg = ants.threshold_image(ilist[i],'Otsu', 3)
    >>>     seglist[i] = ( seg ) + ants.threshold_image( seg, 1, 3 ).morphology( operation='dilate', radius=3 )
    >>> r = 2
    >>> pp = ants.joint_label_fusion(ref, refmask, ilist, r_search=2,
    >>>                     label_list=seglist, rad=[r]*ref.dimension )
    >>> pp = ants.joint_label_fusion(ref,refmask,ilist, r_search=2, rad=[r]*ref.dimension)
    """
    segpixtype = "unsigned int"
    if (label_list is None) or (np.any([l is None for l in label_list])):
        doJif = True
    else:
        doJif = False

    if not doJif:
        if len(label_list) != len(atlas_list):
            raise ValueError("len(label_list) != len(atlas_list)")
        if no_zeroes:
            for label in label_list:
                target_image_mask[label == 0] = 0
        inlabs = set()
        for label in label_list:
            values = np.unique(label[target_image_mask != 0 and label != 0])
            inlabs = inlabs.union(values)
        inlabs = sorted(inlabs)
        maxLab = max(inlabs)
        if max_lab_plus_one:
            for label in label_list:
                label[label == 0 and target_image_mask == 1] = maxLab + 1
        mymask = target_image_mask.clone()
    else:
        mymask = target_image_mask

    osegfn = mktemp(prefix="antsr", suffix="myseg.nii.gz")
    # segdir = osegfn.replace(os.path.basename(osegfn),'')

    if os.path.exists(osegfn):
        os.remove(osegfn)

    if output_prefix is None:
        probs = mktemp(prefix="antsr", suffix="prob%02d.nii.gz")
        probsbase = os.path.basename(probs)
        tdir = probs.replace(probsbase, "")
        searchpattern = probsbase.replace("%02d", "*")

    if output_prefix is not None:
        probs = output_prefix + "prob%02d.nii.gz"
        probpath = Path(probs).parent
        Path(probpath).mkdir(parents=True, exist_ok=True)
        probsbase = os.path.basename(probs)
        tdir = probs.replace(probsbase, "")
        searchpattern = probsbase.replace("%02d", "*")

    mydim = target_image_mask.dimension
    if not doJif:
        # not sure if these should be allocated or what their size should be
        outimg = label_list[1].clone(segpixtype)
        outimgi = target_image * 0

        outimg_ptr = utils.get_pointer_string(outimg)
        outimgi_ptr = utils.get_pointer_string(outimgi)
        outs = "[%s,%s,%s]" % (outimg_ptr, outimgi_ptr, probs)
    else:
        outimgi = target_image * 0
        outs = utils.get_pointer_string(outimgi)

    mymask = mymask.clone(segpixtype)
    if (not isinstance(rad, (tuple, list))) or (len(rad) == 1):
        myrad = [rad] * mydim
    else:
        myrad = rad

    if len(myrad) != mydim:
        raise ValueError("path radius dimensionality must equal image dimensionality")

    myrad = "x".join([str(mr) for mr in myrad])
    vnum = 1 if verbose else 0
    nnum = 1 if nonnegative else 0

    myargs = {
        "d": mydim,
        "t": target_image,
        "a": rho,
        "b": beta,
        "c": nnum,
        "p": myrad,
        "m": "PC",
        "s": r_search,
        "x": mymask,
        "o": outs,
        "v": vnum,
    }

    kct = len(myargs.keys())
    for k in range(len(atlas_list)):
        kct += 1
        myargs["g-MULTINAME-%i" % kct] = atlas_list[k]
        if not doJif:
            kct += 1
            castseg = label_list[k].clone(segpixtype)
            myargs["l-MULTINAME-%i" % kct] = castseg

    myprocessedargs = utils._int_antsProcessArguments(myargs)

    libfn = utils.get_lib_fn("antsJointFusion")
    rval = libfn(myprocessedargs)
    if rval != 0:
        print("Warning: Non-zero return from antsJointFusion")

    if doJif:
        return outimgi

    probsout = glob.glob(os.path.join(tdir, "*" + searchpattern))
    probsout.sort()
    probimgs = []
    for idx in range(len(probsout)):
        probimgs.append(iio2.image_read(probsout[idx]))
    if len(probsout) != (len(inlabs)):
        warnings.warn("Length of output probabilities != length of unique input labels")

    segmat = iio2.images_to_matrix(probimgs, target_image_mask)
    finalsegvec = segmat.argmax(axis=0)
    finalsegvec2 = finalsegvec.copy()
    # mapfinalsegvec to original labels
    for i in range(len(probsout)):
        segnum = str.split(probsout[i], "prob")[1].split(".nii.gz")[0]
        finalsegvec2[finalsegvec == i] = segnum
    outimg = iio2.make_image(target_image_mask, finalsegvec2)

    return {"segmentation": outimg, "intensity": outimgi, "probabilityimages": probimgs}
