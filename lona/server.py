from __future__ import annotations

from concurrent.futures import Future
from collections.abc import Awaitable
from typing import overload, TypeVar
from functools import reduce
import contextlib
import operator
import logging
import inspect
import asyncio
import os

from aiohttp.web import WebSocketResponse, FileResponse, HTTPFound, Response
from typing_extensions import Literal
from aiohttp import WSMsgType

from lona.view_runtime_controller import ViewRuntimeController
from lona.middleware_controller import MiddlewareController
from lona.client_pre_compiler import ClientPreCompiler
from lona.static_file_loader import StaticFileLoader
from lona.response_parser import ResponseParser
from lona.templating import TemplatingEngine
from lona.imports import acquire as _acquire
from lona.server_state import ServerState
from lona.view_loader import ViewLoader
from lona.connection import Connection
from lona.settings import Settings
from lona.protocol import METHOD
from lona.routing import Router
from lona.view import LonaView

DEFAULT_SETTINGS = os.path.join(
    os.path.dirname(__file__),
    'default_settings.py',
)

server_logger = logging.getLogger('lona.server')
http_logger = logging.getLogger('lona.server.http')
websockets_logger = logging.getLogger('lona.server.websockets')

T = TypeVar('T')


class LonaServer:
    def __init__(self, app, project_root, settings_paths=None,
                 settings_pre_overrides=None, settings_post_overrides=None,
                 routes=None):

        self._project_root = os.path.abspath(project_root)

        self._websocket_connections = []
        self._loop = None
        self._worker_pool = None

        server_logger.debug("starting server in '%s'", project_root)

        # setup aiohttp app
        self._app = app
        self._app['lona_server'] = self

        self._app.on_startup.append(self._start)
        self._app.on_shutdown.append(self._stop)

        # setup settings
        server_logger.debug('setup settings')

        self._settings_paths = [
            DEFAULT_SETTINGS,
        ]

        for path in settings_paths or []:
            self._settings_paths.append(
                os.path.normpath(
                    os.path.join(self.project_root, path),
                ),
            )

        self.settings = Settings()

        if settings_pre_overrides:
            server_logger.debug('applying settings pre overrides')

            self.settings.update(settings_pre_overrides)

        for import_string in self._settings_paths:
            server_logger.debug("loading settings from '%s'", import_string)

            self.settings.add(import_string)

        if settings_post_overrides:
            server_logger.debug('applying settings post overrides')

            self.settings.update(settings_post_overrides)

        # setup templating
        server_logger.debug('setup templating')

        self._templating_engine = TemplatingEngine(self)

        # setup server state
        server_logger.debug('setup server state')

        self._state = ServerState(initial_data={})

        # setup routing
        server_logger.debug('setup routing')
        self._router = Router()

        if not routes:
            server_logger.debug("loading routing table from '%s'",
                                self.settings.ROUTING_TABLE)

            routes = self.acquire(self.settings.ROUTING_TABLE)

        if routes:
            self._router.add_routes(*routes)

        else:
            server_logger.warning('routing table is empty')

        # setup middleware controller
        server_logger.debug('setup middleware controller')

        self._middleware_controller = MiddlewareController(self)

        self._app.on_startup.append(
            self._middleware_controller.run_on_startup,
        )

        self._app.on_shutdown.append(
            self._middleware_controller.run_on_shutdown,
        )

        # setup aiohttp routes
        server_logger.debug('setup aiohttp routing')

        static_url = self.settings.STATIC_URL_PREFIX + '{path:.*}'

        server_logger.debug('static url set to %r', static_url)

        self._app.router.add_route(
            '*', static_url, self._handle_static_file_request)

        self._app.router.add_route(
            '*', '/{path_info:.*}', self._handle_http_request)

        # setup view loader
        server_logger.debug('setup view loader')

        self._view_loader = ViewLoader(self)

        # setup response parser
        server_logger.debug('setup response parser')

        self._response_parser = ResponseParser(self)

        # setup views
        server_logger.debug('setup view runtime controller')

        self._view_runtime_controller = ViewRuntimeController(self)

        # setup static files
        self._client_pre_compiler: ClientPreCompiler = ClientPreCompiler(self)

        # the static file loader has to be started last because it does
        # node class discovery which has to happen after all views are imported
        server_logger.debug('setup static file')

        self._static_file_loader = StaticFileLoader(self)

        # finish
        server_logger.debug('setup finish')

    # properties ##############################################################
    @property
    def project_root(self):
        return self._project_root

    @property
    def loop(self):
        return self._loop

    @property
    def worker_pool(self):
        return self._worker_pool

    @property
    def settings_paths(self):
        return tuple(self._settings_paths)

    @property
    def state(self):
        return self._state

    # template dirs
    @property
    def template_dirs(self):
        return tuple(self._templating_engine.template_dirs)

    @template_dirs.setter
    def template_dirs(self, new_value):
        self._templating_engine.template_dirs = new_value

    # static dirs
    @property
    def static_dirs(self):
        return tuple(self._static_file_loader.static_dirs)

    @static_dirs.setter
    def static_dirs(self, new_value):
        self._static_file_loader.static_dirs = new_value

    # start and stop ##########################################################
    async def _start(self, *args, **kwargs):
        server_logger.debug('start')

        if not self._loop:
            raise RuntimeError('loop is not set')

        if not self._worker_pool:
            raise RuntimeError('worker_pool is not set')

        # run checks
        # tests
        test_names = [
            'TEST_VIEW_START_TIMEOUT',
            'TEST_INPUT_EVENT_TIMEOUT',
        ]

        for test_name in test_names:
            if self.settings.get(test_name, False):
                server_logger.warning('%s is enabled', test_name)

        self._view_runtime_controller.start()

    async def _stop(self, *args, **kwargs):
        server_logger.debug('stop')

        await self.run_function_async(self._view_runtime_controller.stop)

        for connection in self._websocket_connections.copy():
            with contextlib.suppress(Exception):
                await connection.websocket.close()

    # connection management ###################################################
    async def _setup_connection(self, http_request, websocket=None):
        connection = Connection(self, http_request, websocket)

        handled, data, middleware = \
            await self._middleware_controller.handle_connection(
                connection,
            )

        if websocket is not None:
            self._websocket_connections.append(connection)

        return connection, (handled, data, middleware)

    def _remove_connection(self, connection):
        self._view_runtime_controller.remove_connection(connection)

        if connection in self._websocket_connections:
            self._websocket_connections.remove(connection)

    # view helper #############################################################
    def _render_response(self, response_dict):
        if response_dict['file']:
            return FileResponse(response_dict['file'])

        if response_dict['redirect']:
            return HTTPFound(response_dict['redirect'])

        if response_dict['http_redirect']:
            return HTTPFound(response_dict['http_redirect'])

        default_headers = {
            'Cache-Control': 'no-cache, no-store, must-revalidate',
        }

        headers = response_dict['headers'] or default_headers

        response = Response(
            status=response_dict['status'],
            content_type=response_dict['content_type'],
            text=response_dict['text'],
            body=response_dict['body'],
            headers=headers,
        )

        return response

    # handle http requests ####################################################
    async def _handle_static_file_request(self, request):
        def _404():
            return Response(
                status=404,
                text='404: Not found',
            )

        if not self.settings.STATIC_FILES_SERVE:
            server_logger.warning(
                'Reverse proxy seems to be misconfigured: a static file request was received but STATIC_FILES_SERVE is disabled',
            )

            return _404()

        rel_path = request.match_info['path']

        abs_path = await self.run_function_async(
            self._static_file_loader.resolve_path,
            rel_path,
            executor_name='static_worker',
        )

        if not abs_path:
            return _404()

        return FileResponse(abs_path)

    async def _handle_websocket_message(self, connection, message):
        websockets_logger.debug(
            '%s message received %s', connection, message.data)

        handled, data, middleware = \
            await self._middleware_controller.handle_websocket_message(
                connection,
                message.data,
            )

        if not handled:
            websockets_logger.debug(
                '%s message got not handled', connection)

    async def _handle_websocket_request(self, http_request):
        websocket = None
        connection = None

        async def close_websocket():
            await self.run_function_async(
                self._remove_connection,
                connection=connection,
            )

            await websocket.close()

            websockets_logger.debug('%s closed', connection)

        # setup websocket
        websocket = WebSocketResponse()
        await websocket.prepare(http_request)

        # setup connection
        connection, middleware_data = await self._setup_connection(
            http_request,
            websocket,
        )

        handled, data, middleware = middleware_data

        # connection got closed by middleware
        if handled:
            if data:
                if not isinstance(data, str):
                    raise RuntimeError(
                        f'{middleware}.handle_connection returned non string data',
                    )

                await connection.send_str(data, wait=False)

            await close_websocket()

            return websocket

        websockets_logger.debug('%s opened', connection)

        # main loop
        try:
            async for message in websocket:
                if message.type == WSMsgType.TEXT:
                    await self._handle_websocket_message(connection, message)

                elif message.type == WSMsgType.PING:
                    await websocket.pong()

                elif message.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                    break

        except asyncio.CancelledError:
            websockets_logger.debug('CancelledError')

        finally:
            await close_websocket()

        return websocket

    async def _handle_http_request(self, http_request):
        http_logger.debug('http request incoming')

        # resolve path
        http_logger.debug('resolving path %r', http_request.path)

        match, route, match_info = await self.run_function_async(
            self._router.resolve,
            http_request.path,
        )

        if match:
            http_logger.debug('route %s matched', route)

        else:
            http_logger.debug('no route matched')

        # http pass through ###################################################
        if match and route.http_pass_through:
            http_logger.debug('http_pass_through mode')

            # load view
            view = self._view_loader.load(route.view)

            if inspect.isclass(view):
                view = view(
                    server=self,
                    view_runtime=None,
                    request=http_request,
                ).handle_request

            # run view
            if asyncio.iscoroutinefunction(view):
                response = await view(http_request)

            else:
                response = await self.run_function_async(
                    view,
                    http_request,
                    executor_name='runtime_worker',
                )

            if asyncio.iscoroutine(response):
                response = await response

            # render and return response
            if isinstance(response, dict):
                return await self.run_function_async(
                    self._render_response,
                    response,
                )

            return response

        # websocket requests ##################################################
        if(http_request.method == 'GET' and
           http_request.headers.get('upgrade', '').lower() == 'websocket'):

            return await self._handle_websocket_request(http_request)

        # non interactive view or frontend ####################################
        # setup connection
        try:
            connection, middleware_data = await self._setup_connection(
                http_request,
            )

        except Exception:
            http_logger.exception(
                'Exception occurred while setting connection up',
            )

            return Response(status=500, body='500: Internal Error')

        handled, data, middleware = middleware_data

        # connection got closed by middleware
        if handled:
            if data is not None:
                if isinstance(data, dict):
                    return self._render_response(data)

                return data

            return Response(status=503, body='503: Service Unavailable')

        post_data = await http_request.post()

        response_dict = await self.run_function_async(
            self._view_runtime_controller.handle_view_message,
            connection=connection,
            window_id=None,
            view_runtime_id=None,
            method=METHOD.VIEW,
            payload=[str(http_request.url), post_data],
            executor_name='runtime_worker',
        )

        return self._render_response(response_dict)

    # public api ##############################################################
    @overload
    def run_coroutine_sync(
            self,
            coroutine: Awaitable[T],
            wait: None | Literal[True] = True,
    ) -> T:
        ...

    @overload
    def run_coroutine_sync(
            self,
            coroutine: Awaitable[T],
            wait: Literal[False],
    ) -> Future[T]:
        ...

    def run_coroutine_sync(
            self,
            coroutine: Awaitable[T],
            wait: None | bool = True,
    ) -> Future[T] | T:
        future = asyncio.run_coroutine_threadsafe(coroutine, loop=self._loop)

        if wait:
            return future.result()

        return future

    def run_function_async(self, function, *args,
                           executor_name='worker', **kwargs):

        def _function():
            return function(*args, **kwargs)

        return self._loop.run_in_executor(
            self._worker_pool.get_executor(executor_name),
            _function,
        )

    def resolve_path(self, path):
        if path.startswith('/'):
            return path

        return os.path.normpath(os.path.join(self.project_root, path))

    def acquire(self, import_string, *args, **kwargs):
        if '::' in import_string:
            script, attribute_name = import_string.split('::')
            script = self.resolve_path(script)

            import_string = f'{script}::{attribute_name}'

        return _acquire(import_string, *args, **kwargs)

    def get_running_views_count(self, *args, **kwargs):
        return self._view_runtime_controller.get_running_views_count(
            *args,
            **kwargs,
        )

    def view_is_already_running(self, request):
        view_runtime = self._view_runtime_controller.get_running_view_runtime(
            user=request.user,
            route=request.route,
            match_info=request.match_info,
        )

        return bool(view_runtime)

    def get_connection_count(self, user):
        count = 0

        for connection in self._websocket_connections.copy():
            if connection.user == user:
                count += 1

        return count

    def get_connected_user_count(self):
        user = []

        def add_user(new_user):
            for _user in user:
                if new_user == _user:
                    return

            user.append(new_user)

        for connection in self._websocket_connections.copy():
            add_user(connection.user)

        return len(user)

    def get_template(self, *args, **kwargs):
        return self._templating_engine.get_template(*args, **kwargs)

    def render_string(self, *args, **kwargs):
        return self._templating_engine.render_string(*args, **kwargs)

    def render_template(self, *args, **kwargs):
        return self._templating_engine.render_template(*args, **kwargs)

    def get_view_class(self, route=None, import_string=None, url=None):
        args = [bool(route), bool(import_string), bool(url)]

        if reduce(operator.xor, args, True):
            raise ValueError('too many or too few arguments given')

        if route:
            return self._view_loader.load(route.view)

        if import_string:
            return self._view_loader.load(import_string)

        if url:
            success, route, match_info = self._router.resolve(url)

            if not success:
                return None

            return self._view_loader.load(route.view)

    def reverse(self, *args, **kwargs):
        return self._router.reverse(*args, **kwargs)

    def fire_view_event(
            self,
            name: str,
            data: dict | None = None,
            view_classes: type[LonaView] | list[type[LonaView]] | None = None,
    ) -> None:

        self._view_runtime_controller.fire_view_event(
            name=name,
            data=data,
            view_classes=view_classes,
        )
