# This code computes DEMs by inverting the forward problem with sparse
# matrix methods. It can seamlessly incorporate multiple instruments,
# with regularization similar to the Plowman & Caspi 2020 method. Unlike
# that method, the regularization can be applied to both the spatial
# and thermal directions. Regularization strength can be tweaked
# along each axis independently. There is a multi-instrument example
# notebook supplied that demonstrates the method.

# NOTE: Sparse as used here means that it uses sparse matrix methods,
# Not (necessarily) that it uses 'sparsity' of the solution as a constraint
# a la the L1 norm used in Mark Cheung's sparse_em basis pursuit algorithm.
# This is a result of a name collision in the mathematical terminology that
# we can't really avoid here. Sparse is the natural term for when a linear
# operator has mostly zero entries, and the accepted mathematical one.
# And although the solver here can work on non-sparse operators, its
# power is in very large dimensional problems which are only tractable
# when they're sparse.

import numba, os, sys, time, pickle, resource, copy, numpy as np
from EMToolKit.schemas.operators import multi_instrument_linear_operator, sparse_d2_partial_matrix
from EMToolKit.schemas.operators import reg_operator_postfac_wrapper, single_instrument_linear_operator_separable
#from EMToolKit.schemas.coord_transform import coord_transform, trivialframe, basic_fits_transform
from EMToolKit.schemas.element_functions import (nd_voigt_psf, bin_function, get_2d_cov, get_3d_cov,
                               nd_gaussian_psf, nd_powgaussian_psf, spike_function,
                               flattop_guassian_psf, spice_spectrograph_psf)
from EMToolKit.schemas.element_grid import detector_grid, source_grid
from EMToolKit.schemas.coord_grid import coord_grid
from EMToolKit.schemas.element_source_responses import element_source_responses as esr
from . import sparse_nlmap_solver
from EMToolKit.schemas.basic_schemas import basic_detector, basic_source

def minmax(arg):
	return([np.min(arg),np.max(arg)])

# datasequence can behave in a list-like fashion with
# n elements that behave as sunpy maps that have the following
# 	data
# 	uncertainty.array (same shape as data)
# 	wcs
#	observer_coordinate
# meta including temperature response, log temperature array
# and exposure time
# The spatial response of each pixel must be either supplied
# or estimated based on the wcs. For instruments that do not
# have spatially localized detector elements (e.g., RHESSI),
# this will presumably have to be supplied.
# For instruments where the spatial and temperature response
# are not separable (e.g., overlappographs), this will need
# to be indicated in the meta, and the responses will
# need to be supplied, methods for computed them provided, or
# a standard way of defining them must be developed.
# The current baseline in EMToolKit assumes localized detector
# elements and only provides wcs for spatial information.
# We should begin by implementing a backward compatible layer
# for that.
def sparse_nlmap_dem_wrapper(datasequence, wrapargs={}):
	nc = len(datasequence)
	nc = len(datasequence)
	drv_con = wrapargs.get('drv_con',8)
	dtype = wrapargs.get('dtype',np.float32)
	norms = wrapargs.get('norms',np.ones(nc))
	overall_norm = wrapargs.get('overall_norm',1)

	# If there is no spatial/thermal model scheme in wrapargs
	# try to make one based on the world coordinate systems:
	source = wrapargs.get('source', basic_source(datasequence))
	src_dims_all = source.shape
	ndims = len(src_dims_all)
	steps = []
	for i in range(0,ndims): steps.append((source.axes[i][1:]-source.axes[i][0:-1]))

	reg_steps = copy.deepcopy(steps)
	for i in range(1,len(reg_steps)): reg_steps[i] /= 60

	reg_operator = sparse_d2_partial_matrix(src_dims_all, 0, nc, steps=reg_steps[0], drv_con=drv_con, dtype=dtype, use_postfactor=False)
	for i in range(1,ndims):
		reg_operator += sparse_d2_partial_matrix(src_dims_all, i, nc, steps=reg_steps[i],
																	  drv_con=drv_con, dtype=dtype, use_postfactor=False)

	# fwdops = []
	# for i in range(0,nc):
	# 	# Need to check to see if the metadata for each data element
	# 	# has a forward operator and whether or not it's the correct
	# 	# one for the model. We'll leave it to the data element to check
	# 	# that and compute one if it's not there:
	# 	print(f"Running on image {i+1} of {nc}" )
	# 	fwdops.append((datasequence[i].meta['SCHEMA']).fwdop(source))
	# 	#fwdops.append(model.get_fwdop(datasequence[i]))

	import concurrent.futures



	# Assuming datasequence, source, and nc are already defined
	fwdops = []
	with concurrent.futures.ProcessPoolExecutor() as executor:
		# Submit tasks to the executor
		futures = [executor.submit(process_data_element, i, datasequence[i], source) for i in range(nc)]

		# Retrieve results in the order they were submitted
		for future in futures:
			try:
				result, ii = future.result()
				fwdops.append(result)
				# print(f"Got result for image {ii + 1}")
			except Exception as e:
				print(f"An error occurred: {e}")
				fwdops.append(None)  # or handle the error as appropriate

	fwd_operator = multi_instrument_linear_operator(fwdops, wrapargs=wrapargs)
	data, errors = [[],[]]
	for i in range(0,nc):
		errors.append(datasequence[i].uncertainty.array.flatten().astype(dtype))
		errors[i] = np.clip(errors[i],np.min(errors[i][errors[i] > 0]),None)/norms[i]/overall_norm
		data.append(np.clip(datasequence[i].data.astype(dtype).flatten(),0.0,None)/norms[i]/overall_norm)


	data, errors = [np.hstack(data),np.hstack(errors)]
	print(np.max(data),np.max(errors),np.min(data),np.min(errors))

	soln = sparse_nlmap_solver.solve(data, errors, fwd_operator, guess=None, reg_fac=np.float32(1), adapt_lam=False, dtype=dtype,
							   regmat=reg_operator, map_reg=True, solver_tol=np.float32(50.0e-5), silent=False, chi2_th=1.0,
							   niter=10)#, steps = steps)

	dem_soln = soln[0].reshape(source.shape)*overall_norm
	alg_object = sparse_nlmap_dem_wrap_object(wrapargs,source=source)
	return list(dem_soln), source.logts, source.bases, source.wcs, 'sparse_multi_instrument', alg_object

def process_data_element(i, element, source):
	# print(f"Running on image {i + 1}")
	return (element.meta['SCHEMA']).fwdop(source), i
	# return model.get_fwdop(element)  # Uncomment if needed

class sparse_nlmap_dem_wrap_object(object):

	def __init__(self,wrapargs,source=None):
		self.wrapargs = copy.deepcopy(wrapargs)
		if(source is not None):
			self.wrapargs['source'] = copy.deepcopy(source)
			self.meta = source.meta

	def compute_dem(self,datasequence,wrapargs=None):
		if(wrapargs is None): wrapargs = self.wrapargs
		return sparse_multi_instrument_dem_wrapper(datasequence, wrapargs=wrapargs)
