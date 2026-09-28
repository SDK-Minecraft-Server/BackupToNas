from mcdreforged.api.all import (
    CommandSource,
    PluginServerInterface,
    Requirements,
    Serializable,
    ServerInterface,
    SimpleCommandBuilder,
    Text,
    new_thread,
)
from threading import Event, Lock, Thread, current_thread
import time
import zipfile
import os
import posixpath
import re
from typing import List, Dict
import shutil
import paramiko
from pathlib import Path


TRANSLATION_PREFIX = 'backup_to_nas.'


def tr(key, source=None, **values):
    server = source.get_server() if source is not None else plugin_server
    if server is None:
        server = ServerInterface.get_instance()
    if server is None:
        return f'{TRANSLATION_PREFIX}{key}'
    values = {
        name: value.rtext(source) if isinstance(value, LocalizedError) and source is not None
        else value.translate(source) if isinstance(value, LocalizedError) else value
        for name, value in values.items()
    }
    if source is not None:
        return server.rtr(f'{TRANSLATION_PREFIX}{key}', **values)
    return server.tr(f'{TRANSLATION_PREFIX}{key}', **values)


class LocalizedError(ValueError):
    def __init__(self, key, **values):
        self.key = key
        self.values = values
        super().__init__(key)

    def translate(self, source=None):
        server = source.get_server() if source is not None else plugin_server
        if server is None:
            server = ServerInterface.get_instance()
        if server is None:
            return f'{TRANSLATION_PREFIX}{self.key}'
        return server.tr(f'{TRANSLATION_PREFIX}{self.key}', **self.values)

    def rtext(self, source):
        return source.get_server().rtr(f'{TRANSLATION_PREFIX}{self.key}', **self.values)

    def __str__(self):
        return str(self.translate())

class Permissions(Serializable):
    help: int = 0
    make: int = 3
    interval: int = 3
    status: int = 0
    upload: int = 3


class SftpConfig(Serializable):
    host: str = ''
    port: int = 22
    username: str = ''
    password_file: str = ''
    private_key_file: str = ''
    remote_dir: str = '/backups'
    timeout: int = 30
    retry_count: int = 0
    auto_add_host_key: bool = False


class Config(Serializable):
    permissions: Permissions = Permissions()
    interval: str = ''
    temp: str = './backup_to_nas'
    turn_off_auto_save: bool = True
    server_path: str = './server'
    world_names: List[str] = [
        'world'
    ]
    ignore_session_lock: bool = True
    sftp: SftpConfig = SftpConfig()


config = Config()


Prefix = '!!btn'


def plugin_version():
    if plugin_server is None:
        return 'unknown'
    return str(plugin_server.get_self_metadata().version)


creating_backup = Lock()
upload_status_lock = Lock()
upload_status = {
    'phase': 'idle',
    'file': '',
    'transferred': 0,
    'total': 0,
    'error': '',
}
game_saved = Event()
plugin_unloaded = False
plugin_server = None
auto_backup_stop = None
auto_backup_thread = None
SAVE_TIMEOUT = 120
RETRY_DELAY = 5


def parse_interval(value: str) -> float:
    value = value.strip().lower()
    if not value:
        return 0
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([shd])', value)
    if match is None or float(match.group(1)) <= 0:
        raise LocalizedError('interval_invalid')
    amount = float(match.group(1))
    return amount * {'s': 1, 'h': 3600, 'd': 86400}[match.group(2)]


def stop_auto_backup():
    global auto_backup_stop, auto_backup_thread
    if auto_backup_stop is not None:
        auto_backup_stop.set()
    thread = auto_backup_thread
    if thread is not None and thread is not current_thread():
        thread.join(timeout=2)
    auto_backup_stop = None
    auto_backup_thread = None


def wait_for_active_task(timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if creating_backup.acquire(blocking=False):
            creating_backup.release()
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def start_auto_backup(server: PluginServerInterface):
    global auto_backup_stop, auto_backup_thread
    stop_auto_backup()
    seconds = parse_interval(config.interval)
    if seconds <= 0:
        return
    stop_event = Event()
    auto_backup_stop = stop_event

    def loop():
        while not stop_event.wait(seconds):
            if plugin_unloaded:
                return
            backup_thread = make_backup(server.get_plugin_command_source())
            while backup_thread.is_alive():
                if stop_event.wait(0.2):
                    return

    auto_backup_thread = Thread(target=loop, name='Backup-To-Nas-Scheduler', daemon=True)
    auto_backup_thread.start()


def start_pending_upload_recovery(server: PluginServerInterface, previous_module=None):
    try:
        temp_dir = Path(config.temp)
        has_pending = temp_dir.is_dir() and any(
            path.is_file() and not path.is_symlink() and path.suffix.lower() == '.zip'
            for path in temp_dir.iterdir()
        )
    except OSError:
        return
    if not has_pending or plugin_unloaded:
        return

    def recover():
        if previous_module is not None:
            wait = getattr(previous_module, 'wait_for_active_task', None)
            if wait is not None:
                wait()
        if not plugin_unloaded:
            upload_temp(server.get_plugin_command_source())

    Thread(target=recover, name='Backup-To-Nas-Recovery', daemon=True).start()

def on_load(server: PluginServerInterface, prev_module):
    global config, plugin_server, plugin_unloaded
    if prev_module is not None:
        getattr(prev_module, 'stop_auto_backup', lambda: None)()
    plugin_server = server
    plugin_unloaded = False
    config = server.load_config_simple(
        'config.json', target_class=Config, failure_policy='raise'
    )
    for name in ('help', 'make', 'interval', 'status', 'upload'):
        level = getattr(config.permissions, name)
        if type(level) is not int or not 0 <= level <= 4:
            raise LocalizedError('permission_invalid', name=name)
    if type(config.sftp.retry_count) is not int:
        raise LocalizedError('retry_count_invalid')

    # 命令注册
    builder = SimpleCommandBuilder()

    builder.command(Prefix, help)
    builder.command(f'{Prefix} make', make_backup)
    builder.command(f'{Prefix} interval <interval>', set_interval)
    builder.command(f'{Prefix} status', sftp_status)
    builder.command(f'{Prefix} upload', upload_temp)

    builder.literal(Prefix).requires(
        Requirements.has_permission(config.permissions.help),
        lambda source: tr('denied_help', source),
    )
    builder.literal('make').requires(
        Requirements.has_permission(config.permissions.make),
        lambda source: tr('denied_make', source),
    )
    builder.literal('interval').requires(
        Requirements.has_permission(config.permissions.interval),
        lambda source: tr('denied_interval', source),
    )
    builder.literal('status').requires(
        Requirements.has_permission(config.permissions.status),
        lambda source: tr('denied_status', source),
    )
    builder.literal('upload').requires(
        Requirements.has_permission(config.permissions.upload),
        lambda source: tr('denied_upload', source),
    )

    builder.arg('interval', Text)

    builder.register(server)
    start_pending_upload_recovery(server, prev_module)
    start_auto_backup(server)

def help(callback: CommandSource):
    callback.reply(tr('help', callback, prefix=Prefix, version=plugin_version()))


def info_message(source: CommandSource, msg: str, broadcast=False):
    if not isinstance(msg, str):
        if broadcast and source.is_player:
            source.get_server().broadcast(msg)
        else:
            source.reply(msg)
        return
    for line in msg.splitlines():
        text = '[Backup To Nas] ' + line
        if broadcast and source.is_player:
            source.get_server().broadcast(text)
        else:
            source.reply(text)

def touch_temp_folder():
    if not os.path.isdir(config.temp):
        os.makedirs(config.temp)

def add_file(zipf, path, arcpath):
    for dir_path, dir_names, file_names in os.walk(path):
        for file_name in file_names:
            full_path = os.path.join(dir_path, file_name)
            arc_name = os.path.join(arcpath, full_path.replace(path, '', 1).lstrip(os.sep))
            zipf.write(full_path, arcname=arc_name)


def _set_upload_status(**values):
    with upload_status_lock:
        upload_status.update(values)


def _get_upload_status():
    with upload_status_lock:
        return upload_status.copy()


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or unit == 'TiB':
            return f'{size:.1f} {unit}' if unit != 'B' else f'{int(size)} B'
        size /= 1024


def ensure_remote_dir(sftp, remote_dir: str):
    current = '/' if remote_dir.startswith('/') else ''
    for part in remote_dir.split('/'):
        if not part or part == '.':
            continue
        current = posixpath.join(current, part)
        try:
            sftp.stat(current)
        except IOError:
            sftp.mkdir(current)


def upload_file_resumable(sftp, local_path: str, remote_path: str, total: int):
    offset = 0
    try:
        remote_size = sftp.stat(remote_path).st_size
    except IOError:
        remote_size = 0
    else:
        if remote_size == total:
            _set_upload_status(phase='uploading', transferred=total, total=total)
            return
        if remote_size > total:
            sftp.remove(remote_path)
        else:
            offset = remote_size

    mode = 'r+' if offset else 'wb'
    with open(local_path, 'rb') as local_file, sftp.open(remote_path, mode) as remote_file:
        if offset:
            local_file.seek(offset)
            remote_file.seek(offset)
        transferred = offset
        _set_upload_status(phase='uploading', transferred=transferred, total=total)
        while True:
            if plugin_unloaded:
                raise LocalizedError('upload_unloaded')
            chunk = local_file.read(1024 * 1024)
            if not chunk:
                break
            remote_file.write(chunk)
            transferred += len(chunk)
            _set_upload_status(transferred=transferred, total=total)
        remote_file.flush()


def upload_sftp(local_path: str):
    sftp_config = config.sftp
    if not sftp_config.host:
        raise LocalizedError('sftp_missing', field='sftp.host')
    if not sftp_config.username:
        raise LocalizedError('sftp_missing', field='sftp.username')
    if not sftp_config.remote_dir:
        raise LocalizedError('sftp_missing', field='sftp.remote_dir')
    password_file = os.path.expanduser(sftp_config.password_file) if sftp_config.password_file else ''
    private_key_file = os.path.expanduser(sftp_config.private_key_file) if sftp_config.private_key_file else ''
    password = ''
    if password_file:
        try:
            password = Path(password_file).read_text(encoding='utf-8').rstrip('\r\n')
        except OSError as error:
            raise LocalizedError('password_unreadable', path=password_file) from error
    if bool(password) == bool(private_key_file):
        raise LocalizedError('credentials_invalid')

    _set_upload_status(
        phase='connecting', file=os.path.basename(local_path),
        transferred=0, total=os.path.getsize(local_path), error='',
    )

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if sftp_config.auto_add_host_key:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    connect_args = {
        'hostname': sftp_config.host,
        'port': sftp_config.port,
        'username': sftp_config.username,
        'timeout': sftp_config.timeout,
    }
    if private_key_file:
        if not Path(private_key_file).is_file():
            raise LocalizedError('key_missing', path=private_key_file)
        connect_args['key_filename'] = private_key_file
    else:
        connect_args['password'] = password

    try:
        client.connect(**connect_args)
        with client.open_sftp() as sftp:
            ensure_remote_dir(sftp, sftp_config.remote_dir)
            remote_path = sftp_config.remote_dir.rstrip('/') + '/' + os.path.basename(local_path)
            upload_file_resumable(sftp, local_path, remote_path, os.path.getsize(local_path))
            if plugin_unloaded:
                raise LocalizedError('upload_unloaded')
        _set_upload_status(
            phase='completed', transferred=os.path.getsize(local_path),
            total=os.path.getsize(local_path), error='',
        )
    except Exception as error:
        _set_upload_status(phase='failed', error=error if isinstance(error, LocalizedError) else str(error))
        raise
    finally:
        client.close()


def _wait_for_retry() -> bool:
    deadline = time.monotonic() + RETRY_DELAY
    while time.monotonic() < deadline:
        if plugin_unloaded:
            return False
        time.sleep(min(0.2, deadline - time.monotonic()))
    return not plugin_unloaded


def upload_sftp_with_retry(local_path: str):
    retries = config.sftp.retry_count
    attempt = 0
    while True:
        try:
            upload_sftp(local_path)
            return
        except Exception as error:
            attempt += 1
            if (
                plugin_unloaded
                or isinstance(error, LocalizedError)
                or (retries >= 0 and attempt > retries)
            ):
                raise
            logger = plugin_server.logger if plugin_server is not None else None
            if logger is not None:
                logger.warning(
                    tr(
                        'upload_retry_log',
                        file=os.path.basename(local_path),
                        attempt=attempt,
                        delay=RETRY_DELAY,
                        error=error,
                    )
                )
            if not _wait_for_retry():
                raise


def sftp_status(source: CommandSource):
    status = _get_upload_status()
    phase = status['phase']
    if phase == 'idle':
        source.reply(tr('status_idle', source))
    elif phase == 'connecting':
        source.reply(tr('status_connecting', source, file=status['file']))
    elif phase == 'uploading':
        total = status['total']
        transferred = status['transferred']
        percent = transferred / total * 100 if total else 0
        source.reply(tr(
            'status_uploading', source, file=status['file'], percent=percent,
            transferred=_format_bytes(transferred), total=_format_bytes(total),
        ))
    elif phase == 'completed':
        source.reply(tr('status_completed', source, file=status['file']))
    else:
        source.reply(tr('status_failed', source, error=status['error'] or tr('unknown_error', source)))

@new_thread('Backup-To-Nas-Upload')
def upload_temp(source: CommandSource):
    if not creating_backup.acquire(blocking=False):
        info_message(source, tr('busy', source))
        return
    try:
        temp_dir = Path(config.temp)
        if not temp_dir.is_dir():
            info_message(source, tr('temp_missing', source))
            return
        # 备份生成在临时目录顶层，不上传世界副本中的文件。
        archives = sorted(
            path for path in temp_dir.iterdir()
            if path.is_file() and not path.is_symlink() and path.suffix.lower() == '.zip'
        )
        if not archives:
            info_message(source, tr('no_archives', source))
            return

        succeeded = failed = 0
        for archive in archives:
            if plugin_unloaded:
                info_message(source, tr('upload_unloaded', source))
                return
            info_message(source, tr('upload_start', source, file=archive.name))
            try:
                upload_sftp_with_retry(str(archive))
            except Exception as error:
                failed += 1
                _set_upload_status(
                    phase='failed', file=archive.name, transferred=0,
                    total=0, error=error if isinstance(error, LocalizedError) else str(error),
                )
                source.get_server().logger.exception(tr('upload_failed_log', file=archive.name))
                info_message(source, tr('upload_failed', source, file=archive.name, error=error))
            else:
                if plugin_unloaded:
                    info_message(source, tr('upload_unloaded', source))
                    return
                try:
                    archive.unlink()
                except OSError as error:
                    source.get_server().logger.warning(
                        'Uploaded archive could not be removed: %s: %s', archive, error
                    )
                succeeded += 1
                info_message(source, tr('upload_completed', source, file=archive.name))
        info_message(source, tr('upload_summary', source, succeeded=succeeded, failed=failed))
    except Exception as error:
        source.get_server().logger.exception(tr('temp_read_failed'))
        info_message(source, tr('upload_task_failed', source, error=error))
    finally:
        creating_backup.release()


@new_thread('Backup-To-Nas')
def make_backup(source: CommandSource):
    global creating_backup
    acquired = creating_backup.acquire(blocking=False)
    auto_save_on = True
    if not acquired:
        info_message(source, tr('busy', source))
        return
    try:
        info_message(source, tr('backup_start', source), broadcast=True)
        start_time = time.time()
        # save world
        if config.turn_off_auto_save:
            source.get_server().execute('save-off')
            auto_save_on = False
        game_saved.clear()
        source.get_server().execute('save-all flush')
        deadline = time.monotonic() + SAVE_TIMEOUT
        while not game_saved.wait(0.2):
            if plugin_unloaded:
                info_message(source, tr('backup_unloaded', source), broadcast=True)
                return
            if time.monotonic() >= deadline:
                raise LocalizedError('save_timeout', seconds=SAVE_TIMEOUT)

        # copy worlds
        def filter_ignore(path, files):
            return [file for file in files if file == 'session.lock' and config.ignore_session_lock]
        touch_temp_folder()
        for world in config.world_names:
            target_path = os.path.join(config.temp, world)
            if os.path.isdir(target_path):
                shutil.rmtree(target_path)
            shutil.copytree(os.path.join(config.server_path, world), target_path, ignore=filter_ignore)
        if not auto_save_on:
            source.get_server().execute('save-on')
            auto_save_on = True

        # find file name
        file_name_raw = os.path.join(config.temp, time.strftime('%Y-%m-%d_%H-%M-%S', time.localtime()))
        zip_file_name = file_name_raw
        counter = 0
        while os.path.isfile(zip_file_name + '.zip'):
            counter += 1
            zip_file_name = '{}_{}'.format(file_name_raw, counter)
        zip_file_name += '.zip'
        zip_part_name = zip_file_name + '.part'

        # zipping worlds
        info_message(source, tr('compress_start', source, file=os.path.basename(zip_file_name)), broadcast=True)
        zipf = zipfile.ZipFile(zip_part_name, 'w', zipfile.ZIP_DEFLATED)
        for world in config.world_names:
            add_file(zipf, os.path.join(config.temp, world), world)
        zipf.close()
        os.replace(zip_part_name, zip_file_name)

        # cleaning worlds
        for world in config.world_names:
            shutil.rmtree(os.path.join(config.temp, world))

        info_message(source, tr('compress_completed', source, seconds=round(time.time() - start_time, 1)), broadcast=True)
        info_message(source, tr('sftp_start', source), broadcast=True)
        upload_sftp_with_retry(zip_file_name)
        if plugin_unloaded:
            info_message(source, tr('backup_unloaded', source), broadcast=True)
            return
        try:
            os.remove(zip_file_name)
        except OSError as error:
            source.get_server().logger.warning(
                'Uploaded archive could not be removed: %s: %s', zip_file_name, error
            )
        info_message(source, tr('sftp_completed', source, directory=config.sftp.remote_dir), broadcast=True)
    except Exception as e:
        info_message(source, tr('backup_failed', source, error=e), broadcast=True)
        source.get_server().logger.exception(tr('backup_failed_log'))
    finally:
        creating_backup.release()
        if config.turn_off_auto_save and not auto_save_on:
            source.get_server().execute('save-on')



def set_interval(callback: CommandSource, context: dict):
    value = context['interval'].strip().lower()
    if value in ('off', 'disable'):
        value = ''
    try:
        parse_interval(value)
    except ValueError as e:
        callback.reply(tr('command_error', callback, error=e))
        return

    config.interval = value
    plugin_server.save_config_simple(config, 'config.json')
    start_auto_backup(plugin_server)
    if value:
        callback.reply(tr('interval_set', callback, value=value))
    else:
        callback.reply(tr('interval_disabled', callback))

def on_unload(server: PluginServerInterface):
    global plugin_unloaded
    plugin_unloaded = True
    stop_auto_backup()

def on_info(server, info):
    if not info.is_user:
        if info.content and info.content.strip().lower().endswith('saved the game'):
            game_saved.set()
