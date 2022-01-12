import logging
import json
import re


from magnus import datastore
from magnus import utils
from magnus import defaults
from magnus import interaction
from magnus import integration
from magnus.nodes import BaseNode
from magnus.graph import Graph
from magnus import exceptions

logger = logging.getLogger(defaults.NAME)

# TODO: Implement single-node mode to run only one node of the graph.
# TODO: A decorator or a programmatic way to define pipeline


class BaseExecutor:
    """
    The skeleton of an executor class.
    Any implementation of an executor should inherit this class and over-ride accordingly.

    The logic of any dag execution is a play between three methods of this class.

    execute_graph:
        This method is responsible for traversing A graph.
        The core logic is start at the start_at the graph and traverse according to the state of the execution.
        execute_graph hands over the actual execution of the node to self.execute_from_graph

        Helper method: prepare_for_graph_execution would be called prior to calling execute_graph.
            Use it to modify settings if needed.

    execute_from_graph:
        This method is responsible for executing a node.
        But given that a node itself could be a dag in cases of parallel, map and dag, this method handles the cases.
        If the node is of type task, success, fail: we can pretty much execute it and we call self.execute_node
        If the node is of type dag, map, parallel: We call the node's execute_as_graph function which internally
        triggers execute_graph in-turn iteratively traverses the graph.


    execute_node:
        This method is where the actual execution of the work happens.
        This method is already in the compute environment of the mode.
        Use prepare_node_for_execution to adjust settings.
        The base class is given an implementation and in most cases should not be touched.

        Helper method: prepare_for_node_execution would be called prior to calling execute_node.
            Use it to modify settings if needed

    The above logic holds good when we are in interactive compute mode i.e. local, local-container, local-aws-batch

    But in 3rd party orchestration mode, we might have to render the job specifications and the roles might be different

    Please see the implementations of local, local-container, local-aws-batch to perform interactive compute.
    And aws-step-functions to see an example of what a 3rd party executor looks like.
    """
    service_name = None

    def __init__(self, config):
        # pylint: disable=R0914,R0913
        self.config = config
        # The remaining would be attached later
        self.pipeline_file = None
        self.variables_file = None
        self.run_id = None
        self.dag = None
        self.use_cached = None
        self.tag = None
        self.run_log_store = None
        self.previous_run_log = None
        self.dag_hash = None
        self.catalog_handler = None
        self.secrets_handler = None
        self.variables_file = None
        self.configuration_file = None
        self.cmd_line_arguments = {}

    def is_parallel_execution(self) -> bool:  # pylint: disable=R0201
        """
        Controls the parallelization of branches in map and parallel state.
        Defaults to False and left for the compute modes to decide.

        Returns:
            bool: True if the mode allows parallel execution of branches.
        """
        return defaults.ENABLE_PARALLEL

    def set_up_run_log(self):
        """
        Create a run log and put that in the run log store
        """
        run_log = self.run_log_store.create_run_log(self.run_id)
        run_log.tag = self.tag
        run_log.use_cached = False
        run_log.status = defaults.PROCESSING
        run_log.dag_hash = self.dag_hash

        parameters = self.cmd_line_arguments
        if self.previous_run_log:
            run_log.original_run_id = self.previous_run_log.run_id
            # Sync the previous run log catalog to this one.
            self.catalog_handler.sync_between_runs(previous_run_id=run_log.original_run_id, run_id=self.run_id)
            run_log.use_cached = True
            parameters.update(self.previous_run_log.parameters)

        run_log.parameters = parameters

        # Update run_config
        run_log.run_config = utils.get_run_config(self)

        self.run_log_store.put_run_log(run_log)

    def prepare_for_graph_execution(self):
        """
        This method would be called prior to calling execute_graph.
        Perform any steps required before doing the graph execution.

        The most common implementation is to prepare a Run Log for the run if the run uses local interactive compute.

        But in cases of actual rendering the job specs (eg: AWS step functions, K8's) we need not do anything.
        """

        integration.validate(self, self.run_log_store)
        integration.configure_for_traversal(self, self.run_log_store)

        integration.validate(self, self.catalog_handler)
        integration.configure_for_traversal(self, self.catalog_handler)

        integration.validate(self, self.secrets_handler)
        integration.configure_for_traversal(self, self.secrets_handler)

        self.set_up_run_log()

    def prepare_for_node_execution(self, node: BaseNode, map_variable: dict = None):
        """
        Perform any tweaks to the services prior to execution of the node.

        Args:
            node (Node): [description]
            map_variable (dict, optional): [description]. Defaults to None.
        """

        integration.validate(self, self.run_log_store)
        integration.configure_for_execution(self, self.run_log_store)

        integration.validate(self, self.catalog_handler)
        integration.configure_for_execution(self, self.catalog_handler)

        integration.validate(self, self.secrets_handler)
        integration.configure_for_execution(self, self.secrets_handler)

    def sync_catalog(self, node: BaseNode, step_log: datastore.StepLog, stage: str, synced_catalogs=None):
        """
        1). Identify the catalog settings by over-riding node settings with the global settings.
        2). For stage = get:
                Identify the catalog items that are being asked to get from the catalog
                And copy them to the local compute data folder
        3). For stage = put:
                Identify the catalog items that are being asked to put into the catalog
                Copy the items from local compute folder to the catalog
        4). Add the items onto the step log according to the stage

        Args:
            node (Node): The current node being processed
            step_log (datastore.StepLog): The step log corresponding to that node
            stage (str): One of get or put
        """
        if stage not in ['get', 'put']:
            msg = (
                'Catalog service only accepts get/put possible actions as part of node execution.'
                f'Sync catalog of the executor: {self.service_name} asks for {stage} which is not accepted'
            )
            raise Exception(msg)

        node_catalog_settings = node.get_catalog_settings()
        if not (node_catalog_settings and stage in node_catalog_settings):
            # Nothing to get/put from the catalog
            return None

        # Local compute data folder over rides the global one
        compute_data_folder = self.catalog_handler.compute_data_folder
        if 'compute_data_folder' in node_catalog_settings and node_catalog_settings['compute_data_folder']:
            compute_data_folder = node_catalog_settings['compute_data_folder']

        data_catalogs = []
        for name_pattern in node_catalog_settings.get(stage) or []:  #  Assumes a list
            data_catalogs = getattr(
                self.catalog_handler, stage)(
                name=name_pattern, run_id=self.run_id, compute_data_folder=compute_data_folder,
                synced_catalogs=synced_catalogs)

        if data_catalogs:
            step_log.add_data_catalogs(data_catalogs)

        return data_catalogs

    def execute_node(self, node: BaseNode, map_variable: dict = None, **kwargs):
        """
        This is the entry point when we do the actual execution of the function.
        Over-ride this function to do what the executor has to do for the actual function call
        Most frequently, this core logic should not be touched either in interactive mode or 3rd party orchestration
        mode.

        While in interactive mode, we just compute, in 3rd party interactive mode, we call this function fron the CLI

        In most cases,
            * We get the corresponding step_log of the node and the parameters.
            * We sync the catalog to GET any data sets that are in the catalog
            * We call the execute method of the node for the actual compute and retry it as many times as asked.
            * If the node succeeds, we get any of the user defined metrics provided by the user.
            * We sync the catalog to PUT any data sets that are in the catalog.

        Args:
            node (Node): The node to execute
            map_variable (str, optional): If the node is of a map state, map_variable is the value of the iterable.
                        Defaults to ''.
        """
        max_attempts = node.get_max_attempts()
        attempts = 0
        step_log = self.run_log_store.get_step_log(node.get_step_log_name(map_variable), self.run_id)

        parameters = self.run_log_store.get_parameters(run_id=self.run_id)
        interaction.store_parameter(**parameters)

        data_catalogs_get = self.sync_catalog(node, step_log, stage='get')

        mock = step_log.mock
        logger.info(f'Trying to execute node: {node.internal_name}, attempt : {attempts}, max_attempts: {max_attempts}')
        while attempts < max_attempts:
            try:
                attempt_log = node.execute(executor=self, mock=mock,
                                           map_variable=map_variable, **kwargs)
                attempt_log.attempt_numner = attempts
                step_log.attempts.append(attempt_log)
                if attempt_log.status == defaults.FAIL:
                    raise Exception()

                step_log.status = defaults.SUCCESS
                step_log.user_defined_metrics = utils.get_tracked_data()
                self.run_log_store.set_parameters(self.run_id, utils.get_user_set_parameters(remove=True))
                break
            except Exception as _e:  # pylint: disable=W0703
                attempts += 1
                logger.exception(f'Node: {node} failed with exception {_e}')
                # Remove any steps data
                utils.get_tracked_data()
                utils.get_user_set_parameters(remove=True)

            if attempts == max_attempts:
                step_log.status = defaults.FAIL
                logger.error(f'Node {node} failed, max retries of {max_attempts} reached')

        self.sync_catalog(node, step_log, stage='put', synced_catalogs=data_catalogs_get)
        self.run_log_store.add_step_log(step_log, self.run_id)

    def add_code_identities(self, node: BaseNode, step_log: object, **kwargs):
        """
        Add code identities specific to the implementation.

        The Base class has an implementation of adding git code identities.

        Args:
            step_log (object): The step log object
            node (BaseNode): The node we are adding the step log for
        """
        step_log.code_identities.append(utils.get_git_code_identity(self.run_log_store))

    def execute_from_graph(self, node: BaseNode, map_variable: dict = None, **kwargs):
        """
        This is the entry point to from the graph execution.

        While the self.execute_graph is responsible for traversing the graph, this function is responsible for
        actual execution of the node.

        If the node type is:
            * task : We can delegate to execute_node after checking the eligibility for re-run in cases of a re-run
            * success: We can delegate to execute_node
            * fail: We can delegate to execute_node

        For nodes that are internally graphs:
            * parallel: Delegate the reponsibility of execution to the node.execute_as_graph()
            * dag: Delegate the reponsibility of execution to the node.execute_as_graph()
            * map: Delegate the reponsibility of execution to the node.execute_as_graph()

        Check the implementations of local, local-container, local-aws-batch for different examples of implementation

        Args:
            node (Node): The node to execute
            map_variable (str, optional): If the node if of a map state, this corresponds to the value of iterable.
                    Defaults to ''.
        """
        step_log = self.run_log_store.create_step_log(node.name, node.get_step_log_name(map_variable))

        self.add_code_identities(node=node, step_log=step_log)

        step_log.step_type = node.node_type
        step_log.status = defaults.PROCESSING

        # Add the step log to the database as per the situation.

        # If its a terminal node, complete it now
        if node.node_type in ['success', 'fail']:
            self.run_log_store.add_step_log(step_log, self.run_id)
            self.execute_node(node, map_variable=map_variable, **kwargs)
            return

        # If previous run was successful, move on to the next step
        if not self.is_eligible_for_rerun(node, map_variable=map_variable):
            step_log.mock = True
            step_log.status = defaults.SUCCESS
            self.run_log_store.add_step_log(step_log, self.run_id)
            return

        # We call an internal function to iterate the sub graphs and execute them
        if node.node_type in ['parallel', 'dag', 'map']:
            self.run_log_store.add_step_log(step_log, self.run_id)
            node.execute_as_graph(self, map_variable=map_variable, **kwargs)
            return

        # Executor specific way to trigger a job
        self.run_log_store.add_step_log(step_log, self.run_id)
        self.trigger_job(node=node, map_variable=map_variable, **kwargs)

    def trigger_job(self, node: BaseNode, map_variable: dict = None, **kwargs):
        """
        Executor specific way of triggering jobs.

        Args:
            node (BaseNode): The node to execute
            map_variable (str, optional): If the node if of a map state, this corresponds to the value of iterable.
                    Defaults to ''.

        Raises: NotImplementedError Base class hence not implemented
        """
        raise NotImplementedError

    def get_status_and_next_node_name(self, current_node: BaseNode, dag: Graph, map_variable: dict = None):
        """
        Given the current node and the graph, returns the name of the next node to execute.

        The name is always relative the graph that the node resides in.

        If the current node succeeded, we return the next node as per the graph.
        If the current node failed, we return the on failure node of the node (if provided) or the global one.

        Args:
            current_node (BaseNode): The current node.
            dag (Graph): The dag we are traversing.
            map_variable (str): If the node belongs to a map branch.
        """

        step_log = self.run_log_store.get_step_log(current_node.get_step_log_name(map_variable), self.run_id)
        logger.info(
            f'Finished executing the node {current_node} with status {step_log.status}')

        next_node_name = current_node.get_next_node()

        if step_log.status == defaults.FAIL:
            next_node_name = dag.get_fail_node().name
            if current_node.get_on_failure_node():
                next_node_name = current_node.get_on_failure_node()

        return step_log.status, next_node_name

    def execute_graph(self, dag: Graph, map_variable: dict = None, **kwargs):
        """
        The parallelization is controlled by the nodes and not by this function.

        The mode of traversal is Depth First traversal.

        Logically the method should:
            * Start at the dag.start_at of the dag.
            * Call the self.execute_from_graph(node)
            * depending upon the status of the exeuction, either move to the success node or failure node.

        Args:
            dag (Graph): The directed acyclic graph to traverse and execute.
            map_variable (dict, optional): If the node if of a map state, this corresponds to the value of the iterable.
                    Defaults to {}.
        """
        current_node = dag.start_at
        previous_node = None
        logger.info(f'Running the execution with {current_node}')
        while True:
            working_on = dag.get_node_by_name(current_node)

            if previous_node == current_node:
                raise Exception('Potentially running in a infinite loop')

            previous_node = current_node

            logger.info(f'Creating execution log for {working_on}')
            self.execute_from_graph(working_on, map_variable=map_variable, **kwargs)

            status, next_node_name = self.get_status_and_next_node_name(
                current_node=working_on, dag=dag, map_variable=map_variable)

            if status == defaults.TRIGGERED:
                # Some nodes go into triggered state and self traverse
                logger.info(f'Triggered the job to execute the node {current_node}')
                break

            if working_on.node_type in ['success', 'fail']:
                break

            current_node = next_node_name

        run_log = self.run_log_store.get_branch_log(working_on.get_branch_log_name(map_variable), self.run_id)

        branch = 'graph'
        if working_on.internal_branch_name:
            branch = working_on.internal_branch_name

        logger.info(f'Finished execution of the {branch} with status {run_log.status}')
        logger.info(json.dumps(run_log.to_dict(), indent=4))

    def is_eligible_for_rerun(self, node, map_variable: dict = None):
        """
        In case of a re-run, this method checks to see if the previous run step status to determine if a re-run is
        necessary.
            * True: If its not a re-run.
            * True: If its a re-run and we failed in the last run or the corresponding logs do not exist.
            * False: If its a re-run and we succeeded in the last run.

        Most cases, this logic need not be touched

        Args:
            node (Node): The node to check against re-run
            map_variable (dict, optional): If the node if of a map state, this corresponds to the value of iterable..
                        Defaults to None.

        Returns:
            bool: Eligibilty for re-run. True means re-run, False means skip to the next step.
        """
        if self.previous_run_log:
            node_step_log_name = node.get_step_log_name(map_variable=map_variable)
            logger.info(f'Scanning previous run logs for node logs of: {node_step_log_name}')

            previous_node_log = None
            try:
                previous_node_log, _ = self.previous_run_log.search_step_by_internal_name(node_step_log_name)
            except exceptions.StepLogNotFoundError:
                logger.warning(f'Did not find the node {node.name} in previous run log')
                return True  # We should re-run the node.

            step_log = self.run_log_store.get_step_log(node.get_step_log_name(map_variable), self.run_id)
            logger.info(f'The original step status: {previous_node_log.status}')

            if previous_node_log.status == defaults.SUCCESS:
                logger.info(f'The step {node.name} is marked success, not executing it')
                step_log.status = defaults.SUCCESS
                step_log.message = 'Node execution successful in previous run, skipping it'
                self.run_log_store.add_step_log(step_log, self.run_id)
                return False  # We need not run the node

            # Remove previous run log to start execution from this step
            logger.info(f'The new execution should start executing graph from this node {node.name}')
            self.previous_run_log = None
        return True

    def send_return_code(self, stage='traversal'):
        """
        Convenience function used by pipeline to send return code to the caller of the cli

        Raises:
            Exception: If the pipeline execution failed
        """
        run_id = self.run_id

        run_log = self.run_log_store.get_run_log_by_id(run_id=run_id, full=False)
        if run_log.status == defaults.FAIL:
            raise Exception('Pipeline execution failed')


class LocalExecutor(BaseExecutor):
    """
    In the mode of local execution, we run everything on the local computer.

    We do not parallelize any of the steps but the mode of traversal would be a Depth First traversal.

    This has some serious implications on the amount of time it would take to complete the run.
    Also ensure that the local compute is good enough for the compute to happen of all the steps.

    Example config:
    mode:
      type: local
      config:
        enable_parallel: string True or False to enable parallel.

    """
    service_name = 'local'

    def is_parallel_execution(self):
        if self.config and 'enable_parallel' in self.config:
            return self.config.get('enable_parallel').lower() == 'true'

        return defaults.ENABLE_PARALLEL

    def trigger_job(self, node: BaseNode, map_variable: dict = None, **kwargs):
        """
        In this mode of execution, we prepare for the node execution and execute the node

        Args:
            node (BaseNode): [description]
            map_variable (str, optional): [description]. Defaults to ''.
        """
        self.prepare_for_node_execution(node, map_variable=map_variable)
        self.execute_node(node=node, map_variable=map_variable, **kwargs)


class LocalContainerExecutor(BaseExecutor):
    """
    In the mode of local-container, we execute all the commands in a container.

    Ensure that the local compute has enough resources to finish all your jobs.

    The image of the run, could either be provided as default in the configuraiton of the mode
    i.e.:
    mode:
      type: 'local-container'
      config:
        docker_image: the image you want the code to run in.

    or default image could be over-ridden for a single node by providing a docker_image in the step config.
    i.e:
    dag:
      steps:
        step:
          mode_config:
            docker_image: The image that you want that single step to run in.
    This image would only be used for that step only.

    This mode does not build the docker image with the latest code for you, it is still left for the user to build
    and ensure that the docker image provided is the correct one.

    Example config:
    mode:
      type: local-container
      config:
        docker_image: The default docker image to use if the node does not provide one.
    """
    service_name = 'local-container'

    def __init__(self, config):
        # pylint: disable=R0914,R0913
        super().__init__(config=config)
        self.container_log_location = '/tmp/run_logs/'
        self.container_catalog_location = '/tmp/catalog/'
        self.container_secrets_location = '/tmp/dotenv'
        self.volumes = {}

    @property
    def docker_image(self):
        try:
            return self.config.get('docker_image', None)
        except AttributeError:
            msg = (
                f'Local container mode typically is used with a config containing the docker image.'
            )
            logger.warning(msg)
            return None

    def is_parallel_execution(self):
        if self.config and 'enable_parallel' in self.config:
            return self.config.get('enable_parallel').lower() == 'true'

        return defaults.ENABLE_PARALLEL

    def add_code_identities(self, node: BaseNode, step_log: object, **kwargs):
        """
        Call the Base class to add the git code identity and add docker identity

        Args:
            node (BaseNode): The node we are adding the code identity
            step_log (Object): The step log corresponding to the node
        """

        super().add_code_identities(node, step_log)
        mode_config = {**self.config, **node.get_mode_config()}

        docker_image = mode_config.get('docker_image', None)
        if docker_image:
            code_id = self.run_log_store.create_code_identity()

            code_id.code_identifier = utils.get_local_docker_image_id(docker_image)
            code_id.code_identifier_type = 'docker'
            code_id.code_identifer_dependable = True
            code_id.code_identifier_url = 'local docker host'
            step_log.code_identities.append(code_id)

    def trigger_job(self, node: BaseNode, map_variable: dict = None, **kwargs):
        """
        In local container mode, we just spin the container to execute magnus execute_single_node

        Args:
            node (BaseNode): The node we are currently executing
            map_variable (str, optional): If the node is part of the map branch. Defaults to ''.
        """
        self._spin_container(node, map_variable=map_variable, **kwargs)

        # Check for the status of the node log and anything apart from Success is FAIL
        # This typically happens if something is wrong with magnus or settings.
        step_log = self.run_log_store.get_step_log(node.get_step_log_name(map_variable), self.run_id)
        if step_log.status != defaults.SUCCESS:
            msg = (
                'Node execution inside the container failed. Please check the logs.\n'
                'Note: If you do not see any docker issue from your side and the code works properly on local mode '
                'please raise a bug report.'
            )
            logger.warning(msg)
            step_log.status = defaults.FAIL
            self.run_log_store.add_step_log(step_log, self.run_id)

    def _spin_container(self, node, map_variable: dict = None, **kwargs):  # pylint: disable=unused-argument
        """
        During the flow run, we have to spin up a container with the docker image mentioned
        and the right log locations
        """
        # Conditional import
        import docker  # pylint: disable=C0415

        try:
            client = docker.from_env()
        except Exception as ex:
            logger.exception('Could not get access to docker')
            raise Exception('Could not get the docker socket file, do you have docker installed?') from ex

        try:
            action = utils.get_node_execution_command(self, node, map_variable=map_variable)
            logger.info(f'Running the command {action}')
            mode_config = {**self.config, **node.get_mode_config()}  #  Overrides global config with local
            docker_image = mode_config.get('docker_image', None)
            if not docker_image:
                raise Exception(
                    f'Please provide a docker_image using mode_config of the step {node.name} or at global mode')

            # TODO: Should consider using getpass.getuser() when running the docker container? Volume permissions
            container = client.containers.create(image=docker_image,
                                                 command=action,
                                                 auto_remove=True,
                                                 volumes=self.volumes,
                                                 network_mode='host')
            container.start()
            stream = container.logs(stream=True, follow=True)
            while True:
                try:
                    output = next(stream).decode("utf-8")
                    output = output.strip('\r\n')
                    logger.info(output)
                except StopIteration:
                    logger.info('Docker Run completed')
                    break

        except Exception as _e:
            logger.exception('Problems with spinning up the container')
            raise _e


class DemoRenderer(BaseExecutor):
    """
    This renderer is an example of how you can render required job specifications as per your orchestration tool.

    BaseExecutor implements many of the functionalities that are common and can be safe defaults.
    In this renderer example: We just render a bash script that sequentially calls the steps.
    We do not handle composite steps in this mode.

    Example config:
    mode:
      type: demo-renderer
    """
    service_name = 'demo-renderer'

    def is_parallel_execution(self) -> bool:  # pylint: disable=R0201
        """
        Controls the parallelization of branches in map and parallel state.

        Most orchestrators control the parralization of the branches outside of magnus control.
        i.e, You would render the parallel job job specification in the language of the orchestrator.

        NOTE: Most often, this should be false for modes that rely upon other orchestration tools.

        Returns:
            bool: True if the mode allows parallel execution of branches.
        """
        return defaults.ENABLE_PARALLEL

    def prepare_for_graph_execution(self):
        """
        This method would be called prior to calling execute_graph.
        Perform any steps required before doing the graph execution.

        NOTE: For most rendering jobs, we need not do anything but customize according to your needs.
            You might want to over-ride this method to do nothing.
        """
        pass

    def prepare_for_node_execution(self, node: BaseNode, map_variable: str = ''):
        """
        This method would be called prior to the node execution in the environment of the compute.

        Use this method to set up the required things for the compute.
        The most common examples might be to ensure that the appropriate run log is in place.

        NOTE: You might need to over-ride this method.
        For interactive modes, prepare_for_graph_execution takes care of a lot of set up. For orchestrated modes,
        the same work has to be done by prepare_for_node_execution.
        """
        super().prepare_for_node_execution(node=node, map_variable=map_variable)

        # Set up the run log or create it if not done previously
        try:
            # Try to get it if previous steps have created it
            self.run_log_store.get_run_log_by_id(self.run_id)
        except exceptions.RunLogNotFoundError:
            # Create one if they are not created
            self.set_up_run_log()

        # Need to set up the step log for the first node as the entry point is different
        step_log = self.run_log_store.create_step_log(node.name, node.get_step_log_name(map_variable))

        self.add_code_identities(node=node, step_log=step_log)

        step_log.step_type = node.node_type
        step_log.status = defaults.PROCESSING
        self.run_log_store.add_step_log(step_log, self.run_id)

    def sync_catalog(self, node: BaseNode, step_log: datastore.StepLog, stage: str, synced_catalogs=None):
        """
        Syncs the catalog for both get and put stages.

        The default executors implementation just delegates the functionlity to catalog handlers get or pur methods.

        NOTE: Most often, you should not be over-riding this.
        Custom funtionality can also be obtained by working on catalog handler implementation.
        """
        super().sync_catalog(node, step_log, stage)

    def execute_node(self, node: BaseNode, map_variable: str = '', **kwargs):
        """
        This method does the actual execution of a task, as-is, success or fail node.

        NOTE: Most often, you should not be over-riding this.
        """
        super().execute_node(node, map_variable=map_variable, **kwargs)

        step_log = self.run_log_store.get_step_log(node.get_step_log_name(map_variable), self.run_id)
        if step_log.status == defaults.FAIL:
            raise Exception(f'Step {node.name} failed')

    def add_code_identities(self, node: BaseNode, step_log: object):
        """
        Add code identities specific to the implementation.

        The Base class has an implementation of adding git code identities.

        NOTE: Most often, you just call the super to add the git code identity and add
        any other code identities that you want part of your implementation
        """
        super().add_code_identities(node, step_log)

    def execute_from_graph(self, node: BaseNode, map_variable: str = '', **kwargs):
        """
        This method delegates the execution of composite nodes to the appropriate methods.

        This method calls add_code_identities and trigger_job as part of its implemetation.
        use them to add the funcionality specific to the compute environment.

        NOTE: Most often, you should not be changing this implementation.
        """
        super().execute_from_graph(node=node, map_variable=map_variable, **kwargs)

    def trigger_job(self, node: BaseNode, map_variable: str = '', **kwargs):
        """
        Executor specific way of triggering jobs.

        This method has to be changed to do what exactly you want as part of your computational engine

        If your compute is not local, use utils.get_node_execution_command(self, node, map_variable=map_variable)
        to get the command to run a single node.

        If the compute is local to the environment, calls prepare_for_node_execution and call execute_node
        NOTE: This method should always be implemented.
        """
        self.prepare_for_node_execution(node, map_variable=map_variable)
        self.execute_node(node=node, map_variable=map_variable, **kwargs)

    def send_return_code(self, stage='traversal'):
        """
        Convenience function used by pipeline to send return code to the caller of the cli

        Raises:
            Exception: If the pipeline execution failed
        """
        if stage != 'traversal':  # traversal does no actual execution, so return code is pointless
            run_id = self.run_id

            run_log = self.run_log_store.get_run_log_by_id(run_id=run_id, full=False)
            if run_log.status == defaults.FAIL:
                raise Exception('Pipeline execution failed')

    def execute_graph(self, dag: Graph, map_variable: str = '', **kwargs):
        """
        Iterate through the graph and frame the bash script.

        For more complex ouputs, dataclasses might be a better option.

        NOTE: This method should be over-written to write the exact specification to the compute engine.

        """
        current_node = dag.start_at
        previous_node = None
        logger.info(f'Rendering job started at {current_node}')
        bash_script_lines = []

        while True:
            working_on = dag.get_node_by_name(current_node)

            if working_on.node_type in ['parallel', 'dag', 'map']:
                raise NotImplementedError('In this demo version, composite nodes are not implemented')

            if previous_node == current_node:
                raise Exception('Potentially running in a infinite loop')

            previous_node = current_node

            logger.info(f'Creating execution log for {working_on}')

            execute_node_command = utils.get_node_execution_command(self, working_on, over_write_run_id='$1')
            current_job_id = re.sub('[^A-Za-z0-9]+', '', f'{current_node}_job_id')
            fail_node_command = utils.get_node_execution_command(self, dag.get_fail_node(), over_write_run_id='$1')

            if working_on.node_type not in ['success', 'fail']:
                bash_script_lines.append(f'{execute_node_command}\n')

                bash_script_lines.append('exit_code=$?\necho $exit_code\n')
                # Write failure node
                bash_script_lines.append(
                    (
                        'if [ $exit_code -ne 0 ];\nthen\n'
                        f'\t $({fail_node_command})\n'
                        '\texit 1\n'
                        'fi\n'
                    )
                )

            if working_on.node_type == 'success':
                bash_script_lines.append(f'{execute_node_command}')
            if working_on.node_type in ['success', 'fail']:
                break

            current_node = working_on.get_next_node()

        with open('demo-bash.sh', 'w', encoding='utf-8') as fw:
            fw.writelines(bash_script_lines)

        msg = (
            'demo-bash.sh for running the pipeline is written. To execute it \n'
            '1). Activate the environment:\n'
            '\t for example poetry shell or pipenv shell etc\n'
            '2). Make the shell script executable.\n'
            '\t chmod 755 demo-bash.sh\n'
            '3). Run the script by: source demo-bash.sh <run_id>\n'
            '\t The first argument to the script is the run id you want for the run.'
        )
        logger.info(msg)
