"""Functionality for injecting data into SWMM input files."""

from collections import Counter, defaultdict
import json
import logging

import shapely.geometry

from . import config as cfg
from . import units
from .swmm import input as si
from .swmm import input_reader as sir
from .swmm import input_writer as siw

input_parameters_schema_path = None
"""The path to the JSON Schema used to validate input parameters."""


def extract_subcatchment_polygons(swmm_input):
    """Extract subcatchment polygons from SWMM input.

    Args:
        swmm_input (dict): The input to extract subcatchment polygons from.

    Returns:
        dict: A mapping of subcatchment names to Shapely polygons.
    """
    subcatchment_polygon_coordinates = defaultdict(list)

    polygon_sc_index = si.data_indices['POLYGONS']['Subcat']
    polygon_x_index = si.data_indices['POLYGONS']['Xcoord']
    polygon_y_index = si.data_indices['POLYGONS']['Ycoord']

    swmm_input_polygon_lines = swmm_input.get('POLYGONS', {
        'lines': [],
    })['lines']
    for line in swmm_input_polygon_lines:
        values = line['values']
        if not values:
            continue
        subcatchment_polygon_coordinates[values[polygon_sc_index]].append(
            (
                float(values[polygon_x_index]),
                float(values[polygon_y_index]),
            ),
        )

    return {
        subcatchment: shapely.geometry.Polygon(coordinates)
        for subcatchment, coordinates
        in subcatchment_polygon_coordinates.iteritems()
    }


def get_subcatchment_definition(swmm_input, sc_name):
    """Get a subcatchment definition from SWMM input.

    Args:
        swmm_input (dict): The input to extract a subcatchment definition from.
        sc_name (string): The name of the subcatchment to extract.

    Returns:
        list|None: The input file definition for the subcatchment or None if
            not found.
    """
    if 'SUBCATCHMENTS' not in swmm_input:
        return None

    sc_name_index = si.data_indices['SUBCATCHMENTS']['Name']
    return next((
        line['values']
        for line
        in swmm_input['SUBCATCHMENTS']['lines']
        if line['values'] and line['values'][sc_name_index] == sc_name
    ), None)


def get_subcatchment_from_map_coords(coordinates, subcatchments):
    """Get the subcatchment a set of map coordinates falls within.

    Args:
        coordinates (dict): A set of map coordinates. (Keys are x, y.)
        subcatchments (dict): A mapping of subcatchments to Shapely Polygons.

    Returns:
        string: The subcatchment the point falls within.

    Raises:
        ValueError: The point did not fall within any given subcatchments.
    """
    point = shapely.geometry.Point(coordinates['x'], coordinates['y'])
    for subcatchment, polygon in subcatchments.iteritems():
        if polygon.contains(point):
            return subcatchment

    raise ValueError(
        "Coordinates ({0}, {1}) not found in subcatchments.".format(
            point.x,
            point.y,
        )
    )


def inject_parameters_into_input(input_parameters, input_template):
    """Inject parameters into a SWMM input template.

    Args:
        input_parameters (dict): The parameters to inject.
        input_template (dict): The input to inject parameters into.

    Raises:
        ConfigException: The configuration is invalid.
    """
    global input_parameters_schema_path
    if input_parameters_schema_path is None:
        input_parameters_schema_path = cfg.get_package_json_schema_path(
            'parameters.schema.json',
        )

    # Validate the input parameters against the JSON Schema.
    cfg.validate_against_json_schema(
        input_parameters,
        input_parameters_schema_path,
    )

    # Get useful input options.
    input_unit_system = si.get_unit_system(input_template)

    # Create a variable to hold the subcatchment polygons, but wait to populate
    # it until needed.
    sc_polygons = None

    # For each LID in the input parameters...
    lids = input_parameters.get("lids", [])
    lid_counter = Counter()
    for lid in lids:
        # If the location is given in map coordinates, convert to subcatchment.
        if 'map' in lid['location']:
            if sc_polygons is None:
                sc_polygons = extract_subcatchment_polygons(input_template)
            lid['location']['subcatchment'] = get_subcatchment_from_map_coords(
                lid['location']['map'],
                sc_polygons,
            )

        # If the drain point is given in map coordinates, convert to subcatch.
        if 'drainTo' in lid and 'map' in lid['drainTo']:
            if sc_polygons is None:
                sc_polygons = extract_subcatchment_polygons(input_template)
            lid['drainTo']['subcatchment'] = get_subcatchment_from_map_coords(
                lid['drainTo']['map'],
                sc_polygons,
            )

        # Get the LID's type.
        lid_type = lid['type']
        if 'LID_CONTROLS' not in input_template:
            raise cfg.ConfigException(
                'There are no LID controls defined in the SWMM input file.',
            )
        lid_controls = input_template['LID_CONTROLS']
        lid_type_name_index = si.data_indices['LID_CONTROLS']['Common']['Name']
        lid_type_definition = [
            line
            for line
            in lid_controls['lines']
            if line['values']
            and line['values'][lid_type_name_index] == lid_type
        ]
        if not lid_type_definition:
            raise cfg.ConfigException(
                'LID type "{0}" not found in SWMM input file.'.format(lid_type)
            )

        # Count this instance of this LID type and give it an ID.
        lid_counter[lid_type] += 1
        lid_id = '{0}_{1}'.format(lid_type, lid_counter[lid_type])

        # Adjust the LID's subcatchment as necessary.
        lid_type_type_index = si.data_indices['LID_CONTROLS']['Type']['Type']
        lid_type_type = lid_type_definition[0]['values'][lid_type_type_index]

        # If the LID is a rain barrel...
        if lid_type_type == 'RB':
            # Get the base subcatchment the barrel is located in.
            lid_base_sc_name = lid['location']['subcatchment']
            lid_base_sc = get_subcatchment_definition(
                input_template,
                lid_base_sc_name,
            )
            if not lid_base_sc:
                raise cfg.ConfigException(
                    'Subcatchment "{0}" not found.'.format(lid_base_sc_name)
                )

            # Generate a unique name for the LID's child subcatchment by
            # checking against the names of existing subcatchments.
            lid_sc_name_index = 0
            lid_sc_name = '{0}##{1}'.format(lid_base_sc_name, lid_id)
            existing_lid_sc = get_subcatchment_definition(
                input_template,
                lid_sc_name,
            )
            while existing_lid_sc:
                lid_sc_name_index += 1
                lid_sc_name = '{0}##{1}###{2}'.format(
                    lid_base_sc_name,
                    lid_id,
                    lid_sc_name_index,
                )
                existing_lid_sc = get_subcatchment_definition(
                    input_template,
                    lid_sc_name,
                )

            # Generate the child subcatchment for the LID and adjust the
            # base subcatchment's properties to compensate.
            lid_sc = list(lid_base_sc)
            lid_sc[si.data_indices['SUBCATCHMENTS']['Name']] = lid_sc_name

            if input_unit_system == 'US':
                lid_area_unit = units.registry.ft ** 2
                sc_area_unit = units.registry.acre
            elif input_unit_system == 'SI':
                lid_area_unit = units.registry.m ** 2
                sc_area_unit = units.registry.hectare
            else:
                raise cfg.ConfigException(
                    'Unknown unit system "{0}".'.format(input_unit_system),
                )

            lid_num_units = lid['number']
            lid_total_area = lid_num_units * lid['area'] * lid_area_unit

            sc_area_index = si.data_indices['SUBCATCHMENTS']['Area']
            lid_base_sc_area = float(lid_base_sc[sc_area_index]) * sc_area_unit

            lid_sc_area = lid_total_area.to(sc_area_unit)
            new_lid_base_sc_area = lid_base_sc_area - lid_sc_area

            lid_sc[sc_area_index] = lid_sc_area.magnitude
            lid_base_sc[sc_area_index] = new_lid_base_sc_area.magnitude
            if lid_base_sc[sc_area_index] < 0:
                raise cfg.ConfigException(
                    'LID "{0}" pushes subcatchment "{1}" below 0 area.'.format(
                        lid_id,
                        lid_base_sc_name,
                    )
                )

            sc_imperv_index = si.data_indices['SUBCATCHMENTS']['%Imperv']
            lid_base_sc_imperv = (
                float(lid_base_sc[sc_imperv_index])
                * units.registry.percent
            )
            lid_base_sc_imperv_area = lid_base_sc_imperv * lid_base_sc_area
            new_lid_base_sc_imperv_area = lid_base_sc_imperv_area - lid_sc_area
            new_lid_base_sc_imperv = (
                new_lid_base_sc_imperv_area / new_lid_base_sc_area
            )

            lid_sc[sc_imperv_index] = 0
            lid_base_sc[sc_imperv_index] = (
                new_lid_base_sc_imperv.to('percent').magnitude
            )
            if lid_base_sc[sc_imperv_index] < 0:
                raise cfg.ConfigException(
                    (
                        'Subcatchment "{1}" does not have enough impervious '
                        'land left to hold LID "{0}".'
                    ).format(lid_id, lid_base_sc_name)
                )

            input_template['SUBCATCHMENTS']['lines'].append({
                'values': lid_sc,
                'comment': '{0} LID units. (Added by OSTRICH-SWMM.)'.format(
                    lid_num_units,
                ),
            })

            # Set the LID subcatchment to the child subcatchment.
            lid['location']['subcatchment'] = lid_sc_name
        else:
            logging.warning(
                (
                    'LID type "{0}" is not directly supported by this module. '
                    'Manual adjustments to other objects, such as '
                    'subcatchments, may be necessary.'
                ).format(lid_type_type)
            )

        # Add the LID to the input.
        lid_drain_to = ''
        if 'drainTo' in lid:
            lid_drain_to_obj = lid['drainTo']
            if 'subcatchment' in lid_drain_to_obj:
                lid_drain_to = lid_drain_to_obj['subcatchment']
            elif 'node' in lid_drain_to_obj:
                lid_drain_to = lid_drain_to_obj['node']
        lid_values = [
            lid['location']['subcatchment'],
            lid['type'],
            lid['number'],
            lid['area'],
            lid['width'],
            lid['initSat'],
            lid['fromImp'],
            lid['toPerv'],
            lid.get('rptFile', ''),
            lid_drain_to,
        ]

        if 'LID_USAGE' not in input_template:
            input_template['LID_USAGE'] = {
                'lines': [],
                'comment': None,
            }

        input_template['LID_USAGE']['lines'].append({
            'values': lid_values,
            'comment': None,
        })


def perform_injection(config, validate=True):
    """Perform the injection as specified in a configuration.

    Args:
        config: The config to get injection configuration from.
        validate (boolean): Validate the configuration before attempting
            to use it. Defaults to True.

    Raises:
        ConfigException: The configuration is invalid.
        IOError: An error occurred during reading or writing.
    """
    if validate:
        validate_config(config)

    with open(config['input_template_path']) as input_template_file:
        input_template = sir.read(input_template_file)

    with open(config['input_parameters_path']) as input_parameters_file:
        input_parameters = json.load(input_parameters_file)

    inject_parameters_into_input(input_parameters, input_template)

    with open(config['input_path'], 'w') as input_file:
        siw.write(input_template, input_file)


def validate_config(config):
    """Validate a configuration for use with this functionality.

    Args:
        config (dict): The configuration to validate.

    Raises:
        ConfigException: The configuration is invalid.
    """
    cfg.validate_required_sections(config, [
        'input_path',
        'input_template_path',
        'input_parameters_path',
    ], 'inject')
    cfg.validate_file_exists(config, 'input_template_path')
    cfg.validate_file_exists(config, 'input_parameters_path')
    cfg.validate_dir_exists(config, 'input_path', path_is_file=True)
