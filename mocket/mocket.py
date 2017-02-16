# coding=utf-8
from __future__ import unicode_literals
import socket
import json
import os
import ssl
import io
import collections
import hashlib
import zlib
import gzip
from datetime import datetime, timedelta

import decorator

from .compat import (
    encode_utf8,
    decode_utf8,
    basestring,
    byte_type,
    text_type,
    FileNotFoundError,
    encoding,
    JSONDecodeError,
)

__all__ = (
    'true_socket',
    'true_create_connection',
    'true_gethostbyname',
    'true_gethostname',
    'true_getaddrinfo',
    'true_ssl_wrap_socket',
    'true_ssl_socket',
    'create_connection',
    'MocketSocket',
    'Mocket',
    'MocketEntry',
    'mocketize',
)

true_socket = socket.socket
true_create_connection = socket.create_connection
true_gethostbyname = socket.gethostbyname
true_gethostname = socket.gethostname
true_getaddrinfo = socket.getaddrinfo
true_ssl_wrap_socket = ssl.wrap_socket
true_ssl_socket = ssl.SSLSocket
try:
    true_ssl_context = ssl.SSLContext
except AttributeError:
    # Python 2.6
    true_ssl_context = None


class SuperFakeSSLContext(object):
    """ For Python 3.6 """
    class FakeSetter(int):
        def __set__(self, *args):
            pass
    options = FakeSetter()
    verify_mode = FakeSetter(ssl.CERT_OPTIONAL)


class FakeSSLContext(SuperFakeSSLContext):
    def __init__(self, sock=None, server_hostname=None, *args, **kwargs):
        if isinstance(sock, MocketSocket):
            self.sock = sock
            self.sock._host = server_hostname
            if true_ssl_context:
                self.sock.true_socket = true_ssl_socket(
                    sock=self.sock.true_socket,
                    server_hostname=server_hostname,
                    _context=true_ssl_context(
                        protocol=ssl.PROTOCOL_SSLv23,
                    )
                )
            else:  # Python 2.
                self.sock.true_socket = true_ssl_socket(
                    sock=self.sock.true_socket,
                )

    @staticmethod
    def load_default_certs(*args, **kwargs):
        pass

    @staticmethod
    def wrap_socket(sock, *args, **kwargs):
        return sock

    def __getattr__(self, name):
        return getattr(self.sock, name)


def create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
        s.settimeout(timeout)
    # if source_address:
    #     s.bind(source_address)
    s.connect(address)
    return s


class MocketSocket(object):
    family = None
    type = None
    proto = None
    _host = None

    def __init__(self, family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
        self.settimeout(socket._GLOBAL_DEFAULT_TIMEOUT)
        self.true_socket = true_socket(family, type, proto)
        self.fd = io.BytesIO()
        self._closed = True
        self._connected = False
        self._buflen = 1024
        self._entry = None
        self.family = family
        self.type = type
        self.proto = proto
        self._truesocket_recording_dir = None

    def gettimeout(self):
        return self.timeout

    def setsockopt(self, family, type, proto):
        self.family = family
        self.type = type
        self.proto = proto

        if self.true_socket:
            self.true_socket.setsockopt(family, type, proto)

    def settimeout(self, timeout):
        try:
            self.timeout = timeout
        except AttributeError:
            pass

    def getpeername(self):
        return self._address

    def getpeercert(self, *args, **kwargs):
        if not self._host:
            self._host, _ = self._address
        now = datetime.now()
        shift = now + timedelta(days=30 * 12)
        return {
            'notAfter': shift.strftime('%b %d %H:%M:%S GMT'),
            'subjectAltName': (
                ('DNS', '*%s' % self._host),
                ('DNS', self._host),
                ('DNS', '*'),
            ),
            'subject': (
                (
                    ('organizationName', '*.%s' % self._host),
                ),
                (
                    ('organizationalUnitName',
                     'Domain Control Validated'),
                ),
                (
                    ('commonName', '*.%s' % self._host),
                ),
            ),
        }

    def fileno(self):
        if self.true_socket:
            return self.true_socket.fileno()
        return self.fd.fileno()

    def connect(self, address):
        self._address = self._host, self._port = address
        self._closed = False

    def close(self):
        if self.true_socket and self._connected:
            self.true_socket.close()
        self._closed = True

    def makefile(self, mode='r', bufsize=-1):
        self._mode = mode
        self._bufsize = bufsize
        return self.fd

    def get_entry(self, data):
        return Mocket.get_entry(self._host, self._port, data)

    def sendall(self, data, *args, **kwargs):
        entry = self.get_entry(data)
        if not entry:
            return self.true_sendall(data, *args, **kwargs)
        entry.collect(data)
        self.fd.seek(0)
        self.fd.write(entry.get_response())
        self.fd.seek(0)

    def recv(self, buffersize, flags=None):
        resp = self.fd.readline(buffersize)
        return resp

    def _connect(self):  # pragma: no cover
        if not self._connected:
            print(self.true_socket)
            self.true_socket.connect(self._address)
            self._connected = True

    def true_sendall(self, data, *args, **kwargs):
        req = decode_utf8(data)
        # make request unique again
        req_signature = hashlib.md5(encode_utf8(''.join(sorted(req.split('\r\n'))))).hexdigest()
        # port should be always a string
        port = text_type(self._port)

        responses = {self._host: {port: {}}}
        if Mocket.get_truesocket_recording_dir():
            path = os.path.join(
                Mocket.get_truesocket_recording_dir(),
                Mocket.get_namespace() + '.json',
            )
            # check if there's already a recorded session dumped to a JSON file
            try:
                with io.open(path) as f:
                    responses.update(json.load(f))
            # if not, create a new dictionary
            except (FileNotFoundError, JSONDecodeError, KeyError):
                pass

        response_dict = responses[self._host][port]

        # try to get the response from the dictionary
        try:
            lines = response_dict[req_signature]['response']
            gzipped_lines = response_dict[req_signature]['gzip']
            r_lines = []
            for line_no, line in enumerate(lines):
                line = encode_utf8(line)
                if line_no + 1 in gzipped_lines:
                    gzip_buffer = io.BytesIO()
                    gzip_file = gzip.GzipFile(mode='wb', fileobj=gzip_buffer)
                    gzip_file.write(line)
                    gzip_file.close()
                    line = gzip_buffer.getvalue()
                    # line = zlib.compress(line)
                r_lines.append(line)
            encoded_response = b'\r\n'.join(r_lines)
            written = len(encoded_response)

        # if not available, call the real sendall
        except KeyError:
            self._connect()
            self.true_socket.sendall(data, *args, **kwargs)
            written, r = 0, io.BytesIO()
            while True:
                recv = self.true_socket.recv(self._buflen)
                r.write(recv)
                written += len(recv)
                if len(recv) < self._buflen:
                    break

            response_dict[req_signature] = dict(request=req)
            lines = response_dict[req_signature]['response'] = []
            gzipped_lines = response_dict[req_signature]['gzip'] = []

            # update the dictionary with the response obtained
            encoded_response = r.getvalue()

            for line_no, line in enumerate(encoded_response.split(b'\r\n')):
                try:
                    line = decode_utf8(line)
                except UnicodeDecodeError:
                    line = decode_utf8(zlib.decompress(line, 16 + zlib.MAX_WBITS))
                    gzipped_lines.append(line_no + 1)

                lines.append(line)

            # dump the resulting dictionary to a JSON file
            if self._truesocket_recording_dir:
                with io.open(path, mode='w', encoding=encoding) as f:
                    f.write(decode_utf8(json.dumps(responses)))

        # write the response to the mocket socket
        self.fd.write(encoded_response)
        # flush the mocket socket
        self.fd.seek(- written, 1)

    def send(self, data, *args, **kwargs):  # pragma: no cover
        entry = self.get_entry(data)
        if entry:
            if self._entry != entry:
                self.sendall(data, *args, **kwargs)
        self._entry = entry
        return len(data)

    def __getattr__(self, name):
        # useful when clients call methods on real
        # socket we do not provide on the fake one
        return getattr(self.true_socket, name)  # pragma: no cover


class RecordingMocketSocket(MocketSocket):
    def __init__(self, *args, **kwargs):
        super(RecordingMocketSocket, self).__init__(*args, **kwargs)
        self._truesocket_recording_dir = True


class Mocket(object):
    _entries = collections.defaultdict(list)
    _requests = []
    _namespace = text_type(id(_entries))
    _truesocket_recording_dir = None

    @classmethod
    def register(cls, *entries):
        for entry in entries:
            cls._entries[entry.location].append(entry)

    @classmethod
    def get_entry(cls, host, port, data):
        entries = cls._entries.get((host, port), [])
        for entry in entries:
            if entry.can_handle(data):
                return entry

    @classmethod
    def collect(cls, data):
        cls._requests.append(data)

    @classmethod
    def reset(cls):
        cls._entries = collections.defaultdict(list)
        cls._requests = []

    @classmethod
    def last_request(cls):
        if cls._requests:
            return cls._requests[-1]

    @classmethod
    def remove_last_request(cls):
        if cls._requests:
            del cls._requests[-1]

    @staticmethod
    def enable(namespace=None, truesocket_recording_dir=None):
        if namespace:
            Mocket._namespace = namespace
        if truesocket_recording_dir:
            # JSON dumps will be saved here
            assert os.path.isdir(truesocket_recording_dir)
            Mocket._truesocket_recording_dir = truesocket_recording_dir

            socket.socket = socket.__dict__['socket'] = RecordingMocketSocket
            socket._socketobject = socket.__dict__['_socketobject'] = RecordingMocketSocket
            socket.SocketType = socket.__dict__['SocketType'] = RecordingMocketSocket
            ssl.SSLSocket = ssl.__dict__['SSLSocket'] = RecordingMocketSocket
        else:
            socket.socket = socket.__dict__['socket'] = MocketSocket
            socket._socketobject = socket.__dict__['_socketobject'] = MocketSocket
            socket.SocketType = socket.__dict__['SocketType'] = MocketSocket
            ssl.SSLSocket = ssl.__dict__['SSLSocket'] = MocketSocket

        socket.create_connection = socket.__dict__['create_connection'] = create_connection
        socket.gethostname = socket.__dict__['gethostname'] = lambda: 'localhost'
        socket.gethostbyname = socket.__dict__['gethostbyname'] = lambda host: '127.0.0.1'
        socket.getaddrinfo = socket.__dict__['getaddrinfo'] = \
            lambda host, port, family=None, socktype=None, proto=None, flags=None: [(2, 1, 6, '', (host, port))]
        socket.inet_aton = socket.__dict__['inet_aton'] = socket.gethostbyname
        ssl.wrap_socket = ssl.__dict__['wrap_socket'] = FakeSSLContext.wrap_socket
        ssl.SSLContext = ssl.__dict__['SSLSocket'] = FakeSSLContext

    @staticmethod
    def disable():
        socket.socket = socket.__dict__['socket'] = true_socket
        socket._socketobject = socket.__dict__['_socketobject'] = true_socket
        socket.SocketType = socket.__dict__['SocketType'] = true_socket
        socket.create_connection = socket.__dict__['create_connection'] = true_create_connection
        socket.gethostname = socket.__dict__['gethostname'] = true_gethostname
        socket.gethostbyname = socket.__dict__['gethostbyname'] = true_gethostbyname
        socket.getaddrinfo = socket.__dict__['getaddrinfo'] = true_getaddrinfo
        socket.inet_aton = socket.__dict__['inet_aton'] = true_gethostbyname
        ssl.wrap_socket = ssl.__dict__['SSLSocket'] = true_ssl_wrap_socket
        ssl.SSLSocket = ssl.__dict__['wrap_socket'] = true_ssl_socket
        ssl.SSLContext = ssl.__dict__['SSLSocket'] = true_ssl_context

    @classmethod
    def get_namespace(cls):
        return cls._namespace

    @classmethod
    def get_truesocket_recording_dir(cls):
        return cls._truesocket_recording_dir


class MocketEntry(object):

    class Response(byte_type):
        @property
        def data(self):
            return self

    request_cls = str
    response_cls = Response

    def __init__(self, location, responses):
        self.location = location
        self.response_index = 0

        if not isinstance(responses, collections.Iterable) or isinstance(responses, basestring):
            responses = [responses]

        lresponses = []
        for r in responses:
            if not getattr(r, 'data', False):
                if isinstance(r, text_type):
                    r = encode_utf8(r)
                r = self.response_cls(r)
            lresponses.append(r)
        else:
            if not responses:
                lresponses = [self.response_cls(encode_utf8(''))]
        self.responses = lresponses

    def can_handle(self, data):
        return True

    def collect(self, data):
        req = self.request_cls(data)
        Mocket.collect(req)

    def get_response(self):
        response = self.responses[self.response_index]
        if self.response_index < len(self.responses) - 1:
            self.response_index += 1
        return response.data


class Mocketizer(object):
    def __init__(self, instance, namespace=None, truesocket_recording_dir=None):
        self.instance = instance
        self.truesocket_recording_dir = truesocket_recording_dir
        self.namespace = namespace or text_type(id(self))

    def __enter__(self):
        Mocket.enable(namespace=self.namespace, truesocket_recording_dir=self.truesocket_recording_dir)
        self.check_and_call('mocketize_setup')

    def __exit__(self, type, value, tb):
        self.check_and_call('mocketize_teardown')
        Mocket.disable()
        Mocket.reset()

    def check_and_call(self, method):
        method = getattr(self.instance, method, None)
        if callable(method):
            method()

    @staticmethod
    def wrap(test=None, truesocket_recording_dir=None):
        def wrapper(t, *args, **kw):
            instance = args[0] if args else None
            namespace = '.'.join((instance.__class__.__module__, instance.__class__.__name__, t.__name__))
            with Mocketizer(instance, namespace=namespace, truesocket_recording_dir=truesocket_recording_dir):
                t(*args, **kw)
            return wrapper
        return decorator.decorator(wrapper, test)


mocketize = Mocketizer.wrap
