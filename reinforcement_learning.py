# do not use eager execution when possible (use tf.function)
# make this a library --> general rl library in tf 2
from functools import partial
from typing import Tuple, Any, Optional, List, Callable

from abc import ABC, abstractmethod

import numpy as np
import tensorflow as tf

from environment import Environment, EnvironmentCallbacks, EnvironmentController
from utils import Config, Gradient, add_gradients, MemVariable

keras = tf.keras


class RLModel(keras.Model, ABC):
    @abstractmethod
    def call(self, inputs: np.ndarray) -> Tuple[tf.Tensor, ...]:
        pass

    @abstractmethod
    def compute_loss(self, action_history: tf.Tensor, reward_history: tf.Tensor,
                     *inner_measures_histories: List[tf.Tensor]) -> tf.Tensor:
        pass

    @abstractmethod
    def get_log_values(self) -> List[Tuple[str, tf.Tensor]]:
        pass


class RLCoordinator(ABC):
    @abstractmethod
    def start_learning(self) -> None:
        pass

    @abstractmethod
    def add_gradient(self, agent_id: int, gradient: Gradient) -> None:
        pass


# rethink the functions (like iteration count should be input of start learning, etc.) or names and meaning of
#   classes (like RLCoordinator is only about learning, while RLAgent plays too!)
# this should inherit from Model
class RLAgent(ABC, EnvironmentCallbacks, EnvironmentController):

    def __init__(self, id: int, coordinator: Optional[RLCoordinator], environment: Environment, rl_model: RLModel,
                 optimizer: keras.optimizers.Optimizer, summary_writer: tf.summary.SummaryWriter,
                 config: Config):
        self.id = id
        self.coordinator = coordinator
        self.environment = environment
        self.rl_model = rl_model
        self.optimizer = optimizer
        self.summary_writer = summary_writer
        self.steps_per_gradient_update = config['steps_per_gradient_update']
        self.total_episodes = config['total_episodes']
        self.steps = 0

        self.episode_reward = MemVariable(lambda: keras.metrics.Sum(dtype=tf.float32))
        self.mean_episode_reward = MemVariable(lambda: keras.metrics.Mean(dtype=tf.float32))
        self.mean_loss = MemVariable(lambda: keras.metrics.Mean(dtype=tf.float32))
        self.tape = MemVariable(lambda: tf.GradientTape())
        self.total_gradient = MemVariable(lambda: 0)
        self.states = MemVariable(lambda: [])
        self.realized_actions = MemVariable(lambda: [])

        self.inner_measures = None
        self.action_history = None
        self.reward_history = None

    @abstractmethod
    def realize_action(self, action: int) -> Any:
        pass

    @abstractmethod
    def log_episode(self, states: List[tf.Tensor], actions: list, step: int) -> None:
        pass

    def should_restart(self) -> bool:
        return self.steps < self.total_episodes

    def get_action(self, state: np.ndarray) -> Any:
        action, *self.inner_measures = self.rl_model(tf.expand_dims(state, axis=0))
        self.action_history += [action[0]]
        return self.realize_action(int(action[0]))

    def episode_start(self, state: np.ndarray) -> None:
        self.total_gradient.archive()
        self.states.archive()
        self.realized_actions.archive()

        self.states.value += [state]

        self.action_history = []
        self.reward_history = []

    def episode_end(self) -> None:
        loss = self.rl_model.compute_loss(tf.reshape(self.action_history, shape=(1, -1, 1)),
                                          tf.reshape(self.reward_history, shape=(1, -1, 1)),
                                          *[tf.reshape(measure_history,
                                                       shape=(1, len(inner_measures_histories[0]), -1))
                                            for measure_history in inner_measures_histories])
        self.steps += 1
        if self.steps % self.steps_per_gradient_update == 0:
            self.last_total_gradient = self.total_gradient
            with self.summary_writer.as_default():
                # use callbacks here
                tf.summary.scalar('RLAgent/mean episode reward', self.mean_episode_reward.result(), self.steps)
                tf.summary.scalar('RLAgent/mean loss', self.mean_loss.result(), self.steps)
                tf.summary.scalar('RLAgent/gradient', tf.linalg.global_norm(self.total_gradient), self.steps)
                tf.summary.scalar('RLAgent/weights', tf.linalg.global_norm(self.rl_model.get_weights()), self.steps)
                for metric_name, metric_value in self.rl_model.get_log_values():
                    tf.summary.scalar(f'RLModel/{metric_name}', metric_value, self.steps)
                self.mean_episode_reward.reset_states()
                self.mean_loss.reset_states()
                self.log_episode(self.states + [self.environment.read_state()], self.realized_actions, self.steps)
            # do i have to release the tapes??

    def new_state(self, state: np.ndarray, reward: float) -> None:
        self.episode_reward.update_state(reward)
        self.reward_history += [reward]

    def waiting(self) -> None:
        pass

    @tf.function
    def apply_gradient(self, gradient: Gradient) -> None:
        self.optimizer.apply_gradients(zip(gradient, self.rl_model.trainable_weights))

    def replace_weights(self, reference_agent: 'RLAgent') -> None:
        self.rl_model.set_weights(reference_agent.rl_model.get_weights())

    def build_model(self, input_shape: tuple) -> None:
        self.rl_model.build(input_shape)

    def is_built(self) -> bool:
        return self.rl_model.built

    # add gradient clipping
    def produce_gradient(self, tape: tf.GradientTape, loss: tf.Tensor, total_gradient: Gradient,
                         last_total_gradient: Gradient) -> Gradient:
        if last_total_gradient is not None:
            if self.coordinator is None:
                self.apply_gradient(last_total_gradient)
            else:
                self.coordinator.add_gradient(self.id, last_total_gradient)
        if tape is not None:
            # this should use tf function. but how?
            gradient = tape.gradient(loss, self.rl_model.trainable_weights)
            # is this good and efficient?
            return add_gradients(gradient, total_gradient)
        return 0

    # assumes fixed-length episodes
    # this method is very long! factorize it.
    def start_learning(self, step_count: int, summary_step: int = None) -> None:
        episode_reward = keras.metrics.Sum(dtype=tf.float32)
        mean_episode_reward = keras.metrics.Mean(dtype=tf.float32)
        mean_loss = keras.metrics.Mean(dtype=tf.float32)
        last_tape = None
        loss = None
        last_total_gradient = None
        for step_i in range(step_count):
            total_gradient = 0
            for _ in range(self.steps_per_gradient_update):
                self.environment.restart()
                # if i assume episodes are of same length this can be much more efficient
                action_history = []
                reward_history = []
                inner_measures_histories = []
                with tf.GradientTape() as tape:
                    states = []
                    realized_actions = []
                    while not self.environment.is_finished():
                        state = self.environment.read_state()
                        # is this ok? it should not create new model (i.e. weights) every time!
                        action, *inner_measures = self.rl_model(tf.expand_dims(state, axis=0))
                        realized_action = self.realize_action(int(action[0]))
                        states += [state]
                        realized_actions += [realized_action]
                        with tape.stop_recording():
                            reward, total_gradient = self.environment.act(realized_action,
                                                                          partial(self.produce_gradient, last_tape,
                                                                                  loss, total_gradient,
                                                                                  last_total_gradient))
                            last_tape = None
                            last_total_gradient = None
                        episode_reward.update_state(reward)
                        action_history += [action[0]]
                        reward_history += [reward]
                        for measure_i, inner_measure in enumerate(inner_measures):
                            if len(inner_measures_histories) == measure_i:
                                inner_measures_histories += [[]]
                            inner_measures_histories[measure_i] += [inner_measure[0]]
                    # maybe if i do it outside the for it becomes better (although there may be ram issue)
                    # if i can pass tape to the compute function, i can also compute loss while waiting for action
                    # do i need to convert histories from list to tensor before calling this function?
                    # why do i need the last dimension in action and reward histories?
                    loss = self.rl_model.compute_loss(tf.reshape(action_history, shape=(1, -1, 1)),
                                                      tf.reshape(reward_history, shape=(1, -1, 1)),
                                                      *[tf.reshape(measure_history,
                                                                   shape=(1, len(inner_measures_histories[0]), -1))
                                                        for measure_history in inner_measures_histories])
                mean_loss.update_state(loss)
                mean_episode_reward.update_state(episode_reward.result())
                episode_reward.reset_states()
                last_tape = tape
            if step_i == step_count - 1:
                total_gradient = self.gradient_producer(last_tape, loss, total_gradient, None)()
                self.gradient_producer(None, None, None, total_gradient)()
            elif total_gradient != 0:
                last_total_gradient = total_gradient
            if total_gradient != 0:
                with self.summary_writer.as_default():
                    # use callbacks here
                    tf.summary.scalar('RLAgent/mean episode reward', mean_episode_reward.result(),
                                      summary_step + step_i)
                    tf.summary.scalar('RLAgent/mean loss', mean_loss.result(), summary_step + step_i)
                    tf.summary.scalar('RLAgent/gradient', tf.linalg.global_norm(total_gradient), summary_step + step_i)
                    tf.summary.scalar('RLAgent/weights', tf.linalg.global_norm(self.rl_model.get_weights()),
                                      summary_step + step_i)
                    for metric_name, metric_value in self.rl_model.get_log_values():
                        tf.summary.scalar(f'RLModel/{metric_name}', metric_value, summary_step + step_i)
                    mean_episode_reward.reset_states()
                    mean_loss.reset_states()
                    self.log_episode(states + [self.environment.read_state()], realized_actions, summary_step + step_i)
            # do i have to release the tapes??
