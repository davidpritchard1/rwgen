import os
import itertools

import numpy as np
import scipy.stats
import scipy.spatial
import gstools
import numba

from . import nsproc
from . import utils


def main(
        spatial_model,
        intensity_distribution,
        discretisation_method,  # 'default' or 'event_totals'
        output_types,
        output_folder,
        output_subfolders,
        output_format,
        season_definitions,
        parameters,
        points,
        catchments,
        grid,
        epsg_code,
        cell_size,
        dem,
        phi,
        simulation_length,
        number_of_realisations,
        timestep_length,
        start_year,
        calendar,
        random_seed,
        additional_output  # TODO: Make this output mandatory?
):
    # Initialisations common to both point and spatial models (derived attributes)
    realisation_ids = range(1, number_of_realisations + 1)
    output_paths = make_output_paths(
        spatial_model, output_types, output_format, output_folder, output_subfolders, points, catchments,
        realisation_ids
    )
    if random_seed is None:
        seed_sequence = np.random.SeedSequence()
    else:
        seed_sequence = np.random.SeedSequence(random_seed)

    # Most of the preparation needed for simulation is only for a spatial model  # TODO: Check each case
    if spatial_model:

        # Set (inner) simulation domain bounds
        xmin, ymin, xmax, ymax = identify_domain_bounds(grid, cell_size, points)

        # Set up discretisation point location metadata arrays (x, y and z by point)
        discretisation_metadata = create_discretisation_metadata_arrays(points, grid, cell_size, dem)

        # Associate a phi value with each point
        unique_seasons = utils.identify_unique_seasons(season_definitions)
        discretisation_metadata = get_phi(unique_seasons, dem, phi, output_types, discretisation_metadata)

        # Get weights associated with catchments for each point
        if 'catchment' in output_types:
            discretisation_metadata = get_catchment_weights(
                grid, catchments, cell_size, epsg_code, discretisation_metadata, output_types, dem,
                unique_seasons, catchment_id_field='ID'
            )

    else:
        xmin = None
        ymin = None
        xmax = None
        ymax = None
        discretisation_metadata = None

    # Do simulation
    for realisation_id in realisation_ids:
        if discretisation_method == 'default':
            simulate_realisation(
                realisation_id, start_year, simulation_length, timestep_length, season_definitions, calendar,
                discretisation_method, spatial_model, output_types, discretisation_metadata, points, catchments,
                parameters, intensity_distribution, seed_sequence, xmin, xmax, ymin, ymax, output_paths
            )
        elif discretisation_method == 'event_totals':
            df = simulate_realisation(
                realisation_id, start_year, simulation_length, timestep_length, season_definitions, calendar,
                discretisation_method, spatial_model, output_types, discretisation_metadata, points, catchments,
                parameters, intensity_distribution, seed_sequence, xmin, xmax, ymin, ymax, output_paths
            )

    # TODO: Implement additional output - phi, catchment weights, random seed


def identify_domain_bounds(grid, cell_size, points):
    """
    Set (inner) simulation domain bounds as maximum extent of output points and grid (if required).

    """
    if grid is not None:  # accounts for both catchment and grid outputs
        grid_xmin, grid_ymin, grid_xmax, grid_ymax = utils.grid_limits(grid)
    if points is not None:
        points_xmin = np.min(points['easting'])
        points_ymin = np.min(points['northing'])
        points_xmax = np.max(points['easting'])
        points_ymax = np.max(points['northing'])
        if grid is not None:
            xmin = np.minimum(points_xmin, grid_xmin)
            ymin = np.minimum(points_ymin, grid_ymin)
            xmax = np.maximum(points_xmax, grid_xmax)
            ymax = np.maximum(points_ymax, grid_ymax)
        else:
            xmin = points_xmin
            ymin = points_ymin
            xmax = points_xmax
            ymax = points_ymax
    xmin = utils.round_down(xmin, cell_size)
    ymin = utils.round_down(ymin, cell_size)
    xmax = utils.round_up(xmax, cell_size)
    ymax = utils.round_up(ymax, cell_size)
    return xmin, ymin, xmax, ymax


def create_discretisation_metadata_arrays(points, grid, cell_size, dem):
    """
    Set up discretisation point location metadata arrays (x, y and z by point).
    
    """
    # Dictionary with keys as tuples of output type and metadata attribute (values as arrays)
    discretisation_metadata = {}

    # Point metadata values are arrays of length one
    if points is not None:
        discretisation_metadata[('point', 'x')] = points['easting'].values
        discretisation_metadata[('point', 'y')] = points['northing'].values
        if 'elevation' in points.columns:
            discretisation_metadata[('point', 'z')] = points['elevation'].values

    # For a grid these arrays are flattened 2D arrays so that every point has an associated x, y pair
    if grid is not None:
        x = np.arange(
            grid['xllcorner'] + cell_size / 2.0,
            grid['xllcorner'] + grid['ncols'] * cell_size,
            cell_size
        )
        y = np.arange(
            grid['yllcorner'] + cell_size / 2.0,
            grid['yllcorner'] + grid['nrows'] * cell_size,
            cell_size
        )
        y = y[::-1]  # reverse to get north-south order

        # Meshgrid then flatten gets each xy pair
        xx, yy = np.meshgrid(x, y)
        xf = xx.flatten()
        yf = yy.flatten()
        discretisation_metadata[('grid', 'x')] = xf
        discretisation_metadata[('grid', 'y')] = yf

        # Resample DEM to grid resolution (presumed coarser) if DEM present
        if dem is not None:
            dem_cell_size = dem.x.values[1] - dem.x.values[0]
            window = int(cell_size / dem_cell_size)

            # Restrict DEM to domain of output grid
            grid_xmin, grid_ymin, grid_xmax, grid_ymax = utils.grid_limits(grid)
            mask_x = (dem.x > grid_xmin) & (dem.x < grid_xmax)
            mask_y = (dem.y > grid_ymin) & (dem.y < grid_ymax)
            dem = dem.where(mask_x & mask_y, drop=True)

            # Boundary argument required to avoid case where DEM does not match grid neatly
            resampled_dem = dem.coarsen(x=window, boundary='pad').mean(skipna=True) \
                .coarsen(y=window, boundary='pad').mean(skipna=True)
            flat_resampled_dem = resampled_dem.data.flatten()
            discretisation_metadata[('grid', 'z')] = flat_resampled_dem

    return discretisation_metadata


def get_phi(unique_seasons, dem, phi, output_types, discretisation_metadata):
    """
    Associate a phi value with each discretisation point.

    """
    # Calculate phi for each discretisation point for all output types using interpolation (unless a point is in the
    # dataframe of known phi, in which case use it directly)
    for season in unique_seasons:

        # Make interpolator (flag needed for whether phi should be log-transformed)
        if dem is not None:
            interpolator, log_transformation = make_phi_interpolator(phi.loc[phi['season'] == season])
        else:
            interpolator, log_transformation = make_phi_interpolator(
                phi.loc[phi['season'] == season], include_elevation=False
            )

        # Estimate phi for points and grid (if phi is known at point location then exact value should be preserved)
        for output_type in list(set(output_types) & set(['point', 'grid'])):
            if dem is not None:
                interpolated_phi = interpolator(
                    (discretisation_metadata[(output_type, 'x')], discretisation_metadata[(output_type, 'y')]),
                    mesh_type='unstructured',
                    ext_drift=discretisation_metadata[(output_type, 'z')],
                    return_var=False
                )
            else:
                interpolated_phi = interpolator(
                    (discretisation_metadata[(output_type, 'x')], discretisation_metadata[(output_type, 'y')]),
                    mesh_type='unstructured',
                    return_var=False
                )
            if log_transformation:
                discretisation_metadata[(output_type, 'phi', season)] = np.exp(interpolated_phi)
            else:
                discretisation_metadata[(output_type, 'phi', season)] = interpolated_phi
            discretisation_metadata[(output_type, 'phi', season)] = np.where(
                discretisation_metadata[(output_type, 'phi', season)] < 0.0,
                0.0,
                discretisation_metadata[(output_type, 'phi', season)]
            )

    return discretisation_metadata


def make_phi_interpolator(df1, include_elevation=True, distance_bins=7):
    """
    Make function to interpolate phi, optionally accounting for elevation dependence if significant.

    """
    # Test for elevation-dependence of phi using linear regression (trying untransformed and log-transformed phi)
    if include_elevation:
        untransformed_regression = scipy.stats.linregress(df1['elevation'], df1['phi'])
        log_transformed_regression = scipy.stats.linregress(df1['elevation'], np.log(df1['phi']))
        if (untransformed_regression.pvalue < 0.05) or (log_transformed_regression.pvalue < 0.05):
            significant_regression = True
            if untransformed_regression.rvalue >= log_transformed_regression.rvalue:
                log_transformation = False
            else:
                log_transformation = True
        else:
            significant_regression = False
    else:
        significant_regression = False
        log_transformation = False

    # Select regression model (untransformed or log-transformed) if significant (linear) elevation dependence
    if log_transformation:
        phi = np.log(df1['phi'])
        if significant_regression:
            regression_model = log_transformed_regression
    else:
        phi = df1['phi'].values
        if significant_regression:
            regression_model = untransformed_regression

    # Remove elevation signal from data first to allow better variogram fit
    if include_elevation and significant_regression:
        detrended_phi = phi - (df1['elevation'] * regression_model.slope + regression_model.intercept)

    # Calculate bin edges
    max_distance = np.max(scipy.spatial.distance.pdist(np.asarray(df1[['easting', 'northing']])))
    interval = max_distance / distance_bins
    bin_edges = np.arange(0.0, max_distance + 0.1, interval)
    bin_edges[-1] = max_distance + 0.1  # ensure that all points covered

    # Estimate empirical variogram
    if include_elevation and significant_regression:
        bin_centres, gamma, counts = gstools.vario_estimate(
            (df1['easting'].values, df1['northing'].values), detrended_phi, bin_edges, return_counts=True
        )
    else:
        bin_centres, gamma, counts = gstools.vario_estimate(
            (df1['easting'].values, df1['northing'].values), phi, bin_edges, return_counts=True
        )
    bin_centres = bin_centres[counts > 0]
    gamma = gamma[counts > 0]

    # Identify best fit from exponential and spherical covariance models
    exponential_model = gstools.Exponential(dim=2)
    _, _, exponential_r2 = exponential_model.fit_variogram(bin_centres, gamma, nugget=False, return_r2=True)
    spherical_model = gstools.Spherical(dim=2)
    _, _, spherical_r2 = spherical_model.fit_variogram(bin_centres, gamma, nugget=False, return_r2=True)
    if exponential_r2 > spherical_r2:
        covariance_model = exponential_model
    else:
        covariance_model = spherical_model

    # Instantiate appropriate kriging object
    if include_elevation and significant_regression:
        phi_interpolator = gstools.krige.ExtDrift(
            covariance_model, (df1['easting'].values, df1['northing'].values), phi, df1['elevation'].values
        )
    else:
        phi_interpolator = gstools.krige.Ordinary(
            covariance_model, (df1['easting'].values, df1['northing'].values), phi
        )

    return phi_interpolator, log_transformation


def get_catchment_weights(
        grid, catchments, cell_size, epsg_code, discretisation_metadata, output_types, dem,
        unique_seasons, catchment_id_field
):
    """
    Catchment weights as contribution of each (grid) point to catchment-average.

    """
    # First get weight for every point for every catchment
    grid_xmin, grid_ymin, grid_xmax, grid_ymax = utils.grid_limits(grid)
    catchment_points = utils.catchment_weights(
        catchments, grid_xmin, grid_ymin, grid_xmax, grid_ymax, cell_size, id_field=catchment_id_field,
        epsg_code=epsg_code
    )
    for catchment_id, point_arrays in catchment_points.items():
        # Check that points are ordered in the same way
        assert np.min(point_arrays['x'] == discretisation_metadata[('grid', 'x')]) == 1
        assert np.min(point_arrays['y'] == discretisation_metadata[('grid', 'y')]) == 1
        # TODO: Replace checks on array equivalence with dataframe merge operation
        discretisation_metadata[('catchment', 'weights', catchment_id)] = point_arrays['weight']

    # Then rationalise grid discretisation points - if a point is not used by any catchment and grid output is not
    # required then no need to discretise it
    if ('catchment' in output_types) and ('grid' not in output_types):

        # Identify cells where any subcatchment is present (i.e. overall catchment mask)
        catchment_mask = np.zeros(discretisation_metadata[('grid', 'x')].shape[0], dtype=bool)
        for catchment_id in catchment_points.keys():
            subcatchment_mask = discretisation_metadata[('catchment', 'weights', catchment_id)] > 0.0
            catchment_mask[subcatchment_mask == 1] = 1

        # Subset static (non-seasonally varying) arrays - location, elevation and weights
        discretisation_metadata[('grid', 'x')] = discretisation_metadata[('grid', 'x')][catchment_mask]
        discretisation_metadata[('grid', 'y')] = discretisation_metadata[('grid', 'y')][catchment_mask]
        if dem is not None:
            discretisation_metadata[('grid', 'z')] = discretisation_metadata[('grid', 'z')][catchment_mask]
        for catchment_id in catchment_points.keys():
            discretisation_metadata[('catchment', 'weights', catchment_id)] = (
                discretisation_metadata[('catchment', 'weights', catchment_id)][catchment_mask]
            )

        # Subset seasonally varying arrays - phi
        for season in unique_seasons:
            discretisation_metadata[('grid', 'phi', season)] = (
                discretisation_metadata[('grid', 'phi', season)][catchment_mask]
            )

    return discretisation_metadata


def make_output_paths(
        spatial_model, output_types, output_format, output_folder, output_subfolders, points, catchments,
        realisation_ids
):
    output_paths = {}
    for output_type in output_types:
        if output_type == 'grid':
            paths = output_paths_helper(
                spatial_model, output_type, 'nc', output_folder, output_subfolders, points, catchments, realisation_ids
            )
        else:
            paths = output_paths_helper(
                spatial_model, output_type, output_format, output_folder, output_subfolders, points, catchments,
                realisation_ids
            )
        for key, value in paths.items():
            output_paths[key] = value
    return output_paths


def output_paths_helper(
        spatial_model, output_type, output_format, output_folder, output_subfolders, points, catchments,
        realisation_ids
):
    output_format_extensions = {'csv': '.csv', 'csvy': '.csvy', 'txt': '.txt', 'netcdf': '.nc'}

    if output_type == 'point':
        if spatial_model:
            location_ids = points['name'].values
        else:
            location_ids = [1]
        output_subfolder = os.path.join(output_folder, output_subfolders['point'])
    elif output_type == 'catchment':
        location_ids = catchments['name'].values
        output_subfolder = os.path.join(output_folder, output_subfolders['catchment'])
    elif output_type == 'grid':
        location_ids = [1]
        output_subfolder = os.path.join(output_folder, output_subfolders['grid'])

    output_paths = {}
    output_cases = itertools.product(realisation_ids, location_ids)
    for realisation_id, location_id in output_cases:
        if not spatial_model:
            output_file_name = 'r' + str(realisation_id) + output_format_extensions[output_format]
        else:
            output_file_name = location_id + '_r' + str(realisation_id) + output_format_extensions[output_format]
        output_path_key = (output_type, location_id, realisation_id)
        output_paths[output_path_key] = os.path.join(output_subfolder, output_file_name)

    return output_paths


def simulate_realisation(
        realisation_id, start_year, number_of_years, timestep_length, season_definitions, calendar,
        discretisation_method, spatial_model, output_types, discretisation_metadata, points, catchments, parameters,
        intensity_distribution, seed_sequence, xmin, xmax, ymin, ymax, output_paths
):
    """
    Simulate realisation of NSRP process.

    """
    print('    - Realisation =', realisation_id)

    # Get datetime series and end year
    end_year = start_year + number_of_years - 1
    datetimes = utils.datetime_series(start_year, end_year, timestep_length, season_definitions, calendar)

    # Helper dataframe with month end timesteps and times
    datetime_helper = datetimes.groupby(['year', 'month'])['hour'].agg('size')
    datetime_helper = datetime_helper.to_frame('n_timesteps')
    datetime_helper.reset_index(inplace=True)
    datetime_helper['end_timestep'] = datetime_helper['n_timesteps'].cumsum()  # beginning timestep of next month
    datetime_helper['start_timestep'] = datetime_helper['end_timestep'].shift()
    datetime_helper.iloc[0, datetime_helper.columns.get_loc('start_timestep')] = 0
    datetime_helper['start_time'] = datetime_helper['start_timestep'] * timestep_length
    datetime_helper['end_time'] = datetime_helper['end_timestep'] * timestep_length
    datetime_helper['n_hours'] = datetime_helper['end_time'] - datetime_helper['start_time']

    # Initialise arrays according to discretisation method
    if discretisation_method == 'default':  # one-month blocks
        discrete_rainfall = initialise_discrete_rainfall_arrays(
            spatial_model, output_types, discretisation_metadata, points, int((24 / timestep_length) * 31)
        )
    # TODO: Consider whether arrays for point or whole-domain event totals need to be initialised here

    # Identify size of blocks (number of years) needed to avoid potential memory issues in simulations. Test both (1)
    # whether arrays needed to store point/catchment numbers for writing can be assigned and (2) whether NSRP process
    # simulation can complete
    # TODO: Consider making this an optional check - add flag as argument?
    rng = np.random.default_rng(seed_sequence)
    block_size = min(number_of_years, 1000)  # TODO: Reasonable choice? 500?
    block_id = 0
    while block_id * block_size < number_of_years:
        idx1 = block_id * block_size * 12
        idx2 = (block_id + 1) * block_size * 12
        month_lengths = datetime_helper['n_hours'].values[idx1:idx2]  # TODO: Slice is new - check
        try:
            dummy1 = nsproc.main(
                spatial_model, parameters, number_of_years, month_lengths, season_definitions, intensity_distribution,
                rng, xmin, xmax, ymin, ymax
            )
            if discretisation_method == 'default':
                # TODO: Check calculations of number of timesteps and points required here
                block_start_year = start_year + block_id * block_size
                n_timesteps = datetimes.loc[
                    (datetimes['year'] >= block_start_year) & (datetimes['year'] < (block_start_year + 100))
                ].shape[0]
                n_points = points.shape[0] * catchments.shape[0]
                dummy2 = np.zeros((n_timesteps * n_points), dtype=np.float16) + 1
                dummy2 = 0
            elif discretisation_method == 'event_totals':
                if block_id == 0:
                    dummy2 = np.zeros((dummy1.shape[0], 3))
                    # TODO: Check that storm ID, arrival time and total are sufficient
                    # TODO: Also consider assigning as dataframe, as ID can be integer and total can be float16
                else:
                    dummy2 = np.concatenate([dummy2, np.zeros((dummy1.shape[0], 3))])
            block_id += 1
        except MemoryError:
            block_size = int(np.floor(block_size / 2))
            block_id = 0
            rng = np.random.default_rng(seed_sequence)
            dummy1 = 0
            dummy2 = 0
    dummy1 = 0
    dummy2 = 0

    # Simulate and discretise NSRP process by block
    rng = np.random.default_rng(seed_sequence)
    block_id = 0
    while block_id * block_size < number_of_years:

        # NSRP process simulation
        idx1 = block_id * block_size * 12
        idx2 = (block_id + 1) * block_size * 12
        month_lengths = datetime_helper['n_hours'].values[idx1:idx2]  # TODO: Slice is new - check
        df = nsproc.main(
            spatial_model, parameters, number_of_years, month_lengths, season_definitions, intensity_distribution,
            rng, xmin, xmax, ymin, ymax
        )

        # Convert raincell coordinates and radii from km to m for discretisation
        if 'raincell_x' in df.columns:
            df['raincell_x'] *= 1000.0
            df['raincell_y'] *= 1000.0
            df['raincell_radii'] *= 1000.0

        # Discretisation
        if discretisation_method == 'default':
            discretise_by_point(
                spatial_model, datetime_helper.iloc[idx1:idx2], season_definitions, df, output_types, timestep_length,
                discrete_rainfall,
                discretisation_metadata, datetimes, points, catchments, realisation_id, output_paths
            )
            # TODO: Check that slice of datetime_helper is correct
        elif discretisation_method == 'event_totals':
            events_df = discretise_by_event()  # TODO: Not yet implemented

        block_id += 1

    # Assuming that event totals etc are not being written to file but should be returned for shuffling etc
    if discretisation_method == 'event_totals':
        return events_df


def initialise_discrete_rainfall_arrays(spatial_model, output_types, discretisation_metadata, points, nt):
    dc = {}
    if 'point' in output_types:
        if spatial_model:
            dc['point'] = np.zeros((nt, points.shape[0]))
        else:
            dc['point'] = np.zeros((nt, 1))
    if ('catchment' in output_types) or ('grid' in output_types):
        dc['grid'] = np.zeros((nt, discretisation_metadata[('grid', 'x')].shape[0]))
    return dc


def discretise_by_point(
        spatial_model, datetime_helper, season_definitions, df, output_types, timestep_length, discrete_rainfall,
        discretisation_metadata, datetimes, points, catchments, realisation_id, output_paths
):
    # TODO: Expecting datetime_helper just for block - check that correctly subset before argument passed

    # Prepare to store realisation output for block (point and catchment output only)
    output_arrays = {}

    # Looping time series of months
    for month_idx in range(datetime_helper.shape[0]):
        year = datetime_helper['year'].values[month_idx]
        month = datetime_helper['month'].values[month_idx]
        season = season_definitions[month]

        # Perform temporal subset before discretising points (much more efficient for spatial model)
        start_time = datetime_helper['start_time'][month_idx]
        end_time = datetime_helper['end_time'][month_idx]
        temporal_mask = (df['raincell_arrival'].values < end_time) & (df['raincell_end'].values > start_time)
        raincell_arrival_times = df['raincell_arrival'].values[temporal_mask]
        raincell_end_times = df['raincell_end'].values[temporal_mask]
        raincell_intensities = df['raincell_intensity'].values[temporal_mask]

        # Spatial model discretisation requires temporal subset of additional raincell properties
        if spatial_model:
            raincell_x = df['raincell_x'].values[temporal_mask]
            raincell_y = df['raincell_y'].values[temporal_mask]
            raincell_radii = df['raincell_radii'].values[temporal_mask]

            # If both catchment and grid are in output types then the same grid is used so only need to do once
            if ('catchment' in output_types) and ('grid' in output_types):
                _output_types = list(set(output_types) & set(['point', 'catchment']))
            else:
                _output_types = output_types
            for output_type in _output_types:
                if output_type == 'catchment':
                    discretisation_case = 'grid'
                else:
                    discretisation_case = output_type
                discretise_spatial(
                    start_time, end_time, timestep_length, raincell_arrival_times, raincell_end_times,
                    raincell_intensities, discrete_rainfall[discretisation_case],
                    raincell_x, raincell_y, raincell_radii,
                    discretisation_metadata[(discretisation_case, 'x')],
                    discretisation_metadata[(discretisation_case, 'y')],
                    discretisation_metadata[(discretisation_case, 'phi', season)],
                )
        else:
            discretise_point(
                start_time, end_time, timestep_length, raincell_arrival_times, raincell_end_times,
                raincell_intensities, discrete_rainfall['point'][:, 0]
            )

        # Find number of timesteps in month to be able to subset the discretised arrays (if < 31 days in current month)
        month_datetimes = datetimes.loc[(datetimes['year'] == year) & (datetimes['month'] == month)]
        timesteps_in_month = month_datetimes.shape[0]

        # Put discrete rainfall in arrays ready for writing once all block available
        for output_type in output_types:
            if output_type == 'point':
                if not spatial_model:
                    location_ids = [1]
                else:
                    location_ids = points['name'].values  # self.points['point_id'].values
            elif output_type == 'catchment':
                location_ids = catchments['name'].values  # self.catchments[self.catchment_id_field].values
            elif output_type == 'grid':
                location_ids = [1]
            # TODO: See if output keys can be looped directly without needing to figure out location_ids again

            # TODO: Reduce dependence on list/array order
            idx = 0
            for location_id in location_ids:
                output_key = (output_type, location_id, realisation_id)

                if output_type == 'point':
                    output_array = discrete_rainfall['point'][:timesteps_in_month, idx]
                elif output_type == 'catchment':
                    catchment_discrete_rainfall = np.average(
                        discrete_rainfall['grid'], axis=1,
                        weights=discretisation_metadata[('catchment', 'weights', location_id)]
                    )
                    output_array = catchment_discrete_rainfall[:timesteps_in_month]
                elif output_type == 'grid':
                    raise NotImplementedError('Grid output not implemented yet')

                # Try concatenating arrays in first instance - could be changed so that upfront initialisation
                if month_idx == 0:
                    output_arrays[output_key] = output_array.astype(np.float16)
                else:
                    output_arrays[output_key] = np.concatenate(
                        [output_arrays[output_key], output_array.astype(np.float16)]
                    )

                idx += 1

    # Write output
    write_output(output_arrays, output_paths)


@numba.jit(nopython=True)
def discretise_point(
        period_start_time, timestep_length, raincell_arrival_times, raincell_end_times,
        raincell_intensities, discrete_rainfall
):
    # Modifying the discrete rainfall arrays themselves so need to ensure zeros before starting
    discrete_rainfall.fill(0.0)

    # Discretise each raincell in turn
    for idx in range(raincell_arrival_times.shape[0]):

        # Times relative to period start
        rc_arrival_time = raincell_arrival_times[idx] - period_start_time
        rc_end_time = raincell_end_times[idx] - period_start_time
        rc_intensity = raincell_intensities[idx]

        # Timesteps relative to period start
        rc_arrival_timestep = int(np.floor(rc_arrival_time / timestep_length))
        rc_end_timestep = int(np.floor(rc_end_time / timestep_length))  # timestep containing end

        # Proportion of raincell in each relevant timestep
        for timestep in range(rc_arrival_timestep, rc_end_timestep+1):
            timestep_start_time = timestep * timestep_length
            timestep_end_time = (timestep + 1) * timestep_length
            effective_start = np.maximum(rc_arrival_time, timestep_start_time)
            effective_end = np.minimum(rc_end_time, timestep_end_time)
            timestep_coverage = effective_end - effective_start

            if timestep < discrete_rainfall.shape[0]:
                discrete_rainfall[timestep] += rc_intensity * timestep_coverage


@numba.jit(nopython=True)
def discretise_spatial(
        period_start_time, timestep_length, raincell_arrival_times, raincell_end_times,
        raincell_intensities, discrete_rainfall,
        raincell_x_coords, raincell_y_coords, raincell_radii,
        point_eastings, point_northings, point_phi,  # point_ids,
):
    # Modifying the discrete rainfall arrays themselves so need to ensure zeros before starting
    discrete_rainfall.fill(0.0)

    # Subset raincells based on whether they intersect the point being discretised
    for idx in range(point_eastings.shape[0]):
        x = point_eastings[idx]
        y = point_northings[idx]
        yi = idx

        distances_from_raincell_centres = np.sqrt((x - raincell_x_coords) ** 2 + (y - raincell_y_coords) ** 2)
        spatial_mask = distances_from_raincell_centres <= raincell_radii

        discretise_point(
            period_start_time, timestep_length, raincell_arrival_times[spatial_mask],
            raincell_end_times[spatial_mask], raincell_intensities[spatial_mask], discrete_rainfall[:, yi]
        )

        discrete_rainfall[:, yi] *= point_phi[idx]


def write_output(output_arrays, output_paths):
    for output_key, output_array in output_arrays.items():
        # output_type, location_id, realisation_id = output_key
        output_path = output_paths[output_key]
        values = []
        for value in output_array:
            values.append(('%.1f' % value).rstrip('0').rstrip('.'))  # + '\n'
        output_lines = '\n'.join(values)
        with open(output_path, 'a') as fh:
            fh.writelines(output_lines)
        # TODO: Implement other text file output options


def discretise_by_event():
    raise NotImplementedError