#
#    Copyright (c) 2014+ Anton Tyurin <noxiouz@yandex.ru>
#    Copyright (c) 2014+ Evgeny Safronov <division494@gmail.com>
#    Copyright (c) 2011-2014 Other contributors as noted in the AUTHORS file.
#
#    This file is part of Cocaine.
#
#    Cocaine is free software; you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as published
#    by the Free Software Foundation; either version 3 of the License, or
#    (at your option) any later version.
#
#    Cocaine is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#    GNU Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public License
#    along with this program. If not, see <http://www.gnu.org/licenses/>.
#
from __future__ import with_statement

import asyncio
import logging

import msgpack

from ..concurrent import Stream
from ..common import CocaineErrno
from ..asio.protocol import CocaineProtocol
from ..asio.message import RPC, Message
from ..asio.rpc import API

logging.basicConfig()
log = logging.getLogger("asyncio")
log.setLevel(logging.INFO)


class BaseService(object):
    def __init__(self, name, host='localhost', port=10053, loop=None):
        self.loop = loop or asyncio.get_event_loop()
        self._state_lock = asyncio.Lock()
        self.host = host
        self.port = port
        self.name = name
        # protocol
        self.pr = None
        # should I add connection epoch
        # Epoch is usefull when on_failure is called
        self.sessions = {}
        self.counter = 0

        self.api = {}

    def connected(self):
        return self.pr and self.pr.connected()

    @asyncio.coroutine
    def connect(self):
        # double state check
        if self.connected():
            log.debug("Connected")
            return

        log.debug("Disconnected")
        with (yield self._state_lock):
            if self.connected():
                return

            log.debug("Still disconnected")
            proto_factory = CocaineProtocol.factory(self.on_message,
                                                    self.on_failure)

            _, self.pr = yield self.loop.create_connection(proto_factory,
                                                           self.host,
                                                           self.port)

    def on_message(self, unpacked_data):
        msg = Message.initialize(unpacked_data)
        log.debug("type %d, session %d, chunk %s",
                  msg.id, msg.session, msg.args)

        stream = self.sessions.get(msg.session)
        if stream is None:
            log.error("Unknown session numder %d", msg.session)
            return

        # replace with constants and message.initializer
        if msg.id == RPC.CHUNK:
            stream.push(msgpack.unpackb(msg.data))
        elif msg.id == RPC.CHOKE:
            stream.done()
        elif msg.id == RPC.ERROR:
            stream.error(msg.errno, msg.reason)

    def on_failure(self, exc):
        log.warn("service %s is disconnected %s", self.name, exc)

        for stream in self.sessions.itervalues():
            stream.error(CocaineErrno.ESRVDISCON,
                         "service %s is disconnected" % self.name)

    @asyncio.coroutine
    def _invoke(self, method, *args):
        yield self.connect()
        method_id = self.api.get(method)

        if method_id is None:
            raise Exception("Method %s is not supported" % method)
        # make it thread-safe
        counter = self.counter
        self.counter += 1

        # send message
        self.pr.write(msgpack.packb([method_id, counter, args]))

        stream = Stream()
        self.sessions[counter] = stream
        raise asyncio.Return(stream)

    def __getattr__(self, name):
        log.debug("Method %s has been called", name)

        def on_getattr(*args):
            return self._invoke(name, *args)
        return on_getattr


class Locator(BaseService):
    def __init__(self, host="localhost", port=10053, loop=None):
        super(Locator, self).__init__(name="locator", host=host,
                                      port=port, loop=loop)
        self.api = API.Locator


class Service(BaseService):
    def __init__(self, name, host="localhost", port=10053,
                 loop=None, version=0):
        super(Service, self).__init__(name=name, loop=loop)
        self.locator = Locator(host=host, port=port, loop=loop)
        self.api = {}
        self.host = None
        self.port = None
        self.version = version

    @asyncio.coroutine
    def connect(self):
        if self.connected():
            log.debug("%s %d connected", self.name, id(self))
            return

        log.info("%s %d is connecting", self.name, id(self))
        f = yield self.locator.resolve(self.name)
        service_description = yield f.get()

        (self.host, self.port), version, self.api = service_description
        # version compatability should be checked here
        if self.version == 0 or version == self.version:
            self.api = dict((v, k) for k, v in self.api.iteritems())
        else:
            raise Exception("wrong service `%s` API version %d, %d is needed" %
                            (self.name, version, self.version))
        yield super(Service, self).connect()
