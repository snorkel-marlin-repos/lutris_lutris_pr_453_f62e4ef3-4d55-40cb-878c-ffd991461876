import os
import re
import time
import threading
try:
    import pyinotify
    from pyinotify import ProcessEvent
except ImportError:
    pyinotify = None
    ProcessEvent = object
from collections import OrderedDict
from lutris import pga
from lutris.util.log import logger
from lutris.util.system import fix_path_case
from lutris.util.strings import slugify
from lutris.config import make_game_config_id, LutrisConfig


APP_STATE_FLAGS = [
    "Invalid",
    "Uninstalled",
    "Update Required",
    "Fully Installed",
    "Encrypted",
    "Locked",
    "Files Missing",
    "AppRunning",
    "Files Corrupt",
    "Update Running",
    "Update Paused",
    "Update Started",
    "Uninstalling",
    "Backup Running",
    "Reconfiguring",
    "Validating",
    "Adding Files",
    "Preallocating",
    "Downloading",
    "Staging",
    "Committing",
    "Update Stopping"
]


def get_default_acf(appid, name):
    userconfig = OrderedDict()
    userconfig['name'] = name
    userconfig['gameid'] = appid

    appstate = OrderedDict()
    appstate['appID'] = appid
    appstate['Universe'] = "1"
    appstate['StateFlags'] = "1026"
    appstate['installdir'] = name
    appstate['UserConfig'] = userconfig
    return {'AppState': appstate}


def vdf_parse(steam_config_file, config):
    """Parse a Steam config file and return the contents as a dict."""
    line = " "
    while line:
        line = steam_config_file.readline()
        if not line or line.strip() == "}":
            return config
        line_elements = line.strip().split("\"")
        if len(line_elements) == 3:
            key = line_elements[1]
            steam_config_file.readline()  # skip '{'
            config[key] = vdf_parse(steam_config_file, {})
        else:
            try:
                config[line_elements[1]] = line_elements[3]
            except IndexError:
                logger.error("Malformed config file: %s", line)
    return config


def to_vdf(dict_data, level=0):
    """Convert a dictionnary to Steam config file format."""
    vdf_data = ""
    for key in dict_data:
        value = dict_data[key]
        if isinstance(value, dict):
            vdf_data += "%s\"%s\"\n" % ("\t" * level, key)
            vdf_data += "%s{\n" % ("\t" * level)
            vdf_data += to_vdf(value, level + 1)
            vdf_data += "%s}\n" % ("\t" * level)
        else:
            vdf_data += "%s\"%s\"\t\t\"%s\"\n" % ("\t" * level, key, value)
    return vdf_data


def vdf_write(vdf_path, config):
    vdf_data = to_vdf(config)
    with open(vdf_path, "w") as vdf_file:
        vdf_file.write(vdf_data)


def read_config(steam_data_dir):
    config_filename = os.path.join(steam_data_dir, 'config/config.vdf')
    if not os.path.exists(config_filename):
        return
    with open(config_filename, "r") as steam_config_file:
        config = vdf_parse(steam_config_file, {})
    try:
        config = config['InstallConfigStore']['Software']['Valve']['Steam']
    except KeyError as e:
        logger.debug("Steam config empty: %s" % e)
        return
    else:
        return config


def get_appmanifest_from_appid(steamapps_path, appid):
    """Given the steam apps path and appid, return the corresponding appmanifest"""
    if not steamapps_path:
        raise ValueError("steamapps_path is mandatory")
    if not os.path.exists(steamapps_path):
        raise IOError("steamapps_path must be a valid directory")
    if not appid:
        raise ValueError("Missing mandatory appid")
    appmanifest_path = os.path.join(steamapps_path, "appmanifest_%s.acf" % appid)
    if not os.path.exists(appmanifest_path):
        return
    return AppManifest(appmanifest_path)


def get_path_from_appmanifest(steamapps_path, appid):
    """Return the path where a Steam game is installed."""
    appmanifest = get_appmanifest_from_appid(steamapps_path, appid)
    if not appmanifest:
        return
    return appmanifest.get_install_path()


def _get_last_content_log(steam_data_dir):
    """Return the last block from content_log.txt"""
    if not steam_data_dir:
        return []
    path = os.path.join(steam_data_dir, "logs/content_log.txt")
    log = []
    try:
        with open(path, 'r') as f:
            line = f.readline()
            while line:
                # Strip old logs
                if line == "\r\n" and f.readline() == "\r\n":
                    log = []
                    line = f.readline()
                else:
                    log.append(line)
                    line = f.readline()
    except IOError:
        return []
    return log


def get_app_log(steam_data_dir, appid, start_time=None):
    """Return all log entries related to appid from the latest Steam run.

    :param start_time: Time tuple, log entries older than this are dumped.
    """
    if start_time:
        start_time = time.strftime('%Y-%m-%d %T', start_time)

    app_log = []
    for line in _get_last_content_log(steam_data_dir):
        if start_time and line[1:20] < start_time:
            continue
        if " %s " % appid in line[22:]:
            app_log.append(line)
    return app_log


def get_app_state_log(steam_data_dir, appid, start_time=None):
    """Return state entries for appid from latest block in content_log.txt.

    "Fully Installed, Running" means running.
    "Fully Installed" means stopped.

    :param start_time: Time tuple, log entries older than this are dumped.
    """
    state_log = []
    for line in get_app_log(steam_data_dir, appid, start_time):
        line = line.split(' : ')
        if len(line) == 1:
            continue
        if line[0].endswith("state changed"):
            state_log.append(line[1][:-2])
    return state_log


def get_appmanifests(steamapps_path):
    """Return the list for all appmanifest files in a Steam library folder"""
    return [f for f in os.listdir(steamapps_path)
            if re.match(r'^appmanifest_\d+.acf$', f)]


def get_steamapps_paths(flat=False):
    from lutris.runners import winesteam, steam
    if flat:
        steamapps_paths = []
    else:
        steamapps_paths = {
            'linux': [],
            'windows': []
        }
    winesteam_runner = winesteam.winesteam()
    steam_runner = steam.steam()
    for folder in steam_runner.get_steamapps_dirs():
        if flat:
            steamapps_paths.append(folder)
        else:
            steamapps_paths['linux'].append(folder)
    for folder in winesteam_runner.get_steamapps_dirs():
        if flat:
            steamapps_paths.append(folder)
        else:
            steamapps_paths['windows'].append(folder)
    return steamapps_paths


def mark_as_installed(steamid, runner_name, game_info):
    for key in ['name', 'slug']:
        assert game_info[key]
    logger.info("Setting %s as installed" % game_info['name'])
    config_id = (game_info.get('config_path') or make_game_config_id(game_info['slug']))
    game_id = pga.add_or_update(
        steamid=int(steamid),
        name=game_info['name'],
        runner=runner_name,
        slug=game_info['slug'],
        installed=1,
        configpath=config_id,
    )

    game_config = LutrisConfig(
        runner_slug=runner_name,
        game_config_id=config_id,
    )
    game_config.raw_game_config.update({'appid': steamid})
    game_config.save()
    return game_id


def mark_as_uninstalled(game_info):
    assert 'id' in game_info
    assert 'name' in game_info
    logger.info('Setting %s as uninstalled' % game_info['name'])
    game_id = pga.add_or_update(
        id=game_info['id'],
        runner='',
        installed=0
    )
    return game_id


def sync_with_lutris():
    steamapps_paths = get_steamapps_paths()
    steam_games_in_lutris = pga.get_steam_games()
    steamids_in_lutris = set([str(game['steamid']) for game in steam_games_in_lutris])
    seen_ids = set()
    for platform in steamapps_paths:
        for steamapps_path in steamapps_paths[platform]:
            appmanifests = get_appmanifests(steamapps_path)
            for appmanifest_file in appmanifests:
                steamid = re.findall(r'(\d+)', appmanifest_file)[0]
                seen_ids.add(steamid)
                game_info = None
                if steamid not in steamids_in_lutris and platform == 'linux':
                    appmanifest_path = os.path.join(steamapps_path, appmanifest_file)
                    try:
                        appmanifest = AppManifest(appmanifest_path)
                    except Exception:
                        logger.error("Unable to parse file %s", appmanifest_path)
                        continue
                    if appmanifest.is_installed():
                        game_info = {
                            'name': appmanifest.name,
                            'slug': appmanifest.slug,
                        }
                        mark_as_installed(steamid, 'steam', game_info)
                else:
                    for game in steam_games_in_lutris:
                        if str(game['steamid']) == steamid and not game['installed']:
                            game_info = game
                            break
                    if game_info:
                        appmanifest_path = os.path.join(steamapps_path, appmanifest_file)
                        try:
                            appmanifest = AppManifest(appmanifest_path)
                        except Exception:
                            logger.error("Unable to parse file %s", appmanifest_path)
                            continue
                        if appmanifest.is_installed():
                            runner_name = appmanifest.get_runner_name()
                            mark_as_installed(steamid, runner_name, game_info)
    unavailable_ids = steamids_in_lutris.difference(seen_ids)
    for steamid in unavailable_ids:
        for game in steam_games_in_lutris:
            if str(game['steamid']) == steamid \
               and game['installed'] \
               and game['runner'] in ('steam', 'winesteam'):
                mark_as_uninstalled(game)


class SteamWatchHandler(ProcessEvent):
    def __init__(self, callback):
        self.callback = callback

    def process_IN_MODIFY(self, event):
        self.process_event('MODIFY', event.pathname)

    def process_IN_CREATE(self, event):
        self.process_event('CREATE', event.pathname)

    def process_IN_DELETE(self, event):
        self.process_event('DELETE', event.pathname)

    def process_event(self, event_type, path):
        if not path.endswith('.acf'):
            return
        self.callback(event_type, path)


class SteamWatcher(threading.Thread):
    def __init__(self, steamapps_paths, callback=None):
        self.notifier = None
        if not pyinotify:
            logger.error("pyinotify is not installed, "
                         "Lutris won't keep track of steam games")
        else:
            self.steamapps_paths = steamapps_paths
            self.callback = callback
            super(SteamWatcher, self).__init__()
            self.daemon = True
            self.start()

    def run(self):
        watch_manager = pyinotify.WatchManager()
        event_handler = SteamWatchHandler(self.callback)
        mask = pyinotify.IN_CREATE | pyinotify.IN_DELETE | pyinotify.IN_MODIFY
        self.notifier = pyinotify.Notifier(watch_manager, event_handler)
        for steamapp_path in self.steamapps_paths:
            logger.debug('Watching Steam folder %s', steamapp_path)
            watch_manager.add_watch(steamapp_path, mask, rec=False)
        self.notifier.loop()

    def stop(self):
        if self.notifier:
            self.notifier.stop()


class AppManifest:
    def __init__(self, appmanifest_path):
        self.steamapps_path, filename = os.path.split(appmanifest_path)
        self.steamid = re.findall(r'(\d+)', filename)[0]
        if os.path.exists(appmanifest_path):
            with open(appmanifest_path, "r") as appmanifest_file:
                self.appmanifest_data = vdf_parse(appmanifest_file, {})

    @property
    def app_state(self):
        return self.appmanifest_data.get('AppState') or {}

    @property
    def user_config(self):
        return self.app_state.get('UserConfig') or {}

    @property
    def name(self):
        _name = self.app_state.get('name')
        if not _name:
            _name = self.user_config.get('name')
        return _name

    @property
    def slug(self):
        return slugify(self.name)

    @property
    def installdir(self):
        return self.app_state.get('installdir')

    @property
    def states(self):
        """Return the states of a Steam game."""
        states = []
        state_flags = self.app_state.get('StateFlags', 0)
        state_flags = bin(int(state_flags))[:1:-1]
        for index, flag in enumerate(state_flags):
            if flag == '1':
                states.append(APP_STATE_FLAGS[index + 1])
        return states

    def is_installed(self):
        return 'Fully Installed' in self.states

    def get_install_path(self):
        if not self.installdir:
            return
        install_path = fix_path_case(os.path.join(self.steamapps_path, "common",
                                                  self.installdir))
        if install_path:
            return install_path

    def get_platform(self):
        steamapps_paths = get_steamapps_paths()
        if self.steamapps_path in steamapps_paths['linux']:
            return 'linux'
        elif self.steamapps_path in steamapps_paths['windows']:
            return 'windows'
        else:
            raise ValueError("Can't find %s in %s"
                             % (self.steamapps_path, steamapps_paths))

    def get_runner_name(self):
        platform = self.get_platform()
        if platform == 'linux':
            return 'steam'
        else:
            return 'winesteam'
