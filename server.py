import asyncio
import itertools
import os
import struct
from concurrent.futures import ProcessPoolExecutor
from glob import glob
from urllib import parse

import aiohttp
import aioredis
import asyncpg
import bcrypt
import sanic
import urllib
import uvloop
import sanic_jwt as jwt
from sanic_jwt import decorators as jwtdec
from sanic import Sanic

from backend import setup, deco
from backend.typedef import Location, Role, MediaItem, MediaType, User
from backend.blueprints import bp

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy()) # make it go faster <3

# Create a Sanic application for this file.
app = Sanic('Booksy')

# "Blueprints", i.e. separate files containing endpoint info to
# avoid clogging up this main file.
# They can be found in ./backend/blueprints
app.blueprint(bp)

async def authenticate(rqst, *args, **kwargs):
    """
    /auth
    Authenticate a user's credentials through sanic-jwt to give them
    access to the application.
    """
    try:
        # standard fare. get username and password from the app's request
        username = rqst.json['user_id'].lower()
        password = rqst.json['password'].encode('utf-8')
        lid = int(rqst.json['lid'])
    except KeyError:
        # this will always be handled client-side regardless, but...
        # ...just in case, I guess
        raise jwt.exceptions.AuthenticationFailed('Missing username or password.')
    # look up the username/pw pair in the database
    async with app.acquire() as conn:
        query = """SELECT pwhash FROM members WHERE lid = $1::bigint AND username = $2::text"""
        pwhash = await conn.fetchval(query, lid, username)
    bvalid = await app.aexec(app.ppe, bcrypt.checkpw, password, pwhash)
    if not any((username, password, pwhash, bvalid)):
            # (we shouldn't specify which of pw/username is invalid lest an attacker
            # use the info to enumerate possible passwords/usernames)
            return False # unverified
    return await User.from_identifiers(rqst.app, lid=lid, username=username)

async def retrieve_user(rqst, payload, *args, **kwargs):
    """/auth/me"""
    if payload:
        uid = payload.get('user_id', None)
        return await User(uid, rqst.app)
    else:
        return None

async def store_rtoken(user_id, refresh_token, *args, **kwargs):
    """/auth/refresh"""
    async with app.rd_pool.get() as conn:
        await conn.execute('set', user_id, refresh_token)

async def retrieve_rtoken(user_id, *args, **kwargs):
    """/auth/refresh"""
    async with app.rd_pool.get() as conn:
        return await conn.execute('get', user_id)

async def revoke_rtoken(user_id, *args, **kwargs):
    """/auth/logout"""
    async with app.rd_pool.get() as conn:
        return await conn.execute('del', user_id)

# Initialize with JSON Web Token (JWT) authentication for logins.
# First argument passed is the Sanic app object, and subsequent
# parameters are helper functions for authentication & security.
jwt.initialize(app,
  authenticate=authenticate,
  retrieve_user=retrieve_user, # could probably be a lambda but meh
  store_refresh_token=store_rtoken,
  retrieve_refresh_token=retrieve_rtoken,
  revoke_refresh_token=revoke_rtoken)

# Config variables for JWT authentication. See sanic-jwt docs on GitHub
# for more info, or the README.md on my own fork bc I added some stuff
app.config.SANIC_JWT_COOKIE_SET = True # Store token in cookies instead of making the client webapp send them
                                       # ...this may also open things up for XSRF. but I don't know enough about
                                       # that to be sure regarding how to deal with or ameliorate it
app.config.SANIC_JWT_REFRESH_TOKEN_ENABLED = True
app.config.SANIC_JWT_SECRET = os.getenv('SANIC_JWT_SECRET') # it's a secret to everybody!
app.config.SANIC_JWT_CLAIM_IAT = True # perhaps for long sessions
# app.config.SANIC_JWT_CLAIM_NBF = True # why not, more security
# app.config.SANIC_JWT_CLAIM_NBF_DELTA = 2 # token becomes checkable 2s after creation

# Get the filenames generated by Angular's AOT build
# (could probably slim this down, sort of just threw stuff together until it worked)
olddir = os.getcwd()
os.chdir('/app/dist')
filenames = ('index.html', 'styles*.css', 'inline*.js', 'main*.js', 'polyfills*.js')
relative = [glob(i) for i in filenames]
os.chdir(olddir)
absolute = [glob(i) for i in map('/app/dist/'.__add__, filenames)]
for rel, absol in zip(relative, absolute):
    app.static(rel[0], absol[0]) # Route user requests to Angular's files

@app.listener('before_server_start')
async def set_up_dbs(app, loop):
    """
    Establishes a connection to the environment's Postgres and Redis DBs
    for use in (first) authenticating and (then) storing refresh tokens.
    """
    app.session = aiohttp.ClientSession()
    app.sem = asyncio.Semaphore(4, loop=loop) # limit concurrency of aiohttp requests to Google Books
    app.filesem = asyncio.Semaphore(255, loop=loop) # limit concurrency of file reads without forcing one at a time
    
    app.ppe = ProcessPoolExecutor(4)
    app.aexec = loop.run_in_executor
    
    app.pg_pool = await asyncpg.create_pool(dsn=os.getenv('DATABASE_URL'), max_size=15, loop=loop)
    app.acquire = app.pg_pool.acquire
    async with app.acquire() as conn:
        await setup.create_pg_tables(conn)
    if os.getenv('REDIS_URL', None) is None: # can't do nothin bout this
        app.config.SANIC_JWT_REFRESH_TOKEN_ENABLED = False
    else:
        app.rd_pool = await aioredis.create_pool(
                      os.getenv('REDIS_URL'),
                      minsize=5,
                      maxsize=15,
                      loop=loop)

@app.listener('before_server_stop')
async def close_dbs(app, loop):
    """
    Sign off by gracefully closing the connection with the env's DBs and other acquired connections.
    """
    await app.pg_pool.close()
    app.rd_pool.close()
    await app.rd_pool.wait_closed()
    await app.session.close()
    print('Shutting down.')


@app.route('/')
async def handle_homepage(rqst):
    return sanic.response.redirect('/index.html')

@app.route('/<path:[^?]+>')
async def redirect_to_index(rqst, path):
    """
    A jury-rigged solution to the problem with Angular's PathLocationStrategy
    routing.
    The app won't load if you don't redirect to /index.html, but then
    /index.html doesn't match any routes within the app... so one has 
    to add a redirect router within the app from /index.html to the
    home component and then have said component redirect to whatever
    is listed in the ?redirect= parameter (a feat in itself!). Sort
    of shamefully proud of this.
    """
    return sanic.response.redirect(f'/index.html/?redirect={urllib.parse.quote(path)}')

# more than 1 worker and you get too many DB connections :((
app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=True, workers=1)
