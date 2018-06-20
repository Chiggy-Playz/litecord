import asyncio
import sys

import asyncpg
import logbook
import websockets
from quart import Quart, g, jsonify
from logbook import StreamHandler, Logger

import config
from litecord.blueprints import gateway, auth
from litecord.gateway import websocket_handler
from litecord.errors import LitecordError
from litecord.gateway.state_manager import StateManager

# setup logbook
handler = StreamHandler(sys.stdout, level=logbook.INFO)
handler.push_application()
log = Logger('litecord.boot')


def make_app():
    app = Quart(__name__)
    app.config.from_object(f'config.{config.MODE}')

    if app.config['DEBUG']:
        handler.level = logbook.DEBUG

    return app


app = make_app()
app.register_blueprint(gateway, url_prefix='/api/v6')
app.register_blueprint(auth, url_prefix='/api/v6')


@app.before_serving
async def app_before_serving():
    log.info('opening db')
    app.db = await asyncpg.create_pool(**app.config['POSTGRES'])
    g.app = app

    app.loop = asyncio.get_event_loop()
    g.loop = asyncio.get_event_loop()

    app.state_manager = StateManager()

    # start the websocket, etc
    host, port = app.config['WS_HOST'], app.config['WS_PORT']
    log.info(f'starting websocket at {host} {port}')

    async def _wrapper(ws, url):
        # We wrap the main websocket_handler
        # so we can pass quart's app object.
        await websocket_handler(app.db, app.state_manager, ws, url)

    ws_future = websockets.serve(_wrapper, host, port)

    await ws_future


@app.after_serving
async def app_after_serving():
    log.info('closing db')
    await app.db.close()


@app.errorhandler(LitecordError)
async def handle_litecord_err(err):
    return jsonify({
        'error': True,
        # 'code': err.code,
        'status': err.status_code,
        'message': err.message,
    }), err.status_code


@app.errorhandler(Exception)
def handle_exception(err):
    log.exception('Error happened in the app')
    return jsonify({
        'error': True,
        'message': repr(err)
    }), 500


@app.route('/')
async def index():
    return 'hewwo'
