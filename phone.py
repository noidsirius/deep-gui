from datetime import datetime
import os
import re
import subprocess
import time
from typing import Union

import matplotlib.image as mpimg
import numpy as np

import glob

from utils import Config, run_parallel_command


# prints here should be centralized in a logger
class Phone:
    def __init__(self, device_name: str, port: int, cfg: Config):
        self.device_name = device_name
        self.port = port
        self.emulator_path = cfg['emulator_path']
        self.adb_path = cfg['adb_path']
        self.app_start_wait_time = cfg['app_start_wait_time']
        self.app_exit_wait_time = cfg['app_exit_wait_time']
        self.snapshot_load_wait_time = cfg['snapshot_load_wait_time']
        # self.screenshot_trials = cfg['screenshot_trials']
        self.avd_path = cfg['avd_path']
        apks_path = cfg['apks_path']
        self.aapt_path = cfg['aapt_path']
        self.app_activity_dict = {}
        self.apk_names = glob.glob(f'{apks_path}/*.apk')
        self.app_names = [self.get_app_name(apk_path) for apk_path in self.apk_names]
        if not os.path.exists(f'tmp-{device_name}'):
            os.makedirs(f'tmp-{device_name}')
        self.step = 0

    def adb(self, command: str, as_bytes: bool = False) -> Union[str, bytes]:
        command = f'{self.adb_path} -s emulator-{self.port} {command}'
        res = subprocess.check_output(command, shell=True)
        if not as_bytes:
            return res.decode('utf-8')
        return res

    def is_in_app(self, app_name: str, force_front: bool) -> bool:
        try:
            # add timeout here
            res = self.adb('shell "dumpsys activity | grep TaskRecord"')
            matches = re.findall(r'.*\* Recent .+: TaskRecord{.+#\d+ .+=(.+) .+StackId=(\d+).*}', res)
            print(f'{datetime.now()}: top app of {self.device_name}: {matches[0][0]}')
            if force_front:
                return matches[0][0] == app_name
            # test for when force_front = False
            top_stack_id = matches[0][2]
            for match in matches:
                if match[0] == app_name and match[2] == top_stack_id:
                    return True
        except KeyboardInterrupt:
            raise
        except:
            pass
        return False

    def is_booted(self):
        print(f'{datetime.now()}: checking boot status of {self.device_name}')
        try:
            # this 2s should be param probably
            return self.adb('shell timeout 2s getprop sys.boot_completed') == ('1\r\n' if os.name == 'nt' else '1\n')
        except subprocess.CalledProcessError:
            return False

    def wait_for_start(self) -> None:
        self.adb('wait-for-device')
        while not self.is_booted():
            time.sleep(2)

    def restart(self):
        print(f'{datetime.now()}: restarting {self.device_name}')
        self.adb('emu kill')
        # this is not a good way of checking if the phone is off. because the phone may be already starting,
        #    not completely booted tho. this means that i try to start the phone twice.
        while self.is_booted():
            time.sleep(1)
        self.start_phone(True)

    def start_phone(self, fresh: bool = False) -> None:
        # ref_snapshot_path = f'{self.avd_path}/snapshots/fresh'
        local_snapshot_path = f'{self.avd_path}/{self.device_name}.avd/snapshots/fresh'
        self.start_emulator(fresh)
        # if os.path.exists(ref_snapshot_path):
        #     if not os.path.exists(local_snapshot_path):
        #         copy_tree(ref_snapshot_path, local_snapshot_path)
        if os.path.exists(local_snapshot_path):
            # use -wipe-data instead
            self.load_snapshot('fresh')
        else:
            self.initial_setups()
            self.save_snapshot('fresh')
            # copy_tree(local_snapshot_path, ref_snapshot_path)

    def start_emulator(self, fresh: bool = False) -> None:
        print(f'{datetime.now()}: starting emulator {self.device_name}')
        run_parallel_command(f'{self.emulator_path} -avd {self.device_name} -ports {self.port},{self.port + 1}' +
                             (f' -no-cache' if fresh else ''))
        self.wait_for_start()

    def initial_setups(self) -> None:
        # now that I've updated adb see if i can use this again
        # apks = ' '.join(self.apk_names)
        # self.adb(f'install-multi-package --instant "{apks}"')
        for apk_name in self.apk_names:
            print(f'installing {apk_name}')
            self.adb(f'install -r "{os.path.abspath(apk_name)}"')

        # self.adb('shell settings put global window_animation_scale 0')
        # self.adb('shell settings put global transition_animation_scale 0')
        # self.adb('shell settings put global animator_duration_scale 0')

    def get_app_name(self, apk_path: str) -> str:
        apk_path = os.path.abspath(apk_path)
        command = f'{self.aapt_path} dump badging "{apk_path}" | grep package'
        res = subprocess.check_output(command, shell=True).decode('utf-8')
        regex = re.compile(r'name=\'([^\']+)\'')
        return regex.search(res).group(1)

    def save_snapshot(self, name: str) -> None:
        self.adb(f'emu avd snapshot save {name}')

    def load_snapshot(self, name: str) -> None:
        if self.snapshot_load_wait_time >= 0:
            self.adb(f'emu avd snapshot load {name}')
            time.sleep(self.snapshot_load_wait_time)
            self.sync_time()

    def sync_time(self):
        self.adb('shell su root date ' + datetime.now().strftime('%m%d%H%M%Y.%S'))

    def close_app(self, app_name: str) -> None:
        print(f'{datetime.now()}: closing {app_name} in {self.device_name}')
        self.adb(f'shell su root pm clear {app_name}')
        time.sleep(self.app_exit_wait_time)

    def add_app_activity(self, app_name: str) -> None:
        dat = self.adb(f'shell dumpsys package {app_name} | grep -A1 "android.intent.action.MAIN:"')
        lines = dat.splitlines()
        activityRE = re.compile('([A-Za-z0-9_.]+/[A-Za-z0-9_.]+)')
        self.app_activity_dict[app_name] = activityRE.search(lines[1]).group(1)

    def open_app(self, app_name: str) -> None:
        print(f'{datetime.now()}: opening {app_name} in {self.device_name}')
        if app_name not in self.app_activity_dict:
            self.add_app_activity(app_name)
        self.adb(f'shell am start -n {self.app_activity_dict[app_name]}')
        # here, if the message that says "it's only bringing an existing task to the front" appears, do not wait :|
        time.sleep(self.app_start_wait_time)

    def screenshot(self) -> np.ndarray:
        self.step += 1
        screenshot_dir = os.path.abspath(f'tmp-{self.device_name}')
        self.adb(f'emu screenrecord screenshot {screenshot_dir}')
        image_path = glob.glob(f'tmp-{self.device_name}/Screenshot*.png')[0]
        res = mpimg.imread(image_path)[:, :, :-1]
        os.remove(image_path)
        return res

    def send_event(self, x: int, y: int, type: int) -> None:
        if type != 0:
            raise NotImplementedError()
        # better logging
        print(f'{datetime.now()}: phone {self.device_name}: click on {x},{y}')
        self.adb(f'emu event mouse {x} {y} 0 1')
        self.adb(f'emu event mouse {x} {y} 0 0')


class DummyPhone:
    def __init__(self, device_name: str, port: int, cfg: Config):
        self.screen_shape = tuple(cfg['screen_shape'])
        self.configs = cfg['dummy_mode_configs']
        self.point_nums = self.configs[0]
        self.point_margin = self.configs[1]
        self.click_margin = self.configs[2]
        self.device_name = device_name
        self.app_names = ['dummy']
        self.screen = None
        self.screenshot()

    def restart(self) -> None:
        pass

    def start_phone(self, fresh: bool = False) -> None:
        pass

    def close_app(self, app_name: str) -> None:
        pass

    def open_app(self, app_name: str) -> None:
        pass

    def is_in_app(self, app_name: str, force_front: bool) -> bool:
        return True

    def screenshot(self) -> np.ndarray:
        if self.screen is None:
            self.screen = np.ones((*self.screen_shape, 3)) * 255.0
            self.points = list(zip(np.random.randint(self.screen_shape[0], size=self.point_nums),
                                   np.random.randint(self.screen_shape[1], size=self.point_nums)))
            print(self.points)
            for p in self.points:
                self.screen[max(p[0] - self.point_margin, 0): p[0] + self.point_margin + 1,
                max(p[1] - self.point_margin, 0):p[1] + self.point_margin + 1, :2] = 0.0
        return self.screen / 255.0

    def send_event(self, x: int, y: int, type: int) -> None:
        if type != 0:
            raise NotImplementedError()
        print(f'{datetime.now()}: dummy event sent to {self.device_name}')
        for p in self.points:
            if np.linalg.norm(np.array([y, x]) - np.array(p)) < self.click_margin:
                self.screen = None
                self.screenshot()
                break
