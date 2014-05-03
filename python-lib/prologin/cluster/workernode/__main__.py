# -*- encoding: utf-8 -*-
# This file is part of Prologin-SADM.
#
# Copyright (c) 2013-2014 Antoine Pietri <antoine.pietri@prologin.org>
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
import functools
import logging
import logging.handlers
import os.path
import optparse
import prologin.log
import prologin.config
import prologin.rpc.client
import prologin.rpc.server
import re
import socket
import tempfile
import time
import tornado
import tornado.platform.asyncio
import yaml

from base64 import b64decode, b64encode
from . import operations

ioloop = asyncio.get_event_loop()
tornado.platform.asyncio.AsyncIOMainLoop().install()


def async_work(func=None, slots=0):
    if func is None:
        return functools.partial(async_work, slots=slots)
    @functools.wraps(func)
    def mktask(self, *args, **kwargs):
        @asyncio.coroutine
        def wrapper(self, *wargs, **wkwargs):
            if self.slots < slots:
                logging.warn('not enough slots to start the required job')
            logging.debug('starting a job for {} slots'.format(slots))
            self.slots -= slots
            yield from self.update_master()
            try:
                r = yield from func(self, *wargs, **wkwargs)
            finally:
                self.slots += slots
                yield from self.update_master()
        asyncio.Task(wrapper(self, *args, **kwargs))
        return slots
    return mktask


class WorkerNode(prologin.rpc.server.BaseRPCApp):
    def __init__(self, *args, config=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = config
        self.interval = config['master']['heartbeat_secs']
        self.hostname = socket.gethostname()
        self.port = config['worker']['port']
        self.slots = self.max_slots = config['worker']['available_slots']
        self.min_srv_port = config['worker']['port_range_start']
        self.max_srv_port = config['worker']['port_range_end']
        self.srv_port = self.min_srv_port
        self.matches = {}
        asyncio.Task(self.send_heartbeat())
        self.master = self.get_master()

    def get_worker_infos(self):
        return (self.hostname, self.port, self.slots, self.max_slots)

    def get_master(self):
        config = self.config
        host, port = (config['master']['host'], config['master']['port'])
        url = "http://{}:{}/".format(host, port)
        return prologin.rpc.client.Client(url,
                secret=config['master']['shared_secret'].encode('utf-8'),
                async=True)

    @asyncio.coroutine
    def update_master(self):
        try:
            yield from self.master.update_worker(self.get_worker_infos())
        except socket.error:
            logging.warn('master down, cannot update it')

    @asyncio.coroutine
    def send_heartbeat(self):
        logging.info('sending heartbeat to the server, {}/{} slots'.format(
            self.slots, self.max_slots))
        first_heartbeat = True
        while True:
            try:
                yield from self.master.heartbeat(self.get_worker_infos(),
                                                 first_heartbeat)
                first_heartbeat = False
            except socket.error:
                logging.warn('master down, retrying heartbeat in {}s'.format(
                        self.interval))

            yield from asyncio.sleep(self.interval)

    @prologin.rpc.remote_method
    def available_server_port(self):
        """
        Be optimistic and hope that:
        - nobody will use the ports in the port range but us
        - there will never be more servers than ports in the range
        """
        port = self.srv_port
        self.srv_port += 1
        if self.srv_port > self.max_srv_port:
            self.srv_port = self.min_srv_port
        return port

    @prologin.rpc.remote_method
    @async_work(slots=1)
    def compile_champion(self, cid, ctgz):
        ctgz = b64decode(ctgz)

        with tempfile.TemporaryDirectory() as cpath:
            compiled_path = os.path.join(cpath, 'champion-compiled.tar.gz')
            log_path = os.path.join(cpath, 'compilation.log')

            yield from loop.run_in_executor(operations.untar, ctgz, cpath)
            ret = yield from operations.compile_champion(self.config, cpath)

            if not ret:
                compilation_content = b''
            else:
                with open(compiled_path, 'rb') as f:
                    compilation_content = f.read()
            with open(log_path, 'r') as f:
                log_content = f.read()

        b64compiled = b64encode(compilation_content).decode('ascii')
        b64log = b64encode(log_content).decode('ascii')

        try:
            yield from self.master.compilation_result(cid, b64compiled, b64log)
        except socket.error:
            logging.warning('master down, cannot send compiled {}'.format(
                cid))

    @prologin.rpc.remote_method
    @async_work(slots=1)
    def run_server(self, rep_port, pub_port, match_id, opts=''):
        logging.info('starting server for match {}'.format(match_id))

        task_server = asyncio.Task(operations.spawn_server(self.config,
            rep_port, pub_port, opts))
        task_dumper = asyncio.Task(operations.spawn_dumper(self.config,
            rep_port, pub_port, opts))

        yield from asyncio.wait([task_server, task_dumper])
        logging.info('match {} done'.format(match_id))

        server_stdout = task_server.result()
        dumper_stdout = task_dumper.result()

        lines = server_stdout.split('\n')
        result = []
        score_re = re.compile(r'^(\d+) (-?\d+) (-?\d+)$')
        for line in lines:
            m = score_re.match(line)
            if m is not None:
                pid, score, stat = m.groups()
                result.append((int(pid), int(score)))

        b64dump = b64encode(dumper_stdout).decode('ascii')

        try:
            yield from self.master.match_done(match_id, result, b64dump)
        except socket.error:
            logging.warning('master down, cannot send match {} result'.format(
                match_id))

    @prologin.rpc.remote_method
    @async_work(slots=2)
    def run_client(self, match_id, pl_id, ip, req_port, sub_port, ctgz, opts):
        ctgz = b64decode(ctgz)
        logging.info('running player {} for match {}'.format(pl_id, match_id))

        with tempfile.TemporaryDirectory() as cpath:
            yield from loop.run_in_executor(operations.untar, tgz, cpath)
            result = yield from operations.spawn_client(self.config, ip,
                    req_port, sub_port, pl_id, champion_path, opts)

        logging.info('player {} for match {} done'.format(pl_id, match_id))

        try:
            yield from self.master.client_done(match_id, pl_id, retcode)
        except socket.error:
            logging.warning('master down, cannot send client {} result '
                            'for match {}'.format(pl_id, match_id))


if __name__ == '__main__':
    parser = optparse.OptionParser()
    parser.add_option('-l', '--local-logging', action='store_true',
                      dest='local_logging', default=False,
                      help='Activate logging to stdout.')
    parser.add_option('-v', '--verbose', action='store_true',
                      dest='verbose', default=False,
                      help='Verbose mode.')
    options, args = parser.parse_args()

    prologin.log.setup_logging('workernode', verbose=options.verbose,
                               local=options.local_logging)
    logging.getLogger('asyncio').setLevel(logging.WARNING)

    config = prologin.config.load('workernode')

    s = WorkerNode(app_name='workernode', config=config,
                   secret=config['master']['shared_secret'].encode('utf-8'))

    logging.info('worker listening on {}'.format(config['worker']['port']))
    s.listen(config['worker']['port'])

    try:
        ioloop.run_forever()
    except KeyboardInterrupt:
        pass
