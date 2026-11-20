"""Export a Compose document containing deployment paths, never GateBot credentials."""
import json
import re
from pathlib import Path, PurePosixPath


def mount_path(value, prefix='/'):
    pattern = r'/[\w./+: -]+' if prefix == '/dev/' else r'/[\w./+ -]+'
    if not isinstance(value, str) or not re.fullmatch(pattern, value) or '..' in PurePosixPath(value).parts:
        raise ValueError('Укажите абсолютный путь Linux')
    value = str(PurePosixPath(value))
    if value == '/' or not value.startswith(prefix):
        raise ValueError('Проверьте каталог или путь устройства')
    return value


def compose(options, settings, modems):
    root = mount_path(options.get('root', '/mnt/ssd-pool/apps-data/gatebot'), '/mnt/')
    host_port = str(options.get('port', '31500'))
    if not host_port.isascii() or not host_port.isdigit() or not 1 <= int(host_port) <= 65535:
        raise ValueError('Проверьте внешний порт')
    network = options.get('network', '').strip()
    if network and not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}', network):
        raise ValueError('Проверьте имя внешней сети Docker')
    version = (Path(__file__).parent/'VERSION').read_text(encoding='utf-8').strip()
    port = int(settings['PORT'])
    if not 1 <= port <= 65535:
        raise ValueError('Проверьте порт веб-сервера')
    quote = json.dumps
    devices = []
    for modem in modems:
        if modem['port'].startswith('/dev/'):
            inside = mount_path(modem['port'], '/dev/')
            outside = mount_path(modem.get('host_device') or inside, '/dev/')
            devices.append((outside,inside))
    gid = options.get('serial_gid', '').strip()
    if gid and (not gid.isascii() or not gid.isdigit() or len(gid) > 9):
        raise ValueError('Укажите числовой GID группы последовательных портов')
    data_dir = settings['GATEBOT_DATA_DIR']
    container_data = mount_path(data_dir) if data_dir.startswith('/') else '/state'
    lines = ['services:', '  gatebot:', f'    image: ghcr.io/zhalenik/gate_bot:{version}',
             '    pull_policy: always', '    restart: unless-stopped', '    init: true',
             '    stop_grace_period: 120s', '    user: "568:568"', '    env_file:',
             '      - '+quote(root+'/gatebot.env'), '    environment:', '      HOST: "0.0.0.0"',
             '      PORT: '+quote(str(port)), '      GATEBOT_DATA_DIR: '+quote(container_data),
             '      GATEBOT_LOG_DIR: '+quote(container_data+'/logs'), '    ports:',
             '      - '+quote(f'{host_port}:{port}'), '    volumes:',
             '      - '+quote(root+'/state:'+container_data),
             '      - '+quote(root+'/system-config:/app/private/system-config'),
             '    read_only: true', '    tmpfs:', '      - /tmp:size=32m,mode=1777',
             '    cap_drop: [ALL]', '    security_opt: ["no-new-privileges:true"]',
             '    logging:', '      driver: json-file', '      options: {max-size: "10m", max-file: "3"}']
    if devices:
        lines.append('    devices:')
        for outside, inside in devices:
            lines.extend(['      - source: '+quote(outside), '        target: '+quote(inside)])
        if gid:
            lines.append('    group_add: ['+quote(gid)+']')
    if network:
        lines.extend(['    networks: [default, telegram_proxy]', 'networks:', '  telegram_proxy:',
                      '    external: true', '    name: '+quote(network)])
    return '\n'.join(lines)+'\n'
