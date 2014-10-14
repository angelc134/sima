import itertools as it
import multiprocessing
import warnings

import numpy as np
try:
    from bottleneck import nanmean
except ImportError:
    from scipy import nanmean

from sima.misc.align import align_cross_correlation
import motion


# Setup global variables used during parallelized whole frame shifting
lock = 0
namespace = 0


class Struct:
    def __init__(self, **entries):
        self.__dict__.update(entries)


class PlaneTranslation2D(motion.MotionEstimationStrategy):
    """Estimate 2D translations for each plane.

    Parameters
    ----------
    max_displacement : array of int, optional
        The maximum allowed displacement magnitudes in [y,x]. By
        default, arbitrarily large displacements are allowed.
    method : {'correlation', 'ECC'}
        Alignment method to be used.
    n_processes : (None, int)
        Number of pool processes to spawn to parallelize frame alignment
    partitions : tuple of int, optional
        The number of partitions in y and x respectively. The alignement
        will be calculated separately on each partition and then the
        results compared. Default: calculates an appropriate value based
        on max_displacement and the frame shape.
    """

    def __init__(self, max_displacement=None, method='correlation',
                 n_processes=None, partitions=None):
        d = locals()
        del d['self']
        self._params = Struct(**d)

    def _estimate(self, dataset):
        """Estimate whole-frame displacements based on pixel correlations.

        Parameters
        ----------

        Returns
        -------
        shifts : array
            (2, num_frames*num_cycles)-array of integers giving the
            estimated displacement of each frame
        """
        # if method == 'correlation':
        #     displacements, correlations = estimate(
        #         mc_sequences, max_displacement, n_processes=n_processes)
        # elif method == 'ECC':
        #     # http://docs.opencv.org/trunk/modules/video/doc
        #     # /motion_analysis_and_object_tracking.html#findtransformecc
        #     #
        #     # http://xanthippi.ceid.upatras.gr/people/evangelidis/ecc/
        #     raise NotImplementedError
        # else:
        #     raise ValueError("Unrecognized option for 'method'")

        params = self._params
        frame_shape = dataset.frame_shape
        DIST_CRITERION = 2.
        if params.partitions is None:
            params.partitions = (1, 1)
        dy = frame_shape[1] / params.partitions[0]
        dx = frame_shape[2] / params.partitions[1]

        shifts_record = []
        corr_record = []
        for ny, nx in it.product(
                range(params.partitions[0]), range(params.partitions[1])):
            partioned_sequences = [s[:, :, ny*dy:(ny+1)*dy, nx*dx:(nx+1)*dx]
                                   for s in dataset]
            shifts, correlations = _frame_alignment_base(
                partioned_sequences, params.max_displacement, params.method,
                params.n_processes)
            shifts_record.append(shifts)
            corr_record.append(correlations)
            if len(shifts_record) > 1:
                first_shifts = []  # shifts to be return
                second_shifts = []
                out_corrs = []  # correlations to be returned
                for corrs, shifts in zip(
                        zip(*corr_record), zip(*shifts_record)):
                    corr_array = np.array(corrs)  # (partions, frames, planes)
                    shift_array = np.array(shifts)
                    assert corr_array.ndim is 3 and shift_array.ndim is 4
                    second, first = np.argpartition(
                        corr_array, -2, axis=0)[-2:]
                    first_shifts.append(np.concatenate(
                        [np.expand_dims(first.choose(s), -1)
                         for s in np.rollaxis(shift_array, -1)],
                        axis=-1))
                    second_shifts.append(np.concatenate(
                        [np.expand_dims(second.choose(s), -1)
                         for s in np.rollaxis(shift_array, -1)],
                        axis=-1))
                    out_corrs.append(first.choose(corr_array))
                if np.mean([np.sum((f - s)**2, axis=-1)
                            for f, s in zip(first_shifts, second_shifts)]
                           ) < (DIST_CRITERION ** 2):
                    break
        try:
            return first_shifts
        except NameError:  # single parition case
            return shifts


def _frame_alignment_base(
        sequences, max_displacement=None, method='correlation',
        n_processes=None):
    """Estimate whole-frame displacements based on pixel correlations.

    Parameters
    ----------
    max_displacement : array
        see estimate_displacements

    Returns
    -------
    shifts : array
        (2, num_frames*num_cycles)-array of integers giving the
        estimated displacement of each frame
    correlations : array
        (num_frames*num_cycles)-array giving the correlation of
        each shifted frame with the reference
    n_processes : (None, int)
        Number of pool processes to spawn to parallelize frame alignment
    """

    if n_processes is None:
        n_pools = multiprocessing.cpu_count() / 2
    else:
        n_pools = n_processes
    if n_pools == 0:
        n_pools = 1

    global namespace
    global lock
    namespace = multiprocessing.Manager().Namespace()
    namespace.offset = np.zeros(2, dtype=int)
    namespace.pixel_counts = np.zeros(sequences[0].shape[1:])  # TODO: int?
    namespace.pixel_sums = np.zeros(sequences[0].shape[1:]).astype('float64')
    # NOTE: float64 gives nan when divided by 0
    namespace.shifts = [
        np.zeros(seq.shape[:2] + (2,), dtype=int) for seq in sequences]
    namespace.correlations = [np.empty(seq.shape[:2]) for seq in sequences]

    lock = multiprocessing.Lock()
    pool = multiprocessing.Pool(processes=n_pools, maxtasksperchild=1)

    for cycle_idx, cycle in zip(it.count(), sequences):
        if n_processes > 1:
            map_generator = pool.imap_unordered(
                _align_frame,
                zip(it.count(), cycle, it.repeat(cycle_idx),
                    it.repeat(method), it.repeat(max_displacement)),
                chunksize=1 + len(cycle) / n_pools)
        else:
            map_generator = it.imap(
                _align_frame,
                zip(it.count(), cycle, it.repeat(cycle_idx),
                    it.repeat(method), it.repeat(max_displacement)))

        # Loop over generator and calculate frame alignments
        while True:
            try:
                next(map_generator)
            except StopIteration:
                break

    # TODO: align planes to minimize shifts between them
    pool.close()
    pool.join()

    def _align_planes(shifts):
        """Align planes to minimize shifts between them."""
        mean_shift = nanmean(list(it.chain(*it.chain(*shifts))), axis=0)
        # calculate alteration of shape (num_planes, dim)
        alteration = (mean_shift - mean_shift[0]).astype(int)
        for seq in shifts:
            seq -= alteration

    shifts = namespace.shifts
    _align_planes(shifts)
    return shifts, namespace.correlations


def _align_frame(inputs):
    """Aligns single frames and updates reference image.
    Called by _frame_alignment_correlation to parallelize the alignment

    Parameters
    ----------
    frame_idx : int
        The index of the current frame
    frame : array
        (num_planes, num_rows, num_columns, num_chanels) array of raw data
    cycle_idx : int
        The index of the current cycle
    method : string
        Method to use for correlation calculation
    max_displacement : list of int
        See motion.hmm

    There is no return, but shifts and correlations in the shared namespace
    are updated.

    """

    frame_idx, frame, cycle_idx, method, max_displacement = inputs

    # Pulls in the shared namespace and lock across all processes
    global namespace
    global lock

    def _update_sums_and_counts(
            pixel_sums, pixel_counts, offset, shift, plane, plane_idx):
        """Updates pixel sums and counts of the reference image each frame"""
        ref_indices = [offset + shift[plane_idx],
                       offset + shift[plane_idx] + plane.shape[:-1]]
        assert pixel_sums.ndim == 4
        pixel_counts[plane_idx][ref_indices[0][0]:ref_indices[1][0],
                                ref_indices[0][1]:ref_indices[1][1]
                                ] += np.isfinite(plane)
        pixel_sums[plane_idx][ref_indices[0][0]:ref_indices[1][0],
                              ref_indices[0][1]:ref_indices[1][1]
                              ] += np.nan_to_num(plane)
        assert pixel_sums.ndim == 4
        return pixel_sums, pixel_counts

    def _resize_arrays(shift, pixel_sums, pixel_counts, offset, frame_shape):
        """Enlarge storage arrays if necessary."""
        l = - np.minimum(0, shift + offset)
        r = np.maximum(
            # 0, shift + offset + np.array(sequences[0].shape[2:-1]) -
            0, shift + offset + np.array(frame_shape[1:-1]) -
            np.array(pixel_sums.shape[1:-1])
        )
        assert pixel_sums.ndim == 4
        if np.any(l > 0) or np.any(r > 0):
            # adjust Y
            pre_shape = (pixel_sums.shape[0], l[0]) + pixel_sums.shape[2:]
            post_shape = (pixel_sums.shape[0], r[0]) + pixel_sums.shape[2:]
            pixel_sums = np.concatenate(
                [np.zeros(pre_shape), pixel_sums, np.zeros(post_shape)],
                axis=1)
            pixel_counts = np.concatenate(
                [np.zeros(pre_shape), pixel_counts, np.zeros(post_shape)],
                axis=1)
            # adjust X
            pre_shape = pixel_sums.shape[:2] + (l[1], pixel_sums.shape[3])
            post_shape = pixel_sums.shape[:2] + (r[1], pixel_sums.shape[3])
            pixel_sums = np.concatenate(
                [np.zeros(pre_shape), pixel_sums, np.zeros(post_shape)],
                axis=2)
            pixel_counts = np.concatenate(
                [np.zeros(pre_shape), pixel_counts, np.zeros(post_shape)],
                axis=2)
            offset += l
        assert pixel_sums.ndim == 4
        assert np.prod(pixel_sums.shape) < 4 * np.prod(frame_shape)
        return pixel_sums, pixel_counts, offset

    for p, plane in zip(it.count(), frame):
        # if frame_idx in invalid_frames:
        #     correlations[i] = np.nan
        #     shifts[:, i] = np.nan
        with lock:
            any_check = np.any(namespace.pixel_counts[p])
            if not any_check:
                corrs = namespace.correlations
                corrs[cycle_idx][frame_idx][p] = 1
                namespace.correlations = corrs
                s = namespace.shifts
                s[cycle_idx][frame_idx][p][:] = 0
                namespace.shifts = s
                namespace.pixel_sums, namespace.pixel_counts = \
                    _update_sums_and_counts(
                        namespace.pixel_sums, namespace.pixel_counts,
                        namespace.offset,
                        namespace.shifts[cycle_idx][frame_idx], plane, p)
        if any_check:
            # recompute reference using all aligned images
            with lock:
                p_sums = namespace.pixel_sums[p]
                p_counts = namespace.pixel_counts[p]
                p_offset = namespace.offset
                shifts = namespace.shifts
            with warnings.catch_warnings():  # ignore divide by 0
                warnings.simplefilter("ignore")
                reference = p_sums / p_counts
            if method == 'correlation':
                if max_displacement is not None and np.all(
                        max_displacement > 0):
                    min_shift = np.min(list(it.chain(*it.chain(*shifts))),
                                       axis=0)
                    max_shift = np.max(list(it.chain(*it.chain(*shifts))),
                                       axis=0)
                    displacement_bounds = p_offset + np.array(
                        [np.minimum(max_shift - max_displacement, min_shift),
                         np.maximum(min_shift + max_displacement, max_shift)])
                else:
                    displacement_bounds = None
                shift, p_corr = align_cross_correlation(
                    reference, plane, displacement_bounds)
                if displacement_bounds is not None:
                    assert np.all(shift >= displacement_bounds[0])
                    assert np.all(shift <= displacement_bounds[1])
                    assert np.all(abs(shift - p_offset) <= max_displacement)
            elif method == 'ECC':
                raise NotImplementedError
                # cv2.findTransformECC(reference, plane)
            else:
                raise ValueError('Unrecognized alignment method')
            with lock:
                s = namespace.shifts
                s[cycle_idx][frame_idx][p][:] = shift - p_offset
                namespace.shifts = s
                corrs = namespace.correlations
                corrs[cycle_idx][frame_idx][p] = p_corr
                namespace.correlations = corrs

            with lock:
                namespace.pixel_sums, namespace.pixel_counts, namespace.offset\
                    = _resize_arrays(
                        namespace.shifts[cycle_idx][frame_idx][p],
                        namespace.pixel_sums, namespace.pixel_counts,
                        namespace.offset, frame.shape)
                namespace.pixel_sums, namespace.pixel_counts \
                    = _update_sums_and_counts(
                        namespace.pixel_sums, namespace.pixel_counts,
                        namespace.offset,
                        namespace.shifts[cycle_idx][frame_idx], plane, p)
