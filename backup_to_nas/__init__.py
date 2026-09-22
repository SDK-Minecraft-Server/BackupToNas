from mcdreforged.api.all import (
    CommandSource,
    PluginServerInterface,
    Requirements,
    Serializable,
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
HelpMessage = '''
§7------§rMCDR Backup To Nas§7------§r
一个打包存档备份为zip并用SFTP协议传输至远端NAS上的插件
§a【格式说明】§r
§7{0}§r 显示帮助信息
§7{0} make§r 创建一个备份
§7{0} interval <interval>§r 设置自动备份的间隔，例:1s|1h|1d
§7{0} interval off§r 禁用自动备份
§7{0} status§r 查询当前 SFTP 上传进度
§7{0} upload§r 上传临时目录中的压缩包
'''.strip().format(Prefix)
creating_backup = Lock()
upload_status_lock = Lock()
upload_status = {
    'phase': 'idle',
    'file': '',
    'transferred': 0,
    'total': 0,
    'error': '',
}
game_saved = False
plugin_unloaded = False
plugin_server = None
auto_backup_stop = None
auto_backup_thread = None


def parse_interval(value: str) -> float:
    value = value.strip().lower()
    if not value:
        return 0
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([shd])', value)
    if match is None or float(match.group(1)) <= 0:
        raise ValueError('自动备份间隔必须是正数加 s、h 或 d，例如 30s（仅支持 s/h/d）')
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
            make_backup(server.get_plugin_command_source())

    auto_backup_thread = Thread(target=loop, name='Backup-To-Nas-Scheduler', daemon=True)
    auto_backup_thread.start()

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
            raise ValueError(f'permissions.{name} 必须是 0 到 4 之间的整数')

    # 命令注册
    builder = SimpleCommandBuilder()

    builder.command(Prefix, help)
    builder.command(f'{Prefix} make', make_backup)
    builder.command(f'{Prefix} interval <interval>', set_interval)
    builder.command(f'{Prefix} status', sftp_status)
    builder.command(f'{Prefix} upload', upload_temp)

    builder.literal(Prefix).requires(
        Requirements.has_permission(config.permissions.help),
        lambda: '§c你没有使用此命令的权限',
    )
    builder.literal('make').requires(
        Requirements.has_permission(config.permissions.make),
        lambda: '§c你没有创建备份的权限',
    )
    builder.literal('interval').requires(
        Requirements.has_permission(config.permissions.interval),
        lambda: '§c你没有修改备份间隔的权限',
    )
    builder.literal('status').requires(
        Requirements.has_permission(config.permissions.status),
        lambda: '§c你没有查询上传状态的权限',
    )
    builder.literal('upload').requires(
        Requirements.has_permission(config.permissions.upload),
        lambda: '§c你没有上传临时文件的权限',
    )

    builder.arg('interval', Text)

    builder.register(server)
    start_auto_backup(server)

def help(callback: CommandSource):
    callback.reply(HelpMessage)


def info_message(source: CommandSource, msg: str, broadcast=False):
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


def upload_sftp(local_path: str):
    sftp_config = config.sftp
    if not sftp_config.host:
        raise ValueError('未配置 sftp.host，无法上传备份')
    if not sftp_config.username:
        raise ValueError('未配置 sftp.username，无法上传备份')
    if not sftp_config.remote_dir:
        raise ValueError('未配置 sftp.remote_dir，无法上传备份')
    password_file = os.path.expanduser(sftp_config.password_file) if sftp_config.password_file else ''
    private_key_file = os.path.expanduser(sftp_config.private_key_file) if sftp_config.private_key_file else ''
    password = ''
    if password_file:
        try:
            # 只去除密码文件结尾的换行，保留密码本身可能包含的空格。
            password = Path(password_file).read_text(encoding='utf-8').rstrip('\r\n')
        except OSError as error:
            raise ValueError(f'无法读取 sftp.password_file：{password_file}') from error
    if bool(password) == bool(private_key_file):
        raise ValueError(
            'sftp.password_file 和 sftp.private_key_file 必须且只能配置一个有效值'
        )

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
            raise ValueError(f'私钥文件不存在：{private_key_file}')
        connect_args['key_filename'] = private_key_file
    else:
        connect_args['password'] = password

    try:
        client.connect(**connect_args)
        with client.open_sftp() as sftp:
            ensure_remote_dir(sftp, sftp_config.remote_dir)
            remote_path = sftp_config.remote_dir.rstrip('/') + '/' + os.path.basename(local_path)
            _set_upload_status(phase='uploading')

            def on_progress(transferred, total):
                _set_upload_status(transferred=transferred, total=total)

            sftp.put(local_path, remote_path, callback=on_progress)
        _set_upload_status(
            phase='completed', transferred=os.path.getsize(local_path),
            total=os.path.getsize(local_path), error='',
        )
    except Exception as error:
        _set_upload_status(phase='failed', error=str(error))
        raise
    finally:
        client.close()


def sftp_status(source: CommandSource):
    status = _get_upload_status()
    phase = status['phase']
    if phase == 'idle':
        source.reply('当前没有进行过 SFTP 上传')
    elif phase == 'connecting':
        source.reply(f"SFTP 正在连接，文件：{status['file']}")
    elif phase == 'uploading':
        total = status['total']
        transferred = status['transferred']
        percent = transferred / total * 100 if total else 0
        source.reply(
            f"SFTP 上传中：{status['file']}，{percent:.1f}% "
            f"（{_format_bytes(transferred)} / {_format_bytes(total)}）"
        )
    elif phase == 'completed':
        source.reply(f"SFTP 上次上传已完成：{status['file']}")
    else:
        source.reply(f"SFTP 上传失败：{status['error'] or '未知错误'}")

@new_thread('Backup-To-Nas-Upload')
def upload_temp(source: CommandSource):
    if not creating_backup.acquire(blocking=False):
        info_message(source, '§c当前已有备份或上传任务，请稍后再试§r')
        return
    try:
        temp_dir = Path(config.temp)
        if not temp_dir.is_dir():
            info_message(source, '临时目录不存在')
            return
        # 备份生成在临时目录顶层，不上传世界副本中的文件。
        archives = sorted(
            path for path in temp_dir.iterdir()
            if path.is_file() and not path.is_symlink() and path.suffix.lower() == '.zip'
        )
        if not archives:
            info_message(source, '临时目录中没有找到 .zip 文件')
            return

        succeeded = failed = 0
        for archive in archives:
            if plugin_unloaded:
                info_message(source, '插件已卸载，停止上传剩余文件')
                return
            info_message(source, f'开始上传 {archive.name}...')
            try:
                upload_sftp(str(archive))
            except Exception as error:
                failed += 1
                _set_upload_status(
                    phase='failed', file=archive.name, transferred=0,
                    total=0, error=str(error),
                )
                source.get_server().logger.exception('上传临时文件失败：%s', archive.name)
                info_message(source, f'§c上传 {archive.name} 失败：{error}§r')
            else:
                succeeded += 1
                info_message(source, f'上传 {archive.name} §a完成§r')
        info_message(source, f'临时文件上传完成：成功 {succeeded} 个，失败 {failed} 个')
    except Exception as error:
        source.get_server().logger.exception('读取临时目录失败')
        info_message(source, f'§c上传任务失败：{error}§r')
    finally:
        creating_backup.release()


@new_thread('Backup-To-Nas')
def make_backup(source: CommandSource):
    global creating_backup
    acquired = creating_backup.acquire(blocking=False)
    auto_save_on = True
    if not acquired:
        info_message(source, '§c正在备份中，请不要重复输入§r')
        return
    try:
        info_message(source, '备份中...请稍等', broadcast=True)
        start_time = time.time()
        # save world
        if config.turn_off_auto_save:
            source.get_server().execute('save-off')
            auto_save_on = False
        global game_saved
        game_saved = False
        source.get_server().execute('save-all flush')
        while True:
            time.sleep(0.01)
            if game_saved:
                break
            if plugin_unloaded:
                source.reply('§c插件卸载，备份中断！§r', broadcast=True)
                return

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

        # zipping worlds
        info_message(source, '创建压缩文件§e{}§r中...'.format(os.path.basename(zip_file_name)), broadcast=True)
        zipf = zipfile.ZipFile(zip_file_name, 'w', zipfile.ZIP_DEFLATED)
        for world in config.world_names:
            add_file(zipf, os.path.join(config.temp, world), world)
        zipf.close()

        # cleaning worlds
        for world in config.world_names:
            shutil.rmtree(os.path.join(config.temp, world))

        info_message(source, '压缩到临时目录§a完成§r，耗时{}秒'.format(round(time.time() - start_time, 1)), broadcast=True)
        info_message(source, '正在上传到 SFTP...', broadcast=True)
        upload_sftp(zip_file_name)
        info_message(source, '上传到 SFTP §a完成§r：{}'.format(config.sftp.remote_dir), broadcast=True)
    except Exception as e:
        info_message(source, '压缩到临时目录§a失败§r，错误代码{}'.format(e), broadcast=True)
        source.get_server().logger.exception('创建备份失败')
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
        callback.reply(f'§c{e}§r')
        return

    config.interval = value
    plugin_server.save_config_simple(config, 'config.json')
    start_auto_backup(plugin_server)
    if value:
        callback.reply(f'自动备份间隔已设置为 §e{value}§r')
    else:
        callback.reply('自动备份已禁用')

def on_unload(server: PluginServerInterface):
    global plugin_unloaded
    plugin_unloaded = True
    stop_auto_backup()

def on_info(server, info):
    if not info.is_user:
        if info.content == 'Saved the game':
            global game_saved
            game_saved = True
