# python3
# Copyright 2018 DeepMind Technologies Limited. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""DQN agent implementation."""

from acme import specs
from acme.agents import agent
from acme.agents import replay
from acme.agents.jax import actors
from acme.agents.jax.dqn import learning
from acme.jax import variable_utils
import dataclasses
import haiku as hk
import jax.numpy as jnp
import numpy as np
import optax
import rlax


@dataclasses.dataclass
class DQNConfig:
  """Configuration options for DQN agent."""
  epsilon: float = 0.05  # Action selection via epsilon-greedy policy.
  samples_per_insert: float = 0.5  # Ratio of learning samples to insert.
  seed: int = 1  # Random seed.

  # Learning rule
  learning_rate: float = 1e-3  # Learning rate for Adam optimizer.
  discount: float = 0.99  # Discount rate applied to value per timestep.
  n_step: int = 5  # N-step TF learning.
  target_update_period: int = 100  # Update target network every period.
  max_gradient_norm: float = np.inf  # For gradient clipping.

  # Replay options
  batch_size: int = 256  # Number of transitions per batch.
  min_replay_size: int = 1_000  # Minimum replay size.
  max_replay_size: int = 1_000_000  # Maximum replay size.
  importance_sampling_exponent: float = 0.2  # Importance sampling for replay.
  priority_exponent: float = 0.6  # Priority exponent for replay.
  prefetch_size: int = 4  # Prefetch size for reverb replay performance.


class DQNFromConfig(agent.Agent):
  """DQN agent.

  This implements a single-process DQN agent. This is a simple Q-learning
  algorithm that inserts N-step transitions into a replay buffer, and
  periodically updates its policy by sampling these transitions using
  prioritization.
  """

  def __init__(
      self,
      environment_spec: specs.EnvironmentSpec,
      network: hk.Transformed,
      config: DQNConfig,
  ):
    """Initialize the agent."""
    # Data is communicated via reverb replay.
    reverb_replay = replay.make_reverb_prioritized_nstep_replay(
        environment_spec=environment_spec,
        n_step=config.n_step,
        batch_size=config.batch_size,
        max_replay_size=config.max_replay_size,
        min_replay_size=config.min_replay_size,
        priority_exponent=config.priority_exponent,
        discount=config.discount,
    )
    self._server = reverb_replay.server

    optimizer = optax.chain(
        optax.clip_by_global_norm(config.max_gradient_norm),
        optax.adam(config.learning_rate),
    )
    # The learner updates the parameters (and initializes them).
    learner = learning.DQNLearner(
        network=network,
        obs_spec=environment_spec.observations,
        rng=hk.PRNGSequence(config.seed),
        optimizer=optimizer,
        discount=config.discount,
        importance_sampling_exponent=config.importance_sampling_exponent,
        target_update_period=config.target_update_period,
        iterator=reverb_replay.data_iterator,
        replay_client=reverb_replay.client,
    )

    # The actor selects actions according to the policy.
    def policy(params: hk.Params, key: jnp.ndarray,
               observation: jnp.ndarray) -> jnp.ndarray:
      action_values = network.apply(params, observation)
      return rlax.epsilon_greedy(config.epsilon).sample(key, action_values)
    actor = actors.FeedForwardActor(
        policy=policy,
        rng=hk.PRNGSequence(config.seed),
        variable_client=variable_utils.VariableClient(learner, ''),
        adder=reverb_replay.adder)

    super().__init__(
        actor=actor,
        learner=learner,
        min_observations=max(config.batch_size, config.min_replay_size),
        observations_per_step=config.batch_size / config.samples_per_insert,
    )


class DQN(DQNFromConfig):
  """DQN agent.

  We are in the process of migrating towards a more modular agent configuration.
  This is maintained now for compatibility.
  """

  def __init__(
      self,
      environment_spec: specs.EnvironmentSpec,
      network: hk.Transformed,
      batch_size: int = 256,
      prefetch_size: int = 4,
      target_update_period: int = 100,
      samples_per_insert: float = 0.5,
      min_replay_size: int = 1000,
      max_replay_size: int = 1000000,
      importance_sampling_exponent: float = 0.2,
      priority_exponent: float = 0.6,
      n_step: int = 5,
      epsilon: float = 0.05,
      learning_rate: float = 1e-3,
      discount: float = 0.99,
      seed: int = 1,
  ):
    config = DQNConfig(
        batch_size=batch_size,
        prefetch_size=prefetch_size,
        target_update_period=target_update_period,
        samples_per_insert=samples_per_insert,
        min_replay_size=min_replay_size,
        max_replay_size=max_replay_size,
        importance_sampling_exponent=importance_sampling_exponent,
        priority_exponent=priority_exponent,
        n_step=n_step,
        epsilon=epsilon,
        learning_rate=learning_rate,
        discount=discount,
        seed=seed,
    )
    super().__init__(
        environment_spec=environment_spec,
        network=network,
        config=config,
    )
