# Overview

Tracking and recording key metrics from your experiment makes data science a "scientific" process. In magnus, we define
an experiment as anything that touched data and produced some insight. For example, the number of rows in a database
or a CSV could be something that needs to be recorded for later insight.

Magnus, by default, has an experiment tracking tools in its rich logging framework but this could be extended to plug
any of the experiment tracking tools like MLflow or Weights and Biases.

## Configuration

Configuration of a Experiment tracking tools is as follows:

```yaml
experiment_tracking:
  type:
  config:
```

### type

The type of experiment tracking tool you want to use.

There is no default experiment tracking tool as logging structure takes care of that internally for magnus.

### config

Any configuration parameters the experiment tracking tool accepts.


## Interaction within code

In magnus, experiment tracking is defined per step of the pipeline. You can instruct magnus to track a metric by:

```python
# In some step of the pipeline or function or notebook
from magnus import track_this

track_this(answer=42, step=0)
```

This would result in a corresponding entry in ```user_defined_metrics``` of the step log by default and also would be
passed to the underlying experiment tracking tool.

---
!!! Note

    Magnus allows the value pair of the metric to be any JSON friendly datatype. The underlying experiment tracker
    should have mechanisms to handle the same.
---


### step parameter

Many implementations of experiment tracking tools support a step parameter to define the history of the parameter.
Any value other than ```0``` as step parameter would create a ```user_defined_metric``` of ```key_{step}```.

## Environmental variables

You can also track metrics via environmental variables. Any environmental variable with prefix ```MAGNUS_TRACK_``` is
added to the step log.


```python
import os

os.environ['MAGNUS_TRACK_answer']=42
```

---
!!! Note

    Setting metrics via environmental variables would not invoke the underlying experiment tracking tool.
---


## Client context

You can also get a initialized context of the experiment tracking tool to completely control the behavior.

```python
from magnus import get_experiment_tracker_context
context = get_experiment_tracker_context()

with context as ctx:
    # do something
```

## Grouping of experiments

With experiment tracking tools that support grouping of experiments under a logical group, you can use ```tag``` of
magnus.

## Parameterized definition

As with any part of the magnus configuration, you can parameterize the configuration of secrets to switch between
providers without changing the base definition.

Please follow the example provided [here](../dag/#parameterized_definition) for more information.


## Extensions

You can easily extend magnus to bring in your custom provider, if a default
implementation does not exist or you are not happy with the implementation.

To implement your custom experiment tracker, please extend BaseExperimentTracker class of magnus whose definition is
given below.

```python
from pydantic import BaseModel

class BaseExperimentTracker:
    """
    Base Experiment tracker class definition.
    """

    service_name = ''

    class Config(BaseModel):
        pass

    def __init__(self, config: dict = None, **kwargs):  # pylint: disable=unused-argument
        config = config or {}
        self.config = self.Config(**config)

    @property
    def client_context(self):
        return contextlib.nullcontext()

    def log_metric(self, key: str, value: float, step: int = 0):
        """
        Sets the metric in the experiment tracking.

        Args:
            key (str): The key against you want to store the value
            value (float): The value of the metric
            step (int): Optional step at which it was recorded

        Raises:
            NotImplementedError: Base class, hence not implemented
        """
        raise NotImplementedError

    def log_parameter(self, key: str, value: Any):
        """
        Logs a parameter in the experiment tracking.

        Args:
            key (str): The key against you want to store the value
            value (Any): The value of the metric

        Raises:
            NotImplementedError: Base class, hence not implemented
        """
        raise NotImplementedError
```

The custom extensions should be registered as part of the namespace:
```magnus.experiment_tracker.BaseExperimentTracker```  for it to be loaded.

```toml
# For example, as part of your pyproject.toml
[tool.poetry.plugins."magnus.experiment_tracker.BaseExperimentTracker"]
"mlflow" = "YOUR_PACKAGE:mlflow"
```
