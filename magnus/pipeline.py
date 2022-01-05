import logging
import json


from magnus import utils
from magnus import graph
from magnus import nodes
from magnus import defaults

logger = logging.getLogger(defaults.NAME)

# Set this global executor to the fitted executor for access later
global_executor = None  # pylint: disable=invalid-name
magnus_defaults = {}  # pylint: disable=invalid-name


def load_user_extensions():
    """
    User can provide extensions as part of their code base, magnus-config.yaml provides the place to put them.
    Look for them and load the extensions if provided.
    """
    user_configs = {}
    if utils.does_file_exist(defaults.USER_CONFIG_FILE):
        user_configs = utils.load_yaml(defaults.USER_CONFIG_FILE)

    if not user_configs:
        return

    extensions = user_configs.get('extensions', [])
    for extension in extensions:
        logger.info('Loading User extension: %s', extension)
        __import__(extension)

    user_defaults = user_configs.get('defaults', {})
    if user_defaults:
        global magnus_defaults  # pylint: disable=W0603,invalid-name,
        magnus_defaults = user_defaults


def prepare_configurations(
        variables_file: str,
        configuration_file: str,
        pipeline_file: str,
        run_id: str,
        tag: str,
        use_cached: bool):
    # pylint: disable=R0914
    """
    Replace the placeholders in the dag/config against the variables file.

    Attach the secrets_handler, run_log_store, catalog_handler to the executor and return it.

    Args:
        variables_file (str): The variables file, if used or None
        pipeline_file (str): The config/dag file
        run_id (str): The run id of the run.
        tag (str): If a tag is provided at the run time
        use_cached (bool): Is true for a re-run, otherwise false

    Returns:
        executor.BaseExecutor : A prepared executor as per the dag/config
    """
    global magnus_defaults

    pipeline_config = utils.load_yaml(pipeline_file)

    variables = {}
    if variables_file:
        variables = utils.load_yaml(variables_file)

    configuration = {}
    if configuration_file:
        configuration = utils.load_yaml(configuration_file)

    # apply variables
    pipeline_config = utils.apply_variables(pipeline_config, variables=variables)

    logger.info('The input pipeline:')
    logger.info(json.dumps(pipeline_config, indent=4))

    # Create the graph
    dag_config = pipeline_config['dag']
    dag_hash = utils.get_dag_hash(dag_config)
    # TODO: Dag nodes should not self refer themselves
    dag = graph.create_graph(dag_config)

    # Run log settings, configuration over-rides everything
    run_log_config = configuration.get('run_log_store', {})
    if not run_log_config:
        default_run_log_config = magnus_defaults.get('run_log_store', defaults.DEFAULT_RUN_LOG_STORE)
        run_log_config = pipeline_config.get('run_log', {}) or default_run_log_config
    run_log_store = utils.get_provider_by_name_and_type('run_log_store', run_log_config)

    # Catalog handler settings, configuration over-rides everything
    catalog_config = configuration.get('catalog', {})
    if not catalog_config:
        default_catalog_config = magnus_defaults.get('catalog', defaults.DEFAULT_CATALOG)
        catalog_config = pipeline_config.get('catalog', {}) or default_catalog_config
    catalog_handler = utils.get_provider_by_name_and_type('catalog', catalog_config)

    # Secret handler settings, configuration over-rides everything
    secrets_config = configuration.get('secrets', {})
    if not secrets_config:
        default_secrets_config = magnus_defaults.get('secrets', defaults.DEFAULT_SECRETS)
        secrets_config = pipeline_config.get('secrets', {}) or default_secrets_config
    secrets_handler = utils.get_provider_by_name_and_type('secrets', secrets_config)

    # Mode configurations, configuration over rides everything
    mode_config = configuration.get('mode', {})
    if not mode_config:
        default_mode_config = magnus_defaults.get('executor', defaults.DEFAULT_EXECUTOR)
        mode_config = pipeline_config.get('mode', {}) or default_mode_config
    mode_executor = utils.get_provider_by_name_and_type('executor', mode_config)

    mode_executor.pipeline_file = pipeline_file
    mode_executor.dag = dag
    mode_executor.run_id = run_id
    mode_executor.tag = tag
    mode_executor.use_cached = use_cached

    # Set a global executor for inter-module access later
    global global_executor  # pylint: disable=W0603,invalid-name,
    global_executor = mode_executor

    mode_executor.run_log_store = run_log_store
    mode_executor.catalog_handler = catalog_handler
    mode_executor.dag_hash = dag_hash
    mode_executor.secrets_handler = secrets_handler
    mode_executor.variables_file = variables_file
    mode_executor.configuration_file = configuration_file

    return mode_executor


def send_return_code(mode_executor):
    """
    If the run log status is fail, let the caller know that by raising an exception

    Args:
        mode_executor (object): The implemented Executor class

    Raises:
        Exception: If the execution status of the pipeline is FAIL
    """
    run_id = mode_executor.run_id

    run_log = mode_executor.run_log_store.get_run_log_by_id(run_id=run_id, full=False)
    if run_log.status == defaults.FAIL:
        raise Exception('Pipeline execution failed')


def execute(
        variables_file: str,
        configuration_file: str,
        pipeline_file: str,
        tag: str = None,
        run_id: str = None,
        use_cached: bool = False,
        use_cached_force: bool = False,
        **kwargs):
    # pylint: disable=R0914,R0913
    """
    The entry point to magnus execution. This method would prepare the configurations and delegates traversal to the
    executor

    Args:
        variables_file (str): The variables file, if used or None
        pipeline_file (str): The config/dag file
        run_id (str): The run id of the run.
        tag (str): If a tag is provided at the run time
        use_cached (bool): Is true for a re-run, otherwise false
        use_cached_force (bool, optional): If you want to force a re-run even if the dag was found to be
        changed. Defaults to False.

    Raises:
        Exception: If the dag hash has found not to be same in case of re-runs and use-cached was not used.
    """
    # Re run settings
    re_run_id = run_id  # Used only if we asked for a cached run
    run_id = utils.generate_run_id(run_id=run_id)

    mode_executor = prepare_configurations(variables_file=variables_file,
                                           configuration_file=configuration_file,
                                           pipeline_file=pipeline_file,
                                           run_id=run_id,
                                           tag=tag,
                                           use_cached=use_cached)

    mode_executor.cmd_line_arguments = kwargs
    previous_run_log = None
    # TODO: Need more design thought on this
    if use_cached or use_cached_force:
        previous_run_log = mode_executor.run_log_store.get_run_log_by_id(run_id=re_run_id, full=True)
        if previous_run_log.dag_hash != mode_executor.dag_hash:
            logger.warning('The previous dag does not match to the current one!')
            if not use_cached_force:
                message = 'Not using the cached run as the dag hash did not match, \
                            use --use-cached-force if you want to force'
                logger.error(message)
                raise Exception(message)
        mode_executor.previous_run_log = previous_run_log
        logger.info('Found a previous run log and using it as cache')

    # Preapre for graph exeuction
    mode_executor.prepare_for_graph_execution()

    logger.info('Executing the graph')
    mode_executor.execute_graph(dag=mode_executor.dag)

    send_return_code(mode_executor)


def execute_single_node(
        variables_file: str,
        configuration_file: str,
        pipeline_file: str,
        step_name: str,
        map_variable: str,
        run_id: str,
        tag: str = None,
        **kwargs):
    # pylint: disable=R0914,R0913
    """
    The entry point into executing a single node of magnus. Orchestration modes should extensivesly use this
    entry point.

    Args:
        variables_file (str): The variables file, if used or None
        step_name : The name of the step to execute in dot path convention
        pipeline_file (str): The config/dag file
        run_id (str): The run id of the run.
        tag (str): If a tag is provided at the run time

    """
    mode_executor = prepare_configurations(variables_file=variables_file,
                                           configuration_file=configuration_file,
                                           pipeline_file=pipeline_file,
                                           run_id=run_id,
                                           tag=tag,
                                           use_cached=False)
    mode_executor.cmd_line_arguments = kwargs
    step_internal_name = nodes.BaseNode.get_internal_name_from_command_name(step_name)

    map_variable = utils.json_to_ordered_dict(map_variable)

    node_to_execute, _ = graph.search_node_by_internal_name(mode_executor.dag, step_internal_name)

    mode_executor.prepare_for_node_execution(node_to_execute, map_variable=map_variable)

    logger.info('Executing the single node of : %s', node_to_execute)
    mode_executor.execute_node(node=node_to_execute, map_variable=map_variable)

    send_return_code(mode_executor)


# TODO: The branches have to be command friendly too
def execute_single_brach(
        variables_file: str,
        configuration_file: str,
        pipeline_file: str,
        branch_name: str,
        map_variable: str,
        run_id: str,
        tag: str = None,
        **kwargs):
    # pylint: disable=R0914,R0913
    """
    The entry point into executing a branch of the graph. Interactive modes in parallel runs use this to execute
    branches in parallel

    Args:
        variables_file (str): The variables file, if used or None
        branch_name : The name of the branch to execute, in dot.path.convention
        pipeline_file (str): The config/dag file
        run_id (str): The run id of the run.
        tag (str): If a tag is provided at the run time
    """
    mode_executor = prepare_configurations(variables_file=variables_file,
                                           configuration_file=configuration_file,
                                           pipeline_file=pipeline_file,
                                           run_id=run_id,
                                           tag=tag,
                                           use_cached=False)
    mode_executor.cmd_line_arguments = kwargs

    map_variable = utils.json_to_ordered_dict(map_variable)

    branch_to_execute = graph.search_branch_by_internal_name(mode_executor.dag, branch_name)

    logger.info('Executing the single branch of %s', branch_to_execute)
    mode_executor.execute_graph(dag=branch_to_execute, map_variable=map_variable)

    send_return_code(mode_executor)


load_user_extensions()
