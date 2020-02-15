import glob
import multiprocessing
import os
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from functools import partial
from io import BytesIO
from pathlib import Path
from queue import Empty
from typing import Any, List, Dict, Tuple, Callable, Optional, Union

import numpy as np
import tensorflow as tf
import tensorflow.keras as keras
import yaml
import matplotlib.cm as cm
import scipy.misc
from PIL import Image, ImageDraw

from environment import EnvironmentCallbacks, EnvironmentController, Environment
from phone import DummyPhone, Phone
from relevant_action import RelevantActionEnvironment
from utils import Config, MemVariable, dump_obj, load_obj


class Episode:
    def __init__(self, state: np.ndarray = None, action: np.ndarray = None,
                 reward: np.ndarray = None, result: np.ndarray = None):
        assert (state is None and result is None) or (state.shape == result.shape and state.dtype == result.dtype)
        self.state = state
        self.action = action
        self.reward = reward
        self.result = result


class EpisodeFile:
    def __init__(self, file_name: str, max_size: int, example: Episode, mode: str):
        self.file_name = file_name

        self.states = np.memmap(file_name + '.states.npy',
                                dtype=example.state.dtype, mode=mode, shape=(max_size, 2, *example.state.shape))
        self.actions = np.memmap(file_name + '.actions.npy',
                                 dtype=example.action.dtype, mode=mode, shape=(max_size, *example.action.shape))
        self.rewards = np.memmap(file_name + '.rewards.npy',
                                 dtype=example.reward.dtype, mode=mode, shape=(max_size, *example.reward.shape))

    def get(self, index: int) -> Episode:
        states = self.states[index]
        return Episode(states[0], self.actions[index], self.rewards[index], states[1])

    def set(self, episode: Episode, index: int) -> None:
        self.states[index][0] = episode.state
        self.states[index][1] = episode.result
        self.actions[index] = episode.action
        self.rewards[index] = episode.reward

    def flush(self):
        self.states.flush()
        self.actions.flush()
        self.rewards.flush()

    def close(self):
        del self.states
        del self.actions
        del self.rewards


class DataCollectionAgent(EnvironmentCallbacks, EnvironmentController):
    def __init__(self, id: int, model: keras.Model, example_episode: Episode,
                 create_environment: Callable[['DataCollectionAgent'], Environment], cfg: Config):
        self.max_episodes = cfg['max_episodes']
        self.max_file_size = cfg['max_file_size']
        self.file_dir = cfg['file_dir']

        self.id = id
        self.model = model
        self.example_episode = example_episode

        self.current_file_version = 0
        self.current_file = None
        self.current_file_size = 0
        self.reward_indices = defaultdict(list)
        self.finished_episodes_count = 0
        self.current_episode = MemVariable(lambda: None)
        self.on_file_completed_callbacks = []

        self.reset_file()
        self.environment = create_environment(self)
        self.environment.add_callback(self)

    # the fact that this gets weights means that i cannot set the weights directly from tf (use tf ops for it)
    def update_weights(self, weights: List[tf.Tensor]):
        self.model.set_weights(weights)

    def add_on_file_completed_callbacks(self, callback: Callable[[int, int], None]) -> None:
        self.on_file_completed_callbacks.append(callback)

    def on_file_completed_callback(self) -> None:
        for callback in self.on_file_completed_callbacks:
            callback(self.id, self.current_file_version)

    def start(self):
        self.environment.start()

    def reset_file(self, new_file: bool = True):
        if self.current_file is not None:
            self.current_file.flush()
            self.current_file.close()
            dump_obj({'max_size': self.max_file_size, 'size': self.current_file_size,
                      'example': self.example_episode, 'reward_indices': self.reward_indices},
                     self.current_file.file_name + '.meta')
            self.current_file = None
            self.on_file_completed_callback()
            # notify controller here

        if new_file and self.max_file_size > 0:
            self.current_file_version += 1
            self.current_file_size = 0
            self.reward_indices = defaultdict(list)
            Path(f'{self.file_dir}/{self.current_file_version}').mkdir(parents=True, exist_ok=True)
            self.current_file = EpisodeFile(f'{self.file_dir}/{self.current_file_version}/{self.id}',
                                            self.max_file_size, self.example_episode, 'w+')

    def store_episode(self, episode: Episode) -> None:
        if self.current_file is not None:
            if self.current_file_size == self.max_file_size:
                self.reset_file()
            self.reward_indices[int(episode.reward)].append(self.current_file_size)
            self.current_file.set(episode, self.current_file_size)
            self.current_file_size += 1

    def should_start_episode(self) -> bool:
        res = self.finished_episodes_count < self.max_episodes
        # bad practice
        if not res:
            self.store_episode(self.current_episode.last_value())
            self.reset_file(False)
        return res

    def get_next_action(self, state: np.ndarray) -> Any:
        state = np.expand_dims(state, axis=0)
        return self.model.predict_on_batch(state)[0]

    def on_episode_start(self, state: np.ndarray) -> None:
        self.current_episode.value = Episode()
        self.current_episode.value.state = state

    def on_wait(self) -> None:
        if self.current_episode.has_archive():
            self.store_episode(self.current_episode.last_value())

    def on_state_change(self, src_state: np.ndarray, action: Any, dst_state: np.ndarray, reward: float) -> None:
        self.current_episode.value.reward = np.array(int(reward), self.example_episode.reward.dtype)
        self.current_episode.value.result = dst_state
        self.current_episode.value.action = action

    def on_episode_end(self, premature: bool) -> None:
        self.current_episode.archive()
        self.finished_episodes_count += 1

    def on_error(self) -> None:
        self.current_episode.reset_value()


class LearningAgent:
    def __init__(self, id: int, model: keras.Model, cfg: Config):
        self.file_dir = cfg['file_dir']
        self.shuffle = cfg['shuffle']
        self.correct_distributions = cfg['correct_distributions']
        self.batch_size = cfg['batch_size']
        self.epochs_per_version = cfg['epochs_per_version']
        self.logs_dir = cfg['logs_dir']
        self.save_dir = cfg['save_dir']
        self.save_frequency = cfg['save_frequency']

        self.id = id
        # plot the model (maybe here or where it's created)
        self.model = model

        # self.log_callback = keras.callbacks.TensorBoard(f'{self.logs_dir}{os.sep}learner_{self.id}')

    class EpisodeFileManager:
        def __init__(self, episode_files: List[EpisodeFile]):
            self.episode_files = episode_files

        def __enter__(self):
            pass

        def __exit__(self, exc_type, exc_val, exc_tb):
            for episode_file in self.episode_files:
                episode_file.close()

    def get_weights(self) -> List[tf.Tensor]:
        return self.model.get_weights()

    @staticmethod
    def get_general_example(ex1: Episode, ex2: Episode) -> Episode:
        assert ex1.state.shape == ex2.state.shape and ex1.action.shape == ex2.action.shape \
               and ex1.reward.shape == ex2.reward.shape
        return Episode(ex1.state if ex1.state.itemsize > ex2.state.itemsize else ex2.state,
                       ex1.action if ex1.action.itemsize > ex2.action.itemsize else ex2.action,
                       ex1.reward if ex1.reward.itemsize > ex2.reward.itemsize else ex2.reward,
                       ex1.result if ex1.result.itemsize > ex2.result.itemsize else ex2.result)

    @staticmethod
    def merge_reward_indices_list(file_reward_indices_list: List[Dict[np.ndarray, List[int]]]) \
            -> Dict[np.ndarray, List[int]]:
        total_reward_indices = defaultdict(list)
        for file_i, reward_indices in enumerate(file_reward_indices_list):
            for reward in reward_indices:
                total_reward_indices[reward] += [(file_i, index) for index in reward_indices[reward]]
        return total_reward_indices

    @staticmethod
    def correct_distribution(total_reward_indices: Dict[np.ndarray, List[int]]) -> Tuple[np.ndarray, int]:
        # i can do this distribution correction by clicking on slightly different positions,
        #   or by re-using from previous versions
        # is this way of doing this good? because i have the same samples a lot!
        # i can also not have this and instead use weights in keras

        if len(total_reward_indices) == 1:
            return np.array(-1), -1
        if len(total_reward_indices) != 2:
            raise NotImplementedError('Oops. Currently not supporting non-binary rewards.')
        less_represented_reward = list(total_reward_indices.keys())[np.argmin([len(total_reward_indices[reward])
                                                                               for reward in total_reward_indices])]
        augmented_size = abs(np.subtract(*[len(total_reward_indices[reward]) for reward in total_reward_indices]))
        return less_represented_reward, augmented_size

    @staticmethod
    def index_to_bucket_i_list(bucket_sizes: List[int]) -> List[Tuple[int, int]]:
        total_size = sum(bucket_sizes)
        total_cum_size = np.cumsum(bucket_sizes)
        index_to_bucket_i = []
        current_file_i = 0
        for pos in range(total_size):
            if pos >= total_cum_size[current_file_i]:
                current_file_i += 1
            index_to_bucket_i.append((current_file_i, pos - (total_cum_size[current_file_i]
                                                             if current_file_i > 0 else 0)))
        return index_to_bucket_i

    def read_episode_files(self, version: int) -> \
            Tuple[List[EpisodeFile], List[int], List[Dict[np.ndarray, List[int]]], Episode]:
        example_episode = None

        episode_files = []
        file_sizes = []
        file_reward_indices_list = []
        meta_files = glob.glob(f'{self.file_dir}/{version}/*.meta')
        for meta_file in meta_files:
            meta = load_obj(meta_file)
            example_episode = meta['example'] if example_episode is None else \
                self.get_general_example(example_episode, meta['example'])
            episode_files.append(EpisodeFile(meta_file[:-5], meta['max_size'], meta['example'], 'r'))
            file_sizes.append(meta['size'])
            file_reward_indices_list.append(meta['reward_indices'])

        return episode_files, file_sizes, file_reward_indices_list, example_episode

    def create_training_data(self, version: int) -> Tuple[Optional[Callable], int]:
        episode_files, file_sizes, file_reward_indices_list, example_episode = self.read_episode_files(version)

        total_reward_indices = self.merge_reward_indices_list(file_reward_indices_list)
        if self.correct_distributions:
            less_represented_reward, augmented_size = self.correct_distribution(total_reward_indices)
        else:
            augmented_size = 0

        if augmented_size == -1:
            return None, 0

        total_size = sum(file_sizes)
        training_size = total_size + augmented_size
        # training_data_file = EpisodeFile(f'{self.file_dir}/{version}/training', training_size, example_episode, 'w+')
        positions = np.random.permutation(training_size) if self.shuffle else np.arange(training_size)
        position_to_file_i = self.index_to_bucket_i_list(file_sizes)

        def generator() -> Tuple[Dict[str, np.ndarray], np.ndarray]:
            # if epochs is a lot more than 1, then i should generate a dataset in file instead of this ad hoc method
            current_positions_i = 0
            with self.EpisodeFileManager(episode_files):
                while True:
                    batch_size = min(self.batch_size, training_size - current_positions_i)
                    if batch_size < self.batch_size:
                        current_positions_i = 0
                        batch_size = self.batch_size

                    if current_positions_i == 0 and augmented_size > 0:
                        augmented_data_indices = np.random.choice(
                            np.arange(len(total_reward_indices[less_represented_reward])), augmented_size)
                        augmented_data = np.array(total_reward_indices[less_represented_reward])[augmented_data_indices]

                    x = {'state': np.zeros((batch_size, *example_episode.state.shape),
                                           dtype=example_episode.state.dtype),
                         'action': np.zeros((batch_size, *example_episode.action.shape),
                                            dtype=example_episode.action.dtype),
                         'result': np.zeros((batch_size, *example_episode.result.shape),
                                            dtype=example_episode.result.dtype)}
                    y = np.zeros((batch_size, 1), dtype=np.int32)
                    for i in range(batch_size):
                        position = positions[current_positions_i + i]
                        if position < total_size:
                            file_i = position_to_file_i[position][0]
                            data_i = position_to_file_i[position][1]
                        else:
                            file_i = augmented_data[position - total_size][0]
                            data_i = augmented_data[position - total_size][1]
                        episode = episode_files[file_i].get(data_i)
                        x['state'][i] = episode.state
                        x['action'][i] = episode.action
                        x['result'][i] = episode.result
                        y[i][0] = episode.reward

                    yield x, y

                    current_positions_i = min(training_size, current_positions_i + self.batch_size) % training_size

        return generator, training_size
        # data_index = 0
        # for episode_file, file_size in zip(episode_files, file_sizes):
        #     for file_ind in range(file_size):
        #         training_data_file.set(episode_file.get(file_ind), positions[data_index])
        #         data_index += 1
        #
        # for data_i, data in enumerate(augmented_data):
        #     training_data_file.set(episode_files[data[0]].get(data[1]), positions[total_size + data_i])

    # add logs
    def learn(self, version: int) -> None:
        generator, data_size = self.create_training_data(version)
        if generator is None:
            print(f'The experience version {version} is not expressive enough to learn from.')
        else:
            print(f'starting learning for experience version {version}')
            data = generator()
            checkpoint_callback = keras.callbacks.ModelCheckpoint(f'{self.save_dir}/{version}', monitor='loss',
                                                                  save_best_only=False, save_weights_only=True,
                                                                  save_freq=int(self.save_frequency *
                                                                                (data_size // self.batch_size) *
                                                                                self.batch_size))
            self.model.fit(data, epochs=self.epochs_per_version, steps_per_epoch=data_size // self.batch_size,
                           callbacks=[checkpoint_callback])
            del data


class ThreadLocals:
    def __init__(self):
        self.thread = None
        self.collector = None
        self.new_weight = None

    def pop_and_run_next(self, *local_args) -> None:
        self.thread.pop_and_run_next(*local_args)


class Thread(ABC):
    @abstractmethod
    def add_to_run_queue(self, func: Callable, *args) -> None:
        pass

    @abstractmethod
    def run(self) -> None:
        pass

    # this can only be accessed from the thread associated to this object
    # this method should have a wait input (remember to change the MultiprocessThread implementation if change this)
    @abstractmethod
    def pop_and_run_next(self, *local_args) -> None:
        pass


class Coordinator(ABC, EnvironmentCallbacks):
    def __init__(self, collector_creators: List[Callable[[], DataCollectionAgent]],
                 learner_creator: Callable[[], LearningAgent],
                 tester_creators: List[Callable[[], DataCollectionAgent]]):
        self.collector_creators = collector_creators
        self.learner_creator = learner_creator
        self.tester_creators = tester_creators

        self.learner = None
        self.file_completions = defaultdict(list)

        self.learner_thread = None
        self.collector_threads = []
        self.tester_threads = []

    @abstractmethod
    def get_thread_locals(self) -> ThreadLocals:
        pass

    @abstractmethod
    def create_thread(self, main_func: Callable, *args) -> Thread:
        pass

    @abstractmethod
    def get_main_thread(self) -> Thread:
        pass

    def start_collector(self, collector_creator: Callable[[], DataCollectionAgent], thread: Thread) -> None:
        time.sleep(2)
        collector = collector_creator()
        collector.add_on_file_completed_callbacks(self.on_collector_file_completed)
        collector.environment.add_callback(self)
        self.get_thread_locals().collector = collector
        self.get_thread_locals().thread = thread
        self.get_thread_locals().pop_and_run_next(self)
        collector.start()

    def on_episode_end(self, premature: bool) -> None:
        self.get_thread_locals().pop_and_run_next(self)
        self.local_update_collector_weight()

    def local_set_new_weight(self, new_weight: List[tf.Tensor]) -> None:
        self.get_thread_locals().new_weight = new_weight

    # make these functions with function decorator for coolness :D
    def local_update_collector_weight(self):
        locals = self.get_thread_locals()
        if locals.new_weight is not None:
            locals.collector.update_weights(locals.new_weight)
            locals.new_weight = None

    def sync_weights(self) -> None:
        for collector_thread in self.collector_threads:
            collector_thread.add_to_run_queue(Coordinator.local_set_new_weight, self.learner.get_weights())
        for tester_thread in self.tester_threads:
            tester_thread.add_to_run_queue(Coordinator.local_set_new_weight, self.learner.get_weights())

    def record_collector_file_completion(self, version: int) -> None:
        self.file_completions[version].append(True)
        if len(self.file_completions[version]) == len(self.collector_creators):
            self.learner.learn(version)
            self.sync_weights()

    def on_collector_file_completed(self, id: int, version: int) -> None:
        self.learner_thread.add_to_run_queue(partial(Coordinator.record_collector_file_completion, version=version))

    def start(self):
        self.learner_thread = self.get_main_thread()
        self.collector_threads = [self.create_thread(self.start_collector, c_creator)
                                  for c_creator in self.collector_creators]
        self.tester_threads = [self.create_thread(self.start_collector, t_creator)
                               for t_creator in self.tester_creators]
        [c_thread.run() for c_thread in self.collector_threads]
        [t_thread.run() for t_thread in self.tester_threads]
        self.learner = self.learner_creator()
        self.sync_weights()
        while True:
            self.learner_thread.pop_and_run_next(self)
            time.sleep(1)


# one problem with current framework is that each Thread has only 1 queue
class Process(Thread):
    def __init__(self, name: Optional[str], main_func: Optional[Callable], *args,
                 cfg: Config, main_process: bool = False):
        type = cfg['type']
        queue_size = cfg['queue_size']

        mp = multiprocessing.get_context(type)

        if main_process:
            assert name is None and main_func is None and len(args) == 0
        else:
            self.process = mp.Process(name=name, target=main_func, args=(*args, self))
        self.queue = mp.Queue(queue_size)

    def add_to_run_queue(self, func: Callable, *args) -> None:
        self.queue.put((func, args))

    def run(self) -> None:
        self.process.start()

    def pop_and_run_next(self, *local_args) -> None:
        try:
            func, args = self.queue.get_nowait()
            func(*local_args, *args)
        except Empty:
            pass


class ProcessBasedCoordinator(Coordinator):
    def __init__(self, collector_creators: List[Callable[[], DataCollectionAgent]],
                 learner_creator: Callable[[], LearningAgent],
                 tester_creators: List[Callable[[], DataCollectionAgent]], cfg: Config):
        self.process_configs = cfg['process_configs']

        super().__init__(collector_creators, learner_creator, tester_creators)

        self.thread_count = 0
        self.thread_locals = None

    def get_thread_locals(self) -> ThreadLocals:
        if self.thread_locals is None:
            self.thread_locals = ThreadLocals()
        return self.thread_locals

    def create_thread(self, main_func: Callable, *args) -> Thread:
        return Process(f'process_{self.thread_count}', main_func, *args, cfg=self.process_configs)

    def get_main_thread(self) -> Thread:
        return Process(None, None, cfg=self.process_configs, main_process=True)


class ScreenPreprocessor(keras.layers.Layer):
    def __init__(self, cfg: Config, **kwargs):
        self.grayscale = cfg['grayscale']
        self.crop_top_left = cfg['crop_top_left']
        self.crop_size = cfg['crop_size']
        self.resize_size = cfg['resize_size']
        self.scale_color = cfg['scale_color']
        self.equalize_background = cfg['equalize_background']
        self.contrast_alpha = cfg['contrast_alpha']

        super().__init__(**kwargs)

    def call(self, screens, **kwargs):
        screens = tf.cast(screens, tf.float32) / 255.0
        if self.grayscale:
            screens = tf.image.rgb_to_grayscale(screens)
        screens = tf.image.crop_to_bounding_box(screens, *self.crop_top_left, *self.crop_size)
        screens_shape = tuple([int(d) for d in screens.shape])
        if screens_shape[1:3] != self.resize_size:
            screens = tf.image.resize(screens, self.resize_size)
        screens_shape = tuple([int(d) for d in screens.shape])
        if self.scale_color:
            if screens_shape[-1] != 1:
                raise AttributeError('cannot scale colored images.')
            axes = [1, 2, 3]
            screens = (screens - tf.reduce_min(screens, axis=axes, keep_dims=True)) / \
                      (tf.reduce_max(screens, axis=axes, keep_dims=True) -
                       tf.reduce_min(screens, axis=axes, keep_dims=True))
        if self.equalize_background:
            if screens_shape[-1] != 1:
                raise AttributeError('cannot equalize background for colored images.')
            image_size = screens_shape[1] * screens_shape[2]
            color_sums = tf.reduce_sum(tf.cast(screens < .5, tf.float32), axis=[1, 2, 3])
            screens, _ = tf.map_fn(lambda elems:
                                   (tf.where(elems[1] < image_size / 2, 1 - elems[0], elems[0]), elems[1]),
                                   (screens, color_sums))
        if self.contrast_alpha > 0:
            if not screens_shape[-1] != 1:
                raise AttributeError('cannot change contrast of colored images.')
            screens = tf.sigmoid(self.contrast_alpha * (screens - .5))
        return screens


# instead of this use more complicated existing models
class ScreenParser(keras.layers.Layer):
    def __init__(self, cfg: Config, **kwargs):
        self.padding_type = cfg['padding_type']
        self.filter_nums = cfg['filter_nums']
        self.kernel_sizes = cfg['kernel_sizes']
        self.stride_sizes = cfg['stride_sizes']
        self.maxpool_sizes = cfg['maxpool_sizes']

        super().__init__(**kwargs)

        self.convs = []
        self.maxpools = []

    def build(self, input_shape):
        self.convs = [keras.layers.Conv2D(filters, kernel_size, stride, self.padding_type, activation=tf.nn.elu)
                      for filters, kernel_size, stride in zip(self.filter_nums, self.kernel_sizes, self.stride_sizes)]
        self.maxpools = [keras.layers.Lambda(lambda x: x)
                         if pool_size == 1 else keras.layers.MaxPool2D(pool_size, pool_size, self.padding_type)
                         for pool_size in self.maxpool_sizes]

    def call(self, screens, **kwargs):
        for conv, maxpool in zip(self.convs, self.maxpools):
            screens = maxpool(conv(screens))
        return screens


class RewardPredictor(keras.layers.Layer):
    def __init__(self, action_type_count: int, reward_categories_count: int, cfg: Config, **kwargs):
        super().__init__(**kwargs)

        self.prediction_shape = tuple(cfg['prediction_shape'])
        self.reward_categories_count = reward_categories_count

        self.action_type_count = action_type_count

        self.last_layer = None

        if self.reward_categories_count != 2:
            raise ValueError('For now only support binary rewards.')

    def build(self, input_shape):
        self.last_layer = keras.layers.Conv2D(self.action_type_count, 1, 1, 'VALID', activation=tf.nn.sigmoid)

    def call(self, parsed_screens, **kwargs):
        parsed_screens = self.last_layer(parsed_screens)
        assert tuple(map(int, (parsed_screens.shape[1:-1]))) == self.prediction_shape
        return parsed_screens


class BufferLogger(keras.layers.Layer):
    def __init__(self, freq: int, handler: Callable, aggregate: bool, **kwargs):
        super().__init__(**kwargs)

        self.freq = freq
        self.handler = handler
        self.aggregate = aggregate

        self.log_values = []
        self.log_step = None
        self.dependency = None

    def build(self, input_shape):
        self.log_step = self.add_weight(shape=(), trainable=False,
                                        initializer=lambda *args, **kwargs: 0, dtype=tf.int32)
        self.dependency = self.add_weight(shape=(), trainable=False,
                                          initializer=lambda *args, **kwargs: 0, dtype=tf.int32)

    def call(self, inputs, **kwargs):
        self.log_step = tf.assign_add(self.log_step, 1)

        if self.aggregate:
            with tf.control_dependencies([tf.py_func(lambda v: self.log_values.append(v), (inputs,), [])]):
                with tf.control_dependencies([tf.py_func(partial(cond_flush, buffer_logger=self),
                                                         (self.log_step,), ())]):
                    self.dependency = tf.identity(self.dependency)
        else:
            with tf.control_dependencies([tf.py_func(partial(cond_flush, buffer_logger=self),
                                                     (self.log_step, inputs), ())]):
                self.dependency = tf.identity(self.dependency)

        return self.dependency


def cond_flush(step: int, values: np.ndarray = None, buffer_logger: 'BufferLogger' = None) -> None:
    if values is None:
        values = buffer_logger.log_values
    if step % buffer_logger.freq == 0:
        buffer_logger.handler(values)
        buffer_logger.log_values = []


class CollectorLogger(EnvironmentCallbacks):
    def __init__(self, name: str, screen_preprocessor: ScreenPreprocessor, screen_parser: ScreenParser,
                 reward_predictor: RewardPredictor, action_for_screen: Callable,
                 to_preprocessed_coord: Callable, cfg: Config):
        self.scalar_log_frequency = cfg['scalar_log_frequency']
        self.image_log_frequency = cfg['image_log_frequency']
        self.dir = cfg['dir']
        self.local_change_size = cfg['local_change_size']
        self.crop_top_left = cfg['crop_top_left']
        self.crop_size = cfg['crop_size']

        self.action_for_screen = action_for_screen
        self.to_preprocessed_coord = to_preprocessed_coord

        self.local_step = 0
        self.rewards = []
        self.action = None
        self.preprocessed_screen = None
        self.summary_writer = tf.summary.FileWriter(f'{self.dir}/{name}')
        self.summary = tf.Summary()

        self.dependencies = [
            BufferLogger(self.image_log_frequency,
                         self.buffer_preprocessed_screen, False)(screen_preprocessor.output[0]),
            BufferLogger(self.image_log_frequency, self.log_predictions, False)(reward_predictor.output[0]),
            BufferLogger(self.scalar_log_frequency,
                         partial(self.log_scalar, 'Predictions Mean'), True)(tf.reduce_mean(reward_predictor.output[0]))
        ]

    def get_dependencies(self) -> List[tf.Tensor]:
        return self.dependencies

    def calc_diffs(self, src_state: np.ndarray, action: Any, dst_state: np.ndarray):
        global_diff = np.linalg.norm(RelevantActionEnvironment.crop_state(self, src_state)
                                     - RelevantActionEnvironment.crop_state(self, dst_state))
        action = self.action_for_screen(action[2:]).astype(np.int32)
        local_diff = np.linalg.norm(
            RelevantActionEnvironment.
            crop_to_local(self, RelevantActionEnvironment.crop_state(self, src_state), action)
            - RelevantActionEnvironment.
            crop_to_local(self, RelevantActionEnvironment.crop_state(self, dst_state), action))
        return global_diff, local_diff

    def on_state_change(self, src_state: np.ndarray, action: Any, dst_state: np.ndarray, reward: float) -> None:
        self.local_step += 1
        self.rewards.append(np.array(reward))
        if self.local_step % self.scalar_log_frequency == 0:
            self.log_scalar('Reward', self.rewards)
            self.rewards = []
        if self.local_step % self.image_log_frequency == 0:
            assert self.action is None
            self.action = action
            diffs = self.calc_diffs(src_state, action, dst_state)
            self.log_screen('Screen', src_state, lambda x: x, diffs)
            self.log_screen('Preprocessed Screen', (self.preprocessed_screen * 255).astype(np.uint8),
                            self.to_preprocessed_coord, diffs)
        self.action = None
        self.preprocessed_screen = None

    def on_episode_end(self, premature: bool) -> None:
        self.summary_writer.add_summary(self.summary, self.local_step)
        self.summary = tf.Summary()

    def buffer_preprocessed_screen(self, screen: np.ndarray) -> None:
        assert self.preprocessed_screen is None
        self.preprocessed_screen = screen

    def log_screen(self, name: str, screen: np.ndarray, point_transformer: Callable,
                   diffs: Optional[Tuple[float, float]]) -> None:
        if self.action[-1] != 0:
            raise NotImplementedError('Only one type of action is supported for now.')
        screen = screen.copy()
        if screen.shape[-1] == 1:
            screen = np.concatenate([screen] * 3, axis=-1)
        action_p = point_transformer(self.action_for_screen(self.action[:2])).astype(np.int32)
        screen[max(0, action_p[0] - 3):action_p[0] + 3, max(0, action_p[1] - 3):action_p[1] + 3] = [255, 0, 0]
        transformed_size_x = np.linalg.norm(point_transformer(np.array([0, 0])) -
                                            point_transformer(np.array([self.local_change_size, 0])))
        transformed_size_y = np.linalg.norm(point_transformer(np.array([0, 0])) -
                                            point_transformer(np.array([0, self.local_change_size])))
        screen = self.add_local_border(screen, np.array([transformed_size_x, transformed_size_y]), action_p)

        if diffs is not None:
            screen = self.add_diff_texts(screen, diffs[0], diffs[1])
        self.log_image(name, screen)

    @staticmethod
    def add_local_border(image: np.ndarray, local_size: np.ndarray, action_p: np.ndarray) -> np.ndarray:
        local_size_half = (local_size // 2).astype(np.int32)
        image = image.copy()
        image[max(0, action_p[0] - local_size_half[0]):action_p[0] + local_size_half[0],
        max(0, action_p[1] - local_size_half[1])] = [0, 255, 0]
        image[max(0, action_p[0] - local_size_half[0]):action_p[0] + local_size_half[0],
        min(image.shape[1] - 1, action_p[1] + local_size_half[1])] = [0, 255, 0]
        image[max(0, action_p[0] - local_size_half[0]),
        max(0, action_p[1] - local_size_half[1]):action_p[1] + local_size_half[1]] = [0, 255, 0]
        image[min(image.shape[0] - 1, action_p[0] + local_size_half[0]),
        max(0, action_p[1] - local_size_half[1]):action_p[1] + local_size_half[1]] = [0, 255, 0]
        return image

    @staticmethod
    def add_diff_texts(image: np.ndarray, global_diff: float, local_diff: float) -> np.ndarray:
        image = image.copy()
        image = Image.fromarray(np.uint8(image))
        draw = ImageDraw.Draw(image)
        draw.text((0, 0), f'global: {global_diff}', (255, 0, 0))
        draw.text((0, 16), f'local: {local_diff}', (0, 255, 0))
        return np.array(image)

    def log_predictions(self, pred: np.ndarray) -> None:
        if pred.shape[-1] > 1:
            raise NotImplementedError('Cannot visualize predictions with more than 1 action type.')
        pred = cm.viridis(pred[:, :, 0])[:, :, :3] * 255
        self.log_image('Predictions', pred)

    def log_image(self, name: str, image: np.ndarray) -> None:
        self.summary.value.add(tag=name, image=get_image_summary(image))

    def log_scalar(self, name: str, values: List[np.ndarray]) -> None:
        self.summary.value.add(tag=name, simple_value=np.mean(values))


def get_image_summary(image: np.ndarray) -> tf.Summary.Image:
    bio = BytesIO()
    scipy.misc.toimage(image).save(bio, format="png")
    res = tf.Summary.Image(encoded_image_string=bio.getvalue(), height=image.shape[0], width=image.shape[1])
    bio.close()
    return res


def index_to_action(index: tf.Tensor, preds: tf.Tensor) -> tf.Tensor:
    shape = preds.shape[1:]
    y = tf.cast(index // np.prod(shape[1:]), tf.int32)
    x = tf.cast((index // shape[2]) % shape[1], tf.int32)
    type = tf.cast(index % shape[2], tf.int32)
    return tf.concat([[y], [x], [type]], axis=-1)


def most_probable_weighted_policy_user(probs: tf.Tensor) -> tf.Tensor:
    return tf.argmax(tf.distributions.Multinomial(1.0, probs=probs).sample(), axis=-1)


def better_reward_to_action(preds: tf.Tensor) -> tf.Tensor:
    preds_f = tf.reshape(preds, (-1, np.prod(preds.shape[1:])))
    return index_to_action(most_probable_weighted_policy_user(tf.nn.softmax(preds_f)), preds)


def worse_reward_to_action(preds: tf.Tensor) -> tf.Tensor:
    preds_f = tf.reshape(preds, (-1, np.prod(preds.shape[1:])))
    return index_to_action(
        most_probable_weighted_policy_user(tf.nn.softmax(neg_reward + pos_reward - preds_f)), preds)


def least_certain_reward_to_action(preds: tf.Tensor) -> tf.Tensor:
    preds_f = tf.reshape(preds, (-1, np.prod(preds.shape[1:])))
    mid_reward = (pos_reward + neg_reward) / 2
    dist = pos_reward - mid_reward
    return index_to_action(
        most_probable_weighted_policy_user(tf.nn.softmax(dist - tf.abs(mid_reward - preds_f))), preds)


def most_certain_reward_to_action(preds: tf.Tensor) -> tf.Tensor:
    preds_f = tf.reshape(preds, (-1, np.prod(preds.shape[1:])))
    mid_reward = (pos_reward + neg_reward) / 2
    return index_to_action(
        most_probable_weighted_policy_user(tf.nn.softmax(tf.abs(mid_reward - preds_f))), preds)


def random_reward_to_action(preds: tf.Tensor) -> tf.Tensor:
    preds_f = tf.reshape(preds, (-1, np.prod(preds.shape[1:])))
    return index_to_action(tf.argmax(
        tf.distributions.Multinomial(1.0, probs=tf.ones_like(preds_f) /
                                                tf.cast(tf.shape(preds_f)[-1], tf.float32)).sample(), axis=-1), preds)


def combine_prediction_to_actions(prediction_to_actions: List[Callable], probs: List[int]) -> Callable:
    assert sum(probs) == 1

    def to_actions(preds: tf.Tensor) -> tf.Tensor:
        return np.random.choice(prediction_to_actions, 1, False, probs)[0](preds)

    return lambda inp: tf.py_function(to_actions, [inp], tf.int32)


def prediction_sampler(predictions: tf.Tensor, actions: tf.Tensor) -> tf.Tensor:
    return tf.expand_dims(tf.gather_nd(predictions, actions, batch_dims=1), axis=-1)


def remove_logs(log_dir: str, reset_logs: bool) -> None:
    while reset_logs and os.path.isdir(log_dir) and len(os.listdir(log_dir)) > 0 and \
            len(os.listdir(f'{log_dir}/{os.listdir(log_dir)[0]}')) > 0:
        for agent_dir in os.listdir(log_dir):
            try:
                for f in os.listdir(f'{log_dir}/{agent_dir}'):
                    os.unlink(f'{log_dir}/{agent_dir}/{f}')
            except FileNotFoundError:
                pass


def transform_linearly(coord: np.ndarray, ratio: np.ndarray, offset: np.ndarray, dtype=None) -> np.ndarray:
    res = coord * ratio + offset
    if dtype is not None:
        res = res.astype(dtype)
    return res


def control_dependencies(inputs) -> tf.Tensor:
    x, dependencies = inputs
    with tf.control_dependencies(dependencies):
        x = tf.identity(x)
    return x


def create_agent(id: int, is_learner: bool, is_tester: bool,
                 agent_option_probs: List[float]) -> Union[DataCollectionAgent, LearningAgent]:
    environment_configs = cfg['environment_configs']
    learner_configs = cfg['learner_configs']
    collector_configs = cfg['collector_configs']
    screen_preprocessor_configs = cfg['screen_preprocessor_configs']
    reward_predictor_configs = cfg['reward_predictor_configs']
    phone_configs = cfg['phone_configs']
    screen_parser_configs = cfg['screen_parser_configs']
    collector_logger_configs = cfg['collector_logger_configs']
    dummy_mode = cfg['dummy_mode']
    data_file_dir = cfg['data_file_dir']
    logs_dir = cfg['logs_dir']
    weights_file = cfg['weights_file']
    collectors_apks_path = cfg['collectors_apks_path']
    testers_apks_path = cfg['testers_apks_path']
    screen_shape = phone_configs['screen_shape']
    action_type_count = environment_configs['action_type_count']
    batch_size = learner_configs['batch_size'] if is_learner else 1
    screen_preprocessor_resize_size = screen_preprocessor_configs['resize_size']
    screen_preprocessor_crop_top_left = screen_preprocessor_configs['crop_top_left']
    screen_preprocessor_crop_size = screen_preprocessor_configs['crop_size']
    prediction_shape = reward_predictor_configs['prediction_shape']
    environment_local_change_size = environment_configs['local_change_size']

    environment_configs['pos_reward'] = pos_reward
    environment_configs['neg_reward'] = neg_reward
    environment_configs['steps_per_episode'] = 1
    environment_configs['shuffle'] = True
    environment_configs['crop_top_left'] = screen_preprocessor_crop_top_left
    environment_configs['crop_size'] = screen_preprocessor_crop_size
    phone_configs['crop_top_left'] = screen_preprocessor_crop_top_left
    phone_configs['crop_size'] = screen_preprocessor_crop_size
    phone_configs['apks_path'] = testers_apks_path if is_tester else collectors_apks_path
    collector_configs['file_dir'] = data_file_dir
    if is_tester:
        collector_configs['max_file_size'] = 0
    learner_configs['file_dir'] = data_file_dir
    learner_configs['logs_dir'] = logs_dir
    collector_logger_configs['dir'] = logs_dir
    collector_logger_configs['local_change_size'] = environment_local_change_size
    collector_logger_configs['crop_top_left'] = screen_preprocessor_crop_top_left
    collector_logger_configs['crop_size'] = screen_preprocessor_crop_size

    screen_preprocessor_resize_size_a = np.array(screen_preprocessor_resize_size)
    screen_preprocessor_crop_top_left_a = np.array(screen_preprocessor_crop_top_left)
    screen_preprocessor_crop_size_a = np.array(screen_preprocessor_crop_size)
    prediction_shape_a = np.array(prediction_shape)

    def action_pos_to_screen_pos(action_p: np.ndarray, dtype=None) -> np.ndarray:
        return transform_linearly(action_p + .5, screen_preprocessor_crop_size_a / prediction_shape_a,
                                  screen_preprocessor_crop_top_left_a, dtype)

    example_episode = Episode(np.zeros((*screen_shape, 3), np.uint8), np.zeros(3, np.int32),
                              np.zeros((), np.bool), np.zeros((*screen_shape, 3), np.uint8))

    screen_input = keras.layers.Input(example_episode.state.shape, batch_size,
                                      name='state', dtype=example_episode.state.dtype)

    screen_preprocessor = ScreenPreprocessor(screen_preprocessor_configs)
    screen_parser = ScreenParser(screen_parser_configs)
    reward_predictor = RewardPredictor(action_type_count, 2, reward_predictor_configs)

    predictions = reward_predictor(screen_parser(screen_preprocessor(screen_input)))
    if is_learner:
        action_input = keras.layers.Input(example_episode.action.shape, batch_size,
                                          name='action', dtype=example_episode.action.dtype)
        input = (screen_input, action_input)
        output = keras.layers.Lambda(lambda elems: prediction_sampler(elems[0], elems[1]))((predictions, action_input))
    else:
        input = screen_input
        output = keras.layers.Lambda(combine_prediction_to_actions(prediction_to_action_options,
                                                                   agent_option_probs))(predictions)

    if not is_learner:
        logger = CollectorLogger(f'{"tester" if is_tester else "collector"}_{id}',
                                 screen_preprocessor, screen_parser, reward_predictor, action_pos_to_screen_pos,
                                 lambda p: transform_linearly(p - screen_preprocessor_crop_top_left_a,
                                                              screen_preprocessor_resize_size_a /
                                                              screen_preprocessor_crop_size_a, np.array([0, 0])),
                                 collector_logger_configs)

        output = keras.layers.Lambda(control_dependencies)((output, logger.get_dependencies()))

    model = keras.Model(inputs=input, outputs=output)
    if weights_file is not None:
        model.load_weights(weights_file)

    if is_learner:
        model.compile(optimizer, keras.losses.BinaryCrossentropy())

    phone_type = DummyPhone if dummy_mode else Phone

    def action2pos(action: np.ndarray) -> Tuple[int, int, int]:
        pos = action_pos_to_screen_pos(action[:2], np.int32)
        return pos[1], pos[0], action[2]

    def create_environment(collector: DataCollectionAgent) -> Environment:
        env = RelevantActionEnvironment(collector, phone_type(f'device{id}', 5554 + 2 * id, phone_configs),
                                        action2pos, environment_configs)
        env.add_callback(logger)
        return env

    if is_learner:
        return LearningAgent(id, model, learner_configs)
    else:
        return DataCollectionAgent(id, model, example_episode, create_environment, collector_configs)


def parse_specs_to_probs(specs: Dict, max_len: int) -> List:
    return sum([[[spec[1][i] if len(spec[1]) > i else (1 - sum(spec[1])) / (max_len - len(spec[1]))
                  for i in range(max_len)]] * spec[0]
                for spec in specs], [])


optimizer = 'adam'
pos_reward = 1
neg_reward = 0
with open('configs.yaml') as f:
    cfg = yaml.load(f, Loader=yaml.FullLoader)
collectors = cfg['collectors']
testers = cfg['testers']
reset_logs = cfg['reset_logs']
coordinator_configs = cfg['coordinator_configs']
logs_dir = cfg['logs_dir']

prediction_to_action_options = [better_reward_to_action, worse_reward_to_action,
                                most_certain_reward_to_action, least_certain_reward_to_action,
                                random_reward_to_action]
collector_option_probs = parse_specs_to_probs(collectors, len(prediction_to_action_options))
tester_option_probs = parse_specs_to_probs(testers, len(prediction_to_action_options))

tf.disable_v2_behavior()

if __name__ == '__main__':
    remove_logs(logs_dir, reset_logs)
    collector_creators = [partial(create_agent, i, False, False, probs)
                          for i, probs in enumerate(collector_option_probs)]
    tester_creators = [partial(create_agent, i + len(collector_creators), False, True, probs)
                       for i, probs in enumerate(tester_option_probs)]
    learner_creator = partial(create_agent, len(collector_creators) + len(tester_creators), True, False, None)
    coord = ProcessBasedCoordinator(collector_creators, learner_creator, tester_creators, cfg['coordinator_configs'])
    coord.start()

# metrics