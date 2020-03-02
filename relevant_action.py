import time
import time as tm
import traceback
from datetime import datetime
from typing import Tuple, Callable, Any, Optional

import numpy as np

from environment import Environment, EnvironmentController
from phone import Phone
from utils import Config


# some parts of this should be factorized to a generalized class
class RelevantActionEnvironment(Environment):
    def __init__(self, controller: EnvironmentController, phone: Phone, action2pos: Callable, cfg: Config):
        super(RelevantActionEnvironment, self).__init__(controller)
        self.phone = phone
        self.action2pos = action2pos

        self.steps_per_app = cfg['steps_per_app']
        self.steps_per_episode = cfg['steps_per_episode']
        self.crop_top_left = cfg['crop_top_left']
        self.crop_size = cfg['crop_size']
        self.pos_reward = cfg['pos_reward']
        self.neg_reward = cfg['neg_reward']
        self.steps_per_in_app_check = cfg['steps_per_in_app_check']
        self.force_app_on_top = cfg['force_app_on_top']
        self.in_app_check_trials = cfg['in_app_check_trials']
        self.black_screen_trials = cfg['black_screen_trials']
        self.global_equality_threshold = cfg['global_equality_threshold']
        self.pixel_equality_threshold = cfg['pixel_equality_threshold']
        self.animation_monitor_time = cfg['animation_monitor_time']
        self.action_max_wait_time = cfg['action_max_wait_time']
        self.action_offset_wait_time = cfg['action_offset_wait_time']
        self.action_freeze_wait_time = cfg['action_freeze_wait_time']
        shuffle = cfg['shuffle']
        assert self.steps_per_app % self.steps_per_episode == 0

        self.step = 0
        self.cur_app_index = -1
        self.finished = False
        self.current_state = None
        self.has_state_changed = True
        self.just_restarted = False
        self.in_blank_screen = False
        self.animation_mask = None
        self.changed_from_last = True

        self.phone.start_phone()

        if shuffle:
            # better way for doing this
            np.random.shuffle(self.phone.app_names)

    def start(self):
        while True:
            try:
                super().start()
                break
            # add this in Environment class
            except Exception:
                print(f'{datetime.now()}: exception in phone #{self.phone.device_name}:\n{traceback.format_exc()}')
                self.on_error()

    def restart(self) -> None:
        self.finished = False
        if self.step % self.steps_per_app == 0:
            self.step = 0
            try:
                self.phone.close_app(self.phone.app_names[self.cur_app_index])
            except Exception:
                pass
            self.cur_app_index = (self.cur_app_index + 1) % len(self.phone.app_names)
            # if self.cur_app_index == 0:
            #     self.phone.load_snapshot('fresh')
            self.phone.open_app(self.phone.app_names[self.cur_app_index])
            self.has_state_changed = True
            self.changed_from_last = True

    def is_finished(self) -> bool:
        return self.finished

    def read_state(self) -> np.ndarray:
        if self.has_state_changed:
            trials = self.black_screen_trials
            while trials > 0:
                self.current_state = self.phone.screenshot()
                if self.are_states_equal(np.zeros_like(self.current_state), self.current_state, None):
                    trials -= 1
                    if trials > 0:
                        time.sleep(.5)
                else:
                    self.has_state_changed = False
                    return self.current_state.copy()
            self.in_blank_screen = True
            raise SystemError("blank screen.")
        return self.current_state.copy()

    def crop_state(self, state: np.ndarray) -> np.ndarray:
        return state[
               self.crop_top_left[0]:self.crop_top_left[0] + self.crop_size[0],
               self.crop_top_left[1]:self.crop_top_left[1] + self.crop_size[1]]

    def are_states_equal(self, s1: np.ndarray, s2: np.ndarray, mask: Optional[np.ndarray]) -> bool:
        mask = self.crop_state(np.ones_like(s1) if mask is None else mask)
        return np.linalg.norm(self.crop_state(s1) * mask - self.crop_state(s2) * mask) <= self.global_equality_threshold

    def send_action(self, action: Tuple[int, int, int]):
        trials = self.in_app_check_trials
        while trials > 0:
            if self.step % self.steps_per_in_app_check != 0 or \
                    self.phone.is_in_app(self.phone.app_names[self.cur_app_index], self.force_app_on_top):
                res = self.phone.send_event(*action)
                self.has_state_changed = True
                self.just_restarted = False
                return res
            trials -= 1
            if trials > 0:
                time.sleep(1)
        raise SystemError("invalid phone state.")

    def get_animation_mask(self, wait_action: Callable) -> np.ndarray:
        start_time = tm.time()
        states = []
        did_action = False
        while tm.time() - start_time < self.animation_monitor_time:
            self.has_state_changed = True
            states.append(self.read_state())
            if not did_action:
                wait_action()
                did_action = True
        res = np.all(np.array(states) - states[0] <= self.pixel_equality_threshold, axis=0)
        first_animation = np.where(res == 0)
        first_animation = None if len(first_animation[0]) == 0 else next(zip(*np.where(res == 0)))
        print(f'{datetime.now()}: took {len(states)} screenshots in device #{self.phone.device_name} '
              f' for animation monitoring. First animation is at {first_animation}.')
        return res

    # extend to actions other than click
    # remember to check if the phone is still in the correct app and other wise restart it
    # look at the phone (in dev mode) to make sure the click positions are correctly generated (realize action)
    #   and sent (env and phone (debug these two independently))
    # maybe instead of 2d discrete actions i can have continuous actions (read a3c paper for continuous actions)
    def act(self, action: np.ndarray, wait_action: Callable[[], Any]) -> float:
        action = self.action2pos(action)

        self.step += 1
        if self.step % self.steps_per_episode == 0:
            self.finished = True

        if self.changed_from_last:
            self.animation_mask = self.get_animation_mask(wait_action)

        last_state = self.read_state()

        change_state = self.send_action(action)
        action_time = change_time = tm.time()
        screenshot_count = 1
        changed_screenshot_num = 0

        self.changed_from_last = not self.are_states_equal(last_state, change_state, self.animation_mask)
        if self.changed_from_last:
            changed_screenshot_num = screenshot_count

        tmp_time = tm.time()
        while tmp_time - action_time < self.action_max_wait_time:
            self.has_state_changed = True
            tmp_state = self.read_state()
            screenshot_count += 1
            # remember having animation_mask in this comparison is just an approximation to end this while sooner
            if not self.are_states_equal(tmp_state, change_state, self.animation_mask):
                change_time = tmp_time
                change_state = tmp_state
                if not self.changed_from_last:
                    changed_screenshot_num = screenshot_count
                self.changed_from_last = True
            if tmp_time - action_time >= self.action_offset_wait_time and \
                    tmp_time - change_time >= self.action_freeze_wait_time:
                break
            tmp_time = tm.time()

        print(f'{datetime.now()}: took {screenshot_count} screenshots in device #{self.phone.device_name} '
              f'to compute reward.'
              f' Screen {f"changed at {changed_screenshot_num}" if self.changed_from_last else "did not change"}.')

        if self.changed_from_last:
            reward = self.pos_reward
        else:
            reward = self.neg_reward

        return reward

    def on_error(self):
        super().on_error()
        self.step -= 1

        if self.just_restarted:
            print(f'{datetime.now()}: seems like {self.phone.app_names[self.cur_app_index]} causes trouble. '
                  f'removing it from phone #{self.phone.device_name}')
            self.phone.app_names.remove(self.phone.app_names[self.cur_app_index])
            self.cur_app_index = self.cur_app_index % len(self.phone.app_names)
        else:
            try:
                if self.phone.is_booted():
                    self.phone.open_app(self.phone.app_names[self.cur_app_index])
            except Exception:
                pass
        if self.just_restarted or self.in_blank_screen or \
                not self.phone.is_in_app(self.phone.app_names[self.cur_app_index], self.force_app_on_top):
            self.in_blank_screen = False
            try:
                self.phone.restart()
            except Exception:
                self.phone.start_phone(True)
            self.phone.open_app(self.phone.app_names[self.cur_app_index])
            self.just_restarted = not self.just_restarted
        self.has_state_changed = True
        self.changed_from_last = True
