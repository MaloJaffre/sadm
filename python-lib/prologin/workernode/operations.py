# This file is part of Prologin-SADM.
#
# Copyright (c) 2013-2015 Antoine Pietri <antoine.pietri@prologin.org>
# Copyright (c) 2011 Pierre Bourdon <pierre.bourdon@prologin.org>
# Copyright (c) 2011-2014 Association Prologin <info@prologin.org>
#
# Prologin-SADM is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prologin-SADM is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prologin-SADM.  If not, see <http://www.gnu.org/licenses/>.

import asyncio
import gzip
import itertools
import logging
import os
import os.path
import subprocess
import tarfile
import tempfile

from base64 import b64decode

ioloop = asyncio.get_event_loop()

def tar(path, compression='gz'):
    with tempfile.NamedTemporaryFile() as temp:
        with tarfile.open(fileobj=temp, mode='w:' + compression) as tar:
            tar.add(path)
        temp.flush()
        temp.seek(0)
        return temp.read()


def untar(content, path, compression='gz'):
    with tempfile.NamedTemporaryFile() as temp:
        temp.write(content)
        temp.seek(0)
        with tarfile.open(fileobj=temp, mode='r:' + compression) as tar:
            tar.extractall(path)


def create_file_opts(file_opts):
    opts = []
    files = []
    for l, content in file_opts.items():
        f = tempfile.NamedTemporaryFile()
        f.write(b64decode(content))
        f.flush()
        opts.append(l)
        opts.append(f.name)
        files.append(f)
    return opts, files


def gen_opts(opts):
    return list(itertools.chain(opts.items()))


@asyncio.coroutine
def communicate_forever(cmdline, *, data=None, env=None, max_len=None,
        truncate_message='', **kwargs):
    proc = yield from asyncio.create_subprocess_exec(*cmdline, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, **kwargs)

    # Send stdin
    if data:
        proc.stdin.write(data.encode())
        yield from proc.stdin.drain()
        proc.stdin.close()

    # Receive stdout
    stdout = bytearray()
    while True:
        to_read = 4096
        if max_len is not None:
            to_read = min(to_read, max_len - len(stdout))
            if not to_read:
                break
        chunk = yield from proc.stdout.read(to_read)
        if not chunk:
            break
        stdout.extend(chunk)

    if not to_read:
        stdout += truncate_message.encode()

    exitcode = yield from proc.wait()
    return (exitcode, stdout)


@asyncio.coroutine
def communicate(cmdline, *args, timeout=None, **kwargs):
    return (yield from asyncio.wait_for(
        communicate_forever(cmdline, *args, **kwargs), timeout))


@asyncio.coroutine
def compile_champion(config, champion_path):
    """
    Compiles the champion at $champion_path/champion.tgz to
    $champion_path/champion-compiled.tar.gz

    Returns a tuple (ok, output), with ok = True/False and output being the
    output of the compilation script.
    """
    cmd = [config['path']['compile_script'], config['path']['makefiles'],
           champion_path]
    retcode, _ = yield from communicate(cmd)
    return retcode == 0


@asyncio.coroutine
def spawn_server(config, rep_addr, pub_addr, nb_players, opts, file_opts):
    cmd = [config['path']['stechec_server'],
            "--rules", config['path']['rules'],
            "--rep_addr", rep_addr,
            "--pub_addr", pub_addr,
            "--nb_clients", str(nb_players + 1),
            "--time", "3000",
            "--socket_timeout", "45000",
            "--verbose", "1"]

    if opts is not None:
        cmd += gen_opts(opts)
    if file_opts is not None:
        fopts, tmp_files = create_file_opts(file_opts)
        cmd.extend(fopts)

    try:
        retcode, stdout = yield from communicate(cmd,
                timeout=config['timeout'].get('server', 400))
    except asyncio.TimeoutError:
        logging.error("Server timeout")
        return "workernode: Server timeout"

    stdout = stdout.decode()
    if retcode != 0:
        logging.error(stdout.strip())
    return stdout


@asyncio.coroutine
def spawn_dumper(config, rep_addr, pub_addr, opts, file_opts, order_id=None):
    if 'dumper' not in config['path'] or not config['path']['dumper']:
        return

    if not os.path.exists(config['path']['dumper']):
        raise FileNotFoundError(config['path']['dumper'] + ' not found.')

    cmd = [config['path']['stechec_client'],
        "--name", "dumper",
        "--rules", config['path']['rules'],
        "--champion", config['path']['dumper'],
        "--req_addr", rep_addr,
        "--sub_addr", pub_addr,
        "--memory", "250000",
        "--time", "3000",
        "--socket_timeout", "45000",
        "--spectator",
        "--verbose", "1"]
    cmd += ["--client_id", str(order_id)] if order_id is not None else []

    if opts is not None:
        cmd += gen_opts(opts)
    if file_opts is not None:
        fopts, tmp_files = create_file_opts(file_opts)
        cmd.extend(fopts)

    with tempfile.NamedTemporaryFile() as dump:
        new_env = os.environ.copy()
        new_env['DUMP_PATH'] = dump.name
        try:
            retcode, _ = yield from communicate(cmd, env=new_env,
                    timeout=config['timeout'].get('dumper', 400))
        except asyncio.TimeoutError:
            logging.error("dumper timeout")
        # even after a timeout, a dump might be available (at worse it's empty)
        gzdump = yield from ioloop.run_in_executor(None,
                gzip.compress, dump.read())
    return gzdump


@asyncio.coroutine
def spawn_client(config, req_addr, sub_addr, pl_id, champion_path, opts,
                 file_opts=None, order_id=None):
    env = os.environ.copy()
    env['CHAMPION_PATH'] = champion_path + '/'

    cmd = [config['path']['stechec_client'],
                "--name", str(pl_id),
                "--rules", config['path']['rules'],
                "--champion", champion_path + '/champion.so',
                "--req_addr", req_addr,
                "--sub_addr", sub_addr,
                "--memory", "250000",
                "--socket_timeout", "45000",
                "--time", "1500",
                "--verbose", "1",
        ]
    cmd += ["--client_id", str(order_id)] if order_id is not None else []

    if opts is not None:
        cmd += gen_opts(opts)
    if file_opts is not None:
        fopts, tmp_files = create_file_opts(file_opts)
        cmd.extend(fopts)

    try:
        retcode, stdout = yield from communicate(cmd, env=env, max_len=2 ** 18,
                truncate_message='\n\nLog truncated to stay below 256K.\n',
                timeout=config['timeout'].get('client', 400))
    except asyncio.TimeoutError:
        logging.error("client timeout")
        return 1, b"workernode: Client timeout"
    return retcode, stdout
