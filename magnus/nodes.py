import logging
from datetime import datetime
import importlib
import subprocess
import os
import sys
import json
import multiprocessing
from collections import OrderedDict
from typing import List

from pkg_resources import resource_filename

import magnus
from magnus import utils
from magnus.graph import create_graph
from magnus import defaults

logger = logging.getLogger(defaults.NAME)


class BaseExecutionType:  # pylint: disable=too-few-public-methods
    """
    A base execution class which actually does the execution of command defined by the user

    Raises:
        NotImplementedError: Base class, hence not implemeted
    """
    execution_type = None

    def execute_command(self, command: str, map_variable: str = ''):
        """
        The function to execute the command mentioned in command.

        The parameters are filtered based on the 'command' signature

        And map_variable is sent in as an argument into the function.

        Args:
            command (str): The actual command to run
            parameters (dict, optional): The parameters available across the system. Defaults to None.
            map_variable (str, optional): If the command is part of map node, the value of map. Defaults to ''.

        Raises:
            NotImplementedError: [description]
        """
        raise NotImplementedError


def get_command_class(command_type: str) -> BaseExecutionType:
    """
    Given a command exeuction type, return the class that implements the execution type.

    Args:
        command_type (str): The command execution type you want, shell, python are examples

    Raises:
        Exception: If the command execution type is not implemented

    Returns:
        BaseExecutionType: The implemention of the command execution type
    """
    for sub_class in BaseExecutionType.__subclasses__():
        if command_type == sub_class.execution_type:
            return sub_class()
    raise Exception(f'Command type {command_type} not found')


class PythonExecutionType(BaseExecutionType):  # pylint: disable=too-few-public-methods
    """
    The execution class for python command
    """
    execution_type = 'python'

    def execute_command(self, command, map_variable: dict = None,):
        module, func = utils.get_module_and_func_names(command)
        sys.path.insert(0, os.getcwd())  # Need to add the current directory to path
        imported_module = importlib.import_module(module)
        f = getattr(imported_module, func)

        parameters = utils.get_user_set_parameters(remove=False)
        filtered_parameters = utils.filter_arguments_for_func(f, parameters, map_variable)

        logger.info(f'Calling {func} from {module} with {filtered_parameters}')
        user_set_parameters = f(**filtered_parameters)

        if user_set_parameters:
            if not type(user_set_parameters) == dict:
                raise Exception('Only dictionaries are supported as return values')
            for key, value in user_set_parameters.items():
                logger.info(f'Setting User defined parameter {key} with value: {value}')
                os.environ[defaults.PARAMETER_PREFIX + key] = json.dumps(value)


class ShellExecutionType(BaseExecutionType):
    """
    The execution class for shell based commands
    """
    execution_type = 'shell'

    def execute_command(self, command, map_variable: dict = None):
        # TODO can we do this without shell=True. Hate that but could not find a way out
        # This is horribly weird, focussing only on python ways of doing for now
        # It might be that we have to write a bash/windows script that does things for us
        # TODO: send in the map variable as environment variable
        return subprocess.run(command.split(), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class BaseNode:
    """
    Base class with common functionality provided for a Node of a graph.

    A node of a graph could be a
        * single execution node as Task, Succcess, Fail.
        * Could be graph in itself as Parallel, dag and map.
        * could be a convenience rendering function like as-is.

    The name is relative to the DAG.
    The internal name of the node, is absolute name in dot path convention.
    The internal name of a node, should always be odd when split against dot.

    The internal branch name, only applies for branched nodes, is the branch it belongs to.
    The internal branch name should always be even when split against dot.
    """

    node_type = None

    def __init__(self, name, internal_name, config, execution_type, internal_branch_name=None):
        # pylint: disable=R0914,R0913
        self.name = name
        self.internal_name = internal_name  #  Dot notation naming of the steps
        self.config = config
        self.internal_branch_name = internal_branch_name  # parallel, map, dag only have internal names
        self.execution_type = execution_type
        self.branches = None

    def command_friendly_name(self, replace_with=defaults.COMMAND_FRIENDLY_CHARACTER) -> str:
        """
        Replace spaces with special character for spaces

        Returns:
            str: The command friendly name of the node
        """
        return self.internal_name.replace(' ', replace_with)

    @classmethod
    def get_internal_name_from_command_name(cls, command_name: str) -> str:
        """
        Replace Magnus specific whitespace character (%) with whitespace

        Args:
            command_name (str): The command friendly node name

        Returns:
            str: The internal name of the step
        """
        return command_name.replace(defaults.COMMAND_FRIENDLY_CHARACTER, ' ')

    def get_step_log_name(self, map_variable: dict = None) -> str:
        """
        For every step in the dag, there is a corresponding step log name.
        This method returns the step log name in dot path convention.

        We should be able to return the corresponding step log from the run log store from this name.

        All node types except a map state has a "static" defined step_log names and are equivalent to internal_name.
        For nodes belonging to map state, the internal name has a placeholder that is replaced at runtime.

        Args:
            map_variable (str): If the node is of type map, this is the iteration variable
            For example: if we are iterating on [a, b, c], this would be 'a', 'b' or 'c'

        Returns:
            str: The dot path name of the step log name
        """
        return self.resolve_map_placeholders(self.internal_name, map_variable=map_variable)

    def get_branch_log_name(self, map_variable: dict = None) -> str:
        """
        For nodes that are internally branches, this method returns the branch log name.
        The branch log name is in dot path convention.

        We should be able to retrieve the corresponding branch log from run log store from this name.

        For nodes that are not map, the internal branch name is equivalent to the branch name.
        For map nodes, the internal branch name has a placeholder that is replaced at runtime.

        Args:
            map_variable ([type]): If the node type is of type map, this is the iteration variable.
            For example: if we are iterating on [a, b, c], this would be 'a', 'b' or 'c'

        Returns:
            str: The dot path name of the branch log
        """
        return self.resolve_map_placeholders(self.internal_branch_name, map_variable=map_variable)

    def __str__(self):  # pragma: no cover
        return f'Node of type {self.node_type} and name {self.internal_name}'

    def get_on_failure_node(self) -> str:
        """
        If the node defines a on_failure node in the config, return this or None.

        The naming is relative to the dag, the caller is supposed to resolve it to the correct graph

        Returns:
            str: The on_failure node defined by the dag or None
        """
        if 'on_failure' in self.config:
            return self.config['on_failure']
        return None

    def get_catalog_settings(self) -> dict:
        """
        If the node defines a catalog settings, return it or None

        Returns:
            dict: catalog settings defined as per the node or None
        """
        if 'catalog' in self.config:
            return self.config['catalog']
        return None

    def get_branch_by_name(self, branch_name: str):
        """
        Retrieve a branch by name.

        The name is expected to follow a dot path convention.

        This method will raise an exception if the node does not have any brances.
        i.e: task, success, fail and as-is would raise an exception

        Args:
            branch_name (str): [description]

        Raises:
            Exception: [description]
        """
        raise Exception(f'Node of type {self.node_type} does not have any branches')

    def is_terminal_node(self):
        """Returns whether a node has a next node

        Returns:
            bool: True or False of whether there is next node.
        """
        if 'next' in self.config:
            return False
        return True

    def get_neighbours(self):
        """Gets the connecting neighbour nodes, either the "next" node or "on_failure" node.

        Returns:
            list: List of connected neighbours for a given node. Empty if terminal node.
        """
        neighbours = []
        next_node = self.get_next_node()
        if next_node:
            neighbours += [next_node]

        fail_node = self.get_on_failure_node()
        if fail_node:
            neighbours += [fail_node]

        return neighbours

    def get_next_node(self) -> str:
        """
        Return the next node as defined by the config.

        Returns:
            str: The node name, relative to the dag, as defined by the config
        """
        if not self.is_terminal_node():
            return self.config['next']
        return None

    def get_mode_config(self) -> dict:
        """
        Return the mode config of the node, if defined, or empty dict

        Returns:
            dict: The mode config, if defined or an empty dict
        """
        return self.config.get('mode_config', {}) or {}

    def get_max_attempts(self) -> int:
        """
        The number of max attempts as defined by the config or 1.

        Returns:
            int: The number of maximum retries as defined by the config or 1.
        """
        if 'retry' in self.config:
            retry = int(self.config['retry']) or 1
            return retry
        return 1

    def execute(self, executor, mock=False, map_variable: dict = None, **kwargs):
        """
        The actual function that does the execution of the command in the config.

        Should only be implemented for task and as-is and never for
        composite nodes.

        Args:
            executor (magnus.executor.BaseExecutor): The executor mode class
            mock (bool, optional): Dont run, just pretend. Defaults to False.
            map_variable (str, optional): The value of the map iteration variable, if part of a map node.
                Defaults to ''.

        Raises:
            NotImplementedError: Base class, hence not implemented.
        """
        raise NotImplementedError

    def execute_as_graph(self, executor, map_variable: dict = None, **kwargs):
        """
        This function would be called to set up the execution of the individual
        branches of a composite node.

        Function should only be implemented for composite nodes like dag, map, parallel.

        Args:
            executor (magnus.executor.BaseExecutor): The executor mode.

        Raises:
            NotImplementedError: Base class, hence not implemented.
        """
        raise NotImplementedError

    @classmethod
    def resolve_map_placeholders(cls, name: str, map_variable: dict = None) -> str:
        """
        Replace map_placeholders with map_variables

        Args:
            name (str): The name to resolve
            map_variable (dict): The dictionary of map variables

        Returns:
            [str]: The resolved name
        """
        if not map_variable:
            return name

        for _, value in map_variable.items():
            name = name.replace(defaults.MAP_PLACEHOLDER, value, 1)

        return name


def validate_node(node: BaseNode) -> List[str]:
    """
    Given a node defintion, run it against a specification of fields that are
    required and should not be present.

    Args:
        node (BaseNode): The node object created before validation

    Raises:
        Exception: If the node type is not part of the specs

    Returns:
        List[str]: The list of error messages, if found
    """
    specs = utils.load_yaml(resource_filename(__name__, defaults.NODE_SPEC_FILE))
    if node.node_type not in specs:
        raise Exception('Undefined node type, please update specs')

    node_spec = specs[node.node_type]
    messages = []
    if '.' in node.name:
        messages.append('Node names cannot have . in them')
    if '%' in node.name:
        messages.append("Node names cannot have '%' in them")
    if 'required' in node_spec:
        for req in node_spec['required']:
            if not req in node.config:
                messages.append(f'{node.name} should have {req} field')
                continue

    if 'error_on' in node_spec:
        for err in node_spec['error_on']:
            if err in node.config:
                messages.append(f'{node.name} should not have {err} field')
    return messages


def get_node_class(node_type: str) -> BaseNode:
    """
    Given a node_type of a node, return the appropriate BaseNode implementation.

    Args:
        node_type (str): The type of node asked by the config.

    Raises:
        Exception: If the node type is not found in implementations

    Returns:
        BaseNode: The node class of type node_type
    """
    for sub_class in BaseNode.__subclasses__():
        if node_type == sub_class.node_type:
            return sub_class
    raise Exception(f'Node type {node_type} not found')


class TaskNode(BaseNode):
    """
    A node of type Task.

    This node does the actual function execution of the graph in all cases.
    """
    node_type = 'task'

    def execute(self, executor, mock=False, map_variable: dict = None, **kwargs):
        # Here is where the juice is
        attempt_log = executor.run_log_store.create_attempt_log()
        try:
            attempt_log.start_time = str(datetime.now())
            attempt_log.status = defaults.SUCCESS
            command = self.config['command']
            if not mock:
                # Do not run if we are mocking the execution, could be useful for caching and dry runs
                self.execution_type.execute_command(command, map_variable=map_variable)
        except Exception as _e:  # pylint: disable=W0703
            logger.exception('Task failed')
            attempt_log.status = defaults.FAIL
            attempt_log.message = str(_e)
        finally:
            attempt_log.end_time = str(datetime.now())
            attempt_log.duration = utils.get_duration_between_datetime_strings(
                attempt_log.start_time, attempt_log.end_time)
        return attempt_log

    def execute_as_graph(self, executor, map_variable: dict = None, **kwargs):
        """
        Should not be implemented for a single node.

        Args:
            executor ([type]): [description]

        Raises:
            Exception: Not a composite node, always raises an exception
        """
        raise Exception('Node is not a composite node, invalid traversal rule')


class FailNode(BaseNode):
    """
    A leaf node of the graph that represents a failure node
    """
    node_type = 'fail'

    def execute(self, executor, mock=False, map_variable: dict = None, **kwargs):
        attempt_log = executor.run_log_store.create_attempt_log()
        try:
            attempt_log.start_time = str(datetime.now())
            attempt_log.status = defaults.SUCCESS
            #  could be a branch or run log
            run_or_branch_log = executor.run_log_store.get_branch_log(
                self.get_branch_log_name(map_variable), executor.run_id)
            run_or_branch_log.status = defaults.FAIL
            executor.run_log_store.add_branch_log(run_or_branch_log, executor.run_id)
        except:  # pylint: disable=W0703
            logger.exception('Fail node execution failed')
        finally:
            attempt_log.status = defaults.SUCCESS  # This is a dummy node, so we ignore errors and mark SUCCESS
            attempt_log.end_time = str(datetime.now())
            attempt_log.duration = utils.get_duration_between_datetime_strings(
                attempt_log.start_time, attempt_log.end_time)
        return attempt_log

    def execute_as_graph(self, executor, map_variable: dict = None, **kwargs):
        """
        Should not be implemented for a single node.

        Args:
            executor ([type]): [description]

        Raises:
            Exception: Not a composite node, always raises an exception
        """
        raise Exception('Node is not a composite node, invalid traversal rule')


class SuccessNode(BaseNode):
    """
    A leaf node of the graph that represents a success node
    """
    node_type = 'success'

    def execute(self, executor, mock=False, map_variable: dict = None, **kwargs):
        attempt_log = executor.run_log_store.create_attempt_log()
        try:
            attempt_log.start_time = str(datetime.now())
            attempt_log.status = defaults.SUCCESS
            #  could be a branch or run log
            run_or_branch_log = executor.run_log_store.get_branch_log(
                self.get_branch_log_name(map_variable), executor.run_id)
            run_or_branch_log.status = defaults.SUCCESS
            executor.run_log_store.add_branch_log(run_or_branch_log, executor.run_id)
        except:  # pylint: disable=W0703
            logger.exception('Success node execution failed')
        finally:
            attempt_log.status = defaults.SUCCESS  # This is a dummy node and we make sure we mark it as success
            attempt_log.end_time = str(datetime.now())
            attempt_log.duration = utils.get_duration_between_datetime_strings(
                attempt_log.start_time, attempt_log.end_time)
        return attempt_log

    def execute_as_graph(self, executor, map_variable: dict = None, **kwargs):
        """
        Should not be implemented for a single node.

        Args:
            executor ([type]): [description]

        Raises:
            Exception: Not a composite node, always raises an exception
        """
        raise Exception('Node is not a composite node, invalid traversal rule')


class ParallelNode(BaseNode):
    """
    A composite node containing many graph objects within itself.

    The structure is generally:
        ParallelNode:
            Branch A:
                Sub graph definition
            Branch B:
                Sub graph definition
            . . .

    We currently support parallel nodes within parallel nodes.
    """
    node_type = 'parallel'

    def __init__(self, name, internal_name, config, execution_type, internal_branch_name=None):
        # pylint: disable=R0914,R0913
        super().__init__(name, internal_name, config, execution_type, internal_branch_name=internal_branch_name)
        self.branches = self.get_sub_graphs()

    def get_sub_graphs(self):
        """
        For the branches mentioned in the config['branches'], create a graph object.
        The graph object is also given an internal naming convention following a dot path convention

        Returns:
            dict: A branch_name: dag for every branch mentioned in the branches
        """
        branches = {}
        for branch_name, branch_config in self.config['branches'].items():
            sub_graph = create_graph(branch_config, internal_branch_name=self.internal_name + '.' + branch_name)
            branches[self.internal_name + '.' + branch_name] = sub_graph

        if not branches:
            raise Exception('A parallel node should have branches')
        return branches

    def get_branch_by_name(self, branch_name: str):
        """
        Retrieve a branch by name.
        The name is expected to follow a dot path convention.

        Returns a Graph Object

        Args:
            branch_name (str): The name of the branch to retrieve

        Raises:
            Exception: If the branch by that name does not exist
        """
        if branch_name in self.branches:
            return self.branches[branch_name]

        raise Exception(f'No branch by name: {branch_name} is present in {self.name}')

    def execute(self, executor, mock=False, map_variable: dict = None, **kwargs):
        """
        This method should never be called for a node of type Parallel

        Args:
            executor (BaseExecutor): The Executor class as defined by the config
            mock (bool, optional): If the operation is just a mock. Defaults to False.

        Raises:
            NotImplementedError: This method should never be called for a node of type Parallel
        """
        raise Exception('Node is of type composite, error in traversal rules')

    def execute_as_graph(self, executor, map_variable: dict = None, **kwargs):
        """
        This function does the actual execution of the sub-branches of the parallel node.

        From a design perspective, this function should not be called if the execution mode is 3rd party orchestrated.

        The modes that render the job specifications, do not need to interact with this node at all as they have their
        own internal mechanisms of handing parallel states.
        If they do not, find a better orchestrator or use as-is state to make it work.

        The execution of a dag, could result in
            * The dag being completely executed with a definite (fail, success) state in case of
                local or local-container execution
            * The dag being in a processing state with PROCESSING status in case of local-aws-batch

        Only fail state is considered failure during this phase of execution.

        Args:
            executor (Executor): The Executor as per the use config
            **kwargs: Optional kwargs passed around
        """
        # Preapre the branch logs
        for internal_branch_name, branch in self.branches.items():
            effective_branch_name = self.resolve_map_placeholders(internal_branch_name, map_variable=map_variable)

            branch_log = executor.run_log_store.create_branch_log(effective_branch_name)
            branch_log.status = defaults.PROCESSING
            executor.run_log_store.add_branch_log(branch_log, executor.run_id)

        #pool = multiprocessing.Pool(multiprocessing.cpu_count() - 1)
        jobs = []
        for internal_branch_name, branch in self.branches.items():
            if executor.is_parallel_execution():
                # Trigger parallel jobs
                action = magnus.pipeline.execute_single_brach
                kwargs = {
                    'configuration_file': executor.configuration_file,
                    'pipeline_file': executor.pipeline_file,
                    'variables_file': executor.variables_file,
                    'branch_name': internal_branch_name,
                    'run_id': executor.run_id,
                    'map_variable': json.dumps(map_variable)
                }
                process = multiprocessing.Process(target=action, kwargs=kwargs)
                jobs.append(process)
                process.start()
                # pool.apply_async(func=action, kwds=kwargs)pool.apply_async(func=action, kwds=kwargs)

            else:
                # If parallel is not enabled, execute them sequentially
                executor.execute_graph(branch, map_variable=map_variable, **kwargs)

        # pool.close()
        # pool.join()
        for job in jobs:
            job.join()  # Find status of the branches

        step_success_bool = True
        waiting = False
        for internal_branch_name, branch in self.branches.items():
            effective_branch_name = self.resolve_map_placeholders(internal_branch_name, map_variable=map_variable)
            branch_log = executor.run_log_store.get_branch_log(effective_branch_name, executor.run_id)
            if branch_log.status == defaults.FAIL:
                step_success_bool = False

            if branch_log.status == defaults.PROCESSING:
                waiting = True

        # Collate all the results and update the status of the step
        effective_internal_name = self.resolve_map_placeholders(self.internal_name, map_variable=map_variable)
        step_log = executor.run_log_store.get_step_log(effective_internal_name, executor.run_id)
        step_log.status = defaults.PROCESSING

        if step_success_bool:  #  If none failed and nothing is waiting
            if not waiting:
                step_log.status = defaults.SUCCESS
        else:
            step_log.status = defaults.FAIL

        executor.run_log_store.add_step_log(step_log, executor.run_id)


class MapNode(BaseNode):
    """
    A composite node that contains ONE graph object within itself that has to be executed with an iterable.

    The structure is genrally:
        MapNode:
            Branch

        The config is expected to have a variable 'iterate_on' which is looked for in the parameters.
        for iter_variable in parameters['iterate_on']:
            Execute the Branch

    The internal naming convention creates branches dynamically based on the iteration value
    Currently, only simple Task, fail, success nodes are tested.
    """
    node_type = 'map'

    def __init__(self, name, internal_name, config, execution_type, internal_branch_name=None):
        # pylint: disable=R0914,R0913
        super().__init__(name, internal_name, config, execution_type, internal_branch_name=internal_branch_name)
        self.iterate_on = self.config.get('iterate_on', None)
        self.iterate_as = self.config.get('iterate_as', None)

        if not self.iterate_on:
            raise Exception('A node type of map requires a parameter iterate_on, please define it in the config')
        if not self.iterate_as:
            raise Exception('A node type of map requires a parameter iterate_as, please define it in the config')

        self.branch_placeholder_name = defaults.MAP_PLACEHOLDER
        self.branch = self.get_sub_graph()

    def get_sub_graph(self):
        """
        Create a sub-dag from the config['branch']

        The graph object has an internal branch name, that is equal to the name of the step.
        And the sub-dag nodes follow an dot path naming convention

        Returns:
            Graph: A graph object
        """
        branch_config = self.config['branch']
        branch = create_graph(
            branch_config, internal_branch_name=self.internal_name + '.' + self.branch_placeholder_name)
        return branch

    def get_branch_by_name(self, branch_name: str):
        """
        Retrieve a branch by name.

        In the case of a Map Object, the branch naming is dynamic as it is parameterised on iterable.
        This method takes no responsibility in checking the validity of the naming.

        Returns a Graph Object

        Args:
            branch_name (str): The name of the branch to retrieve

        Raises:
            Exception: If the branch by that name does not exist
        """
        return self.branch

    def execute(self, executor, mock=False, map_variable: dict = None, **kwargs):
        """
        This method should never be called for a node of type Parallel

        Args:
            executor (BaseExecutor): The Executor class as defined by the config
            mock (bool, optional): If the operation is just a mock. Defaults to False.

        Raises:
            NotImplementedError: This method should never be called for a node of type Parallel
        """
        raise Exception('Node is of type composite, error in traversal rules')

    def execute_as_graph(self, executor, map_variable: dict = None,  **kwargs):
        """
        This function does the actual execution of the branch of the map node.

        From a design perspective, this function should not be called if the execution mode is 3rd party orchestrated.
        Only modes that are currently accepted are: local, local-container, local-aws-batch.

        The modes that render the job specifications, do not need to interact with this node at all as
        they have their own internal mechanisms of handing map states or dynamic parallel states.
        If they do not, find a better orchestrator or use as-is state to make it work.

        The actual logic is :
            * We iterate over the iterable as mentioned in the config
            * For every value in the iterable we call the executor.execute_graph(branch, iter_variable)

        The execution of a dag, could result in
            * The dag being completely executed with a definite (fail, success) state in case of local
                or local-container execution
            * The dag being in a processing state with PROCESSING status in case of local-aws-batch

        Only fail state is considered failure during this phase of execution.

        Args:
            executor (Executor): The Executor as per the use config
            map_variable (dict): The map variables the graph belongs to
            **kwargs: Optional kwargs passed around
        """
        run_log = executor.run_log_store.get_run_log_by_id(executor.run_id)
        if self.iterate_on not in run_log.parameters:
            raise Exception(
                f'Expected parameter {self.iterate_on} not present in Run Log parameters, was it ever set before?')

        iterate_on = run_log.parameters[self.iterate_on]
        if not isinstance(iterate_on, list):
            raise Exception('Only list is allowed as a valid iterator type')

        # Prepare the branch logs
        for iter_variable in iterate_on:
            effective_branch_name = self.resolve_map_placeholders(
                self.internal_name + '.' + str(iter_variable),
                map_variable=map_variable)
            branch_log = executor.run_log_store.create_branch_log(effective_branch_name)
            branch_log.status = defaults.PROCESSING
            executor.run_log_store.add_branch_log(branch_log, executor.run_id)

        #pool = multiprocessing.Pool(multiprocessing.cpu_count() - 1)
        jobs = []
        for iter_variable in iterate_on:
            effective_map_variable = map_variable or OrderedDict()
            effective_map_variable[self.iterate_as] = iter_variable

            if executor.is_parallel_execution():
                # Trigger parallel jobs
                action = magnus.pipeline.execute_single_brach
                kwargs = {
                    'configuration_file': executor.configuration_file,
                    'pipeline_file': executor.pipeline_file,
                    'variables_file': executor.variables_file,
                    'branch_name': self.branch.internal_branch_name,
                    'run_id': executor.run_id,
                    'map_variable': json.dumps(effective_map_variable)
                }
                process = multiprocessing.Process(target=action, kwargs=kwargs)
                jobs.append(process)
                process.start()
                #pool.apply_async(func=action, kwds=kwargs)

            else:
                # If parallel is not enabled, execute them sequentially
                executor.execute_graph(self.branch, map_variable=effective_map_variable, **kwargs)

        # pool.close()
        # pool.join()
        for job in jobs:
            job.join()
        # # Find status of the branches
        step_success_bool = True
        waiting = False
        for iter_variable in iterate_on:
            effective_branch_name = self.resolve_map_placeholders(self.internal_name + '.' + str(iter_variable),
                                                                  map_variable=map_variable)
            branch_log = executor.run_log_store.get_branch_log(
                effective_branch_name, executor.run_id)
            if branch_log.status == defaults.FAIL:
                step_success_bool = False

            if branch_log.status == defaults.PROCESSING:
                waiting = True

        # Collate all the results and update the status of the step
        effective_internal_name = self.resolve_map_placeholders(self.internal_name, map_variable=map_variable)
        step_log = executor.run_log_store.get_step_log(effective_internal_name, executor.run_id)
        step_log.status = defaults.PROCESSING

        if step_success_bool:  #  If none failed and nothing is waiting
            if not waiting:
                step_log.status = defaults.SUCCESS
        else:
            step_log.status = defaults.FAIL

        executor.run_log_store.add_step_log(step_log, executor.run_id)


class DagNode(BaseNode):
    """
    A composite node that internally holds a dag.

    The structure is genrally:
        DagNode:
            dag_definition: A YAML file that holds the dag in 'dag' block

        The config is expected to have a variable 'dag_definition'.

    Currently, only simple Task, fail, success nodes are tested.
    No variable substitution is allowed as of now.

    """
    node_type = 'dag'

    def __init__(self, name, internal_name, config, execution_type, internal_branch_name=None):
        # pylint: disable=R0914,R0913
        super().__init__(name, internal_name, config, execution_type, internal_branch_name=internal_branch_name)
        self.sub_dag_file = self.config.get('dag_definition', None)

        if not self.sub_dag_file:
            raise Exception('A node type of dag requires a parameter dag_definition, please define it in the config')

        self.branch = self.get_sub_graph()

    @property
    def _internal_branch_name(self):
        """
        THe internal branch name in dot path convention

        Returns:
            [type]: [description]
        """
        return self.internal_name + '.' + defaults.DAG_BRANCH_NAME

    def get_sub_graph(self):
        """
        Create a sub-dag from the config['dag_definition']

        The graph object has an internal branch name, that is equal to the name of the step.
        And the sub-dag nodes follow an dot path naming convention

        Returns:
            Graph: A graph object
        """
        dag_config = utils.load_yaml(self.sub_dag_file)
        if 'dag' not in dag_config:
            raise Exception(f'No DAG found in {self.sub_dag_file}, please provide it in dag block')

        branch = create_graph(dag_config['dag'],
                              internal_branch_name=self._internal_branch_name)
        return branch

    def get_branch_by_name(self, branch_name: str):
        """
        Retrieve a branch by name.
        The name is expected to follow a dot path convention.

        Returns a Graph Object

        Args:
            branch_name (str): The name of the branch to retrieve

        Raises:
            Exception: If the branch_name is not 'dag'
        """
        if branch_name != self._internal_branch_name:
            raise Exception(f'Node of type {self.node_type} only allows a branch of name {defaults.DAG_BRANCH_NAME}')

        return self.branch

    def execute(self, executor, mock=False, map_variable: dict = None, **kwargs):
        """
        This method should never be called for a node of type Parallel

        Args:
            executor (BaseExecutor): The Executor class as defined by the config
            mock (bool, optional): If the operation is just a mock. Defaults to False.

        Raises:
            NotImplementedError: This method should never be called for a node of type Parallel
        """
        raise Exception('Node is of type composite, error in traversal rules')

    def execute_as_graph(self, executor, map_variable: dict = None, **kwargs):
        """
        This function does the actual execution of the branch of the dag node.

        From a design perspective, this function should not be called if the execution mode is 3rd party orchestrated.
        Only modes that are currently accepted are: local, local-container, local-aws-batch.
        The modes that render the job specifications, do not need to interact with this node at all
        as they have their own internal mechanisms of handling sub dags.
        If they do not, find a better orchestrator or use as-is state to make it work.

        The actual logic is :
            * We just execute the branch as with any other composite nodes
            * The branch name is called 'dag'

        The execution of a dag, could result in
            * The dag being completely executed with a definite (fail, success) state in case of
                local or local-container execution
            * The dag being in a processing state with PROCESSING status in case of local-aws-batch

        Only fail state is considered failure during this phase of execution.

        Args:
            executor (Executor): The Executor as per the use config
            **kwargs: Optional kwargs passed around
        """
        step_success_bool = True
        waiting = False

        effective_branch_name = self.resolve_map_placeholders(self._internal_branch_name, map_variable=map_variable)
        effective_internal_name = self.resolve_map_placeholders(self.internal_name, map_variable=map_variable)

        branch_log = executor.run_log_store.create_branch_log(effective_branch_name)
        branch_log.status = defaults.PROCESSING
        executor.run_log_store.add_branch_log(branch_log, executor.run_id)

        executor.execute_graph(self.branch, map_variable=map_variable, **kwargs)

        branch_log = executor.run_log_store.get_branch_log(effective_branch_name, executor.run_id)
        if branch_log.status == defaults.FAIL:
            step_success_bool = False

        if branch_log.status == defaults.PROCESSING:
            waiting = True

        step_log = executor.run_log_store.get_step_log(effective_internal_name, executor.run_id)
        step_log.status = defaults.PROCESSING

        if step_success_bool:  #  If none failed and nothing is waiting
            if not waiting:
                step_log.status = defaults.SUCCESS
        else:
            step_log.status = defaults.FAIL

        executor.run_log_store.add_step_log(step_log, executor.run_id)


class AsISNode(BaseNode):
    """
    AsIs is a convenience design node.

    It always returns success in the attempt log and does nothing during interactive compute.
        i.e. local, local-container, local-aws-batch
    The command given to execute is ignored but it does do the syncing of the catalog.
    This node is very akin to pass state in Step functions.

    This node type could be handy when designing the pipeline and stubbing functions

    But in render mode for job specification of a 3rd party orchestrator, this node comes handy.
    """
    node_type = 'as-is'

    def __init__(self, name, internal_name, config, execution_type, internal_branch_name=None):
        # pylint: disable=R0914,R0913
        super().__init__(name, internal_name, config, execution_type, internal_branch_name=internal_branch_name)
        self.render_string = self.config.get('render_string', None)

    def execute(self, executor, mock=False, map_variable: dict = None, **kwargs):
        """
        Do Nothing node.
        We just send an success attempt log back to the caller

        Args:
            executor ([type]): [description]
            mock (bool, optional): [description]. Defaults to False.
            map_variable (str, optional): [description]. Defaults to ''.

        Returns:
            [type]: [description]
        """
        attempt_log = executor.run_log_store.create_attempt_log()

        attempt_log.start_time = str(datetime.now())
        attempt_log.status = defaults.SUCCESS  # This is a dummy node and always will be success

        attempt_log.end_time = str(datetime.now())
        attempt_log.duration = utils.get_duration_between_datetime_strings(
            attempt_log.start_time, attempt_log.end_time)
        return attempt_log

    def execute_as_graph(self, executor, map_variable: dict = None, **kwargs):
        """
        Should not be implemented for a single node.

        Args:
            executor ([type]): [description]

        Raises:
            Exception: Not a composite node, always raises an exception
        """
        raise Exception('Node is not a composite node, invalid traversal rule')
