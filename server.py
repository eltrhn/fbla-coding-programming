import asyncio
import os
import urllib
from concurrent.futures import ProcessPoolExecutor
from glob import glob
from inspect import cleandoc

import aiohttp
import aioredis
import asyncpg
import bcrypt
import sanic
import uvloop
import sanic_jwt as jwt
from sanic import Sanic

from backend import deco
from backend.typedef import Location, Role, MediaItem, MediaType, User
from backend.blueprints import bp

# make it go faster!
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
# Create a Sanic application for this file
app = Sanic('Booksy')
# These are for checking API access later. (See function force_angular())
app.safe_segments = ('?', '.html', '.css', '.js', '.ts', '/auth', 'auth/', 'api/', 'stock/', '/verify', '/register')
# "Blueprints", i.e. separate files containing endpoint info to
# avoid clogging up this main file.
# They can be found in ./backend/blueprints
app.blueprint(bp)
app.config.TESTING = False  # tells my backend to act like the real deal

app.rtoken_cache = {}  # To mitigate DB slowness
# Note that I don't have to worry myself about LRUing the above cache
# (to handle the users that just close their browser w/o logging out)
# because Heroku does the thing where it restarts processes after 24h
# -- so the 'cache' won't get a chance to accumulate in memory anyway


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
        query = '''SELECT pwhash FROM members WHERE lid = $1::bigint AND username = $2::text'''
        pwhash = await conn.fetchval(query, lid, username)
    bvalid = await app.aexec(app.ppe, bcrypt.checkpw, password, pwhash)
    if not all((username, password, pwhash, bvalid)):
        return False
    return await User.from_identifiers(username=username, lid=lid, app=rqst.app)


async def retrieve_user(rqst, payload, *args, **kwargs):
    """/auth/me"""
    return await User(payload.get('user_id'), rqst.app)


async def store_rtoken(user_id, refresh_token, *args, **kwargs):
    """/auth/refresh"""
    async with app.rd_pool.get() as conn:
        await conn.execute('set', user_id, refresh_token)
        await conn.execute('set', refresh_token, user_id)  # for retrieving user from refresh token
        app.rtoken_cache[refresh_token] = user_id
        app.rtoken_cache[user_id] = refresh_token


async def retrieve_rtoken(user_id, *args, **kwargs):
    """/auth/refresh"""
    try:
        return app.rtoken_cache[user_id]
    except KeyError:
        async with app.rd_pool.get() as conn:
            app.rtoken_cache[user_id] = await conn.execute('get', user_id)
            return app.rtoken_cache[user_id]


async def revoke_rtoken(user_id, *args, **kwargs):
    """/auth/logout"""
    async with app.rd_pool.get() as conn:
        await conn.execute('del', await conn.execute('get', user_id))  # delete refresh token first
        await conn.execute('del', user_id)
        del app.rtoken_cache[app.rtoken_cache[user_id]], app.rtoken_cache[user_id]


# Initialize with JSON Web Token (JWT) authentication for logins.
# First argument passed is the Sanic app object, and subsequent
# parameters are helper functions for authentication & security.
jwt.initialize(
  app,
  authenticate=authenticate,
  retrieve_user=retrieve_user,
  store_refresh_token=store_rtoken,
  retrieve_refresh_token=retrieve_rtoken,
  revoke_refresh_token=revoke_rtoken
  )

# Config variables for JWT authentication.
app.config.SANIC_JWT_REFRESH_TOKEN_ENABLED = True  # Use refresh tokens
app.config.SANIC_JWT_SECRET = os.environ['SANIC_JWT_SECRET']  # it's a secret to everybody!
app.config.SANIC_JWT_CLAIM_IAT = True  # for longer sessions
# Store token in cookies instead of making the client webapp send them
# ...this may also open things up for XSRF. but I don't know enough about
# that to be sure as to how to deal with or ameliorate it
app.config.SANIC_JWT_COOKIE_SET = True

# Get the filenames generated by Angular's AOT build
# (could probably slim this down, sorta just threw the stuff together until it worked)
olddir = os.getcwd()
os.chdir('/app/dist')
filenames = 'index.html', 'styles*.css', 'inline*.js', 'main*.js', 'polyfills*.js', 'scripts*.js'
relative = [glob(i) for i in filenames]
os.chdir(olddir)
absolute = [glob(i) for i in map('/app/dist/'.__add__, filenames)]
# Route user requests to Angular's files by redirecting, say,
# /api/whatever to /app/dist/api/whatever
for rel, absol in zip(relative, absolute):
    app.static(rel[0], absol[0])


@app.listener('before_server_start')
async def set_up_dbs(app, loop):
    """
    Establishes a connection to the environment's Postgres and Redis DBs
    for use in (first) authenticating and (then) storing refresh tokens.
    """
    app.session = aiohttp.ClientSession()
    app.sem = asyncio.Semaphore(4, loop=loop)  # limit concurrency of aiohttp requests to Google Books
    
    app.ppe = ProcessPoolExecutor(4)
    app.aexec = loop.run_in_executor
    
    app.pg_pool = await asyncpg.create_pool(dsn=os.getenv('DATABASE_URL'), max_size=15, loop=loop)
    app.acquire = app.pg_pool.acquire
    
    # The below line is necessary (as are the @staticmethod do_imports() methods
    # in each typedef class) because if the imports are done at the top of each
    # file, Python will die on attempting to resolve the circular dependencies.
    [i.do_imports() for i in [Location, Role, MediaType, MediaItem, User]]
    if os.getenv('REDIS_URL') is None:  # Means I'm testing (don't have Redis on home PC)
        app.config.SANIC_JWT_REFRESH_TOKEN_ENABLED = False
    else:
        app.rd_pool = await aioredis.create_pool(
          os.getenv('REDIS_URL'),
          minsize=5,
          maxsize=15,
          loop=loop
          )


@app.listener('before_server_stop')
async def close_dbs(app, loop):
    """
    Gracefully close all acquired connections before shutting off.
    """
    print('Shutting down.')
    await app.pg_pool.close()
    app.rd_pool.close()
    await app.rd_pool.wait_closed()
    await app.session.close()


@app.middleware('request')
async def force_angular(rqst):
    """
    Let through any requestes with URLs containing strings in `safe`,
    because this denotes API access; else redirect to Angular's files
    """
    if not any(i in rqst.url for i in app.safe_segments):
        try:
            url = rqst.url[3+rqst.url.find('://'):]
            path = urllib.parse.quote(url.split('/', 1)[1])
            return sanic.response.redirect(f'/index.html/?redirect={path}')
        except IndexError:
            return sanic.response.redirect('/index.html')


@app.middleware('response')
async def force_no_cache(rqst, resp):
    """
    This is ABSOLUTELY necessary because browsers will otherwise cache
    the sidebar buttons(which, of course, are supposed to be delivered
    by calculating the CURRENT user's permissions, not whomever an
    IP logged in as previously)
    """
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'


@app.route('/login')
async def login_refresh_fix(rqst):
    """
    Occasionally when you refresh on the login page it'll show an ugly
    error: URL /login not found. This fixes that.
    """
    return sanic.response.redirect('/index.html/?redirect=login')


@app.get('/verify')
@deco.rqst_get('token')
async def finalize_registration(rqst, token):
    return sanic.response.html(cleandoc(f'''
      <html><head><style>
      * {{ font-family: sans-serif; }}
      h1 {{ text-decoration: underline; }}
      h3 {{ font-weight: normal; margin-bottom: 5px; }}
      input {{
        margin-bottom: 1em;
        border-radius: 5px;
        border: 1px solid #bbb;
        padding: 10px;
      }}
      button {{
        background-color: #43ba2e;
        color: white;
        padding: 10px 10px;
        margin: auto;
        cursor: pointer;
        border: none;
        border-radius: 5px;
        padding: 15px;
        transition: opacity .2s;
      }}
      </style></head><body>
        <h1>Registering your library</h1>
        <p>You're almost done! Fill out the form below to finalize your registration.</p>
        <br/>
        <form method="post" action="../register" oninput="checkMatching()">
          <input name="token" type="hidden" value="{token}">
          <h3>Your admin password:</h3>
          <input type="password" id="adminpw" name="adminpw" placeholder="Admin account password"/>
          <input type="password" id="aconf" placeholder="Confirm admin password">
          <h3>Your library's self-checkout account's password:</h3>
          <input type="password" id="checkoutpw" name="checkoutpw" placeholder="Self-checkout account password"/>
          <input type="password" id="cconf" placeholder="Confirm checkout account password">
          <p>Make sure you keep these on hand!</p>
          <button id="sbmt" type="submit">Register</button>
        </form>
      </body><script>
      function checkMatching() {{
          var adminpw = document.getElementById("adminpw");
          var aconf = document.getElementById("aconf");
          var checkoutpw = document.getElementById("checkoutpw");
          var cconf = document.getElementById("cconf");
          var sbmt = document.getElementById("sbmt");
          
          if (aconf.value && aconf.value !== adminpw.value) {{
              aconf.style.backgroundColor = "#fef0ed";
              aconf.style.color = "#ff2929";
              aconf.style.borderColor = "#ff2929";
              sbmt.style.display = "none";
          }} else {{
              aconf.style.backgroundColor = "white";
              aconf.style.color = "black";
              aconf.style.borderColor = "#bbb";
              sbmt.style.display = (cconf.value && cconf.value !== checkoutpw.value)?"none":"";
          }}
          if (cconf.value && cconf.value !== checkoutpw.value) {{
              cconf.style.backgroundColor = "#fef0ed";
              cconf.style.color = "#ff2929";
              cconf.style.borderColor = "#ff2929";
              sbmt.style.display = "none";
          }} else {{
              cconf.style.backgroundColor = "white";
              cconf.style.color = "black";
              cconf.style.borderColor = "#bbb";
              sbmt.style.display = (aconf.value && aconf.value !== adminpw.value)?"none":"";
          }}
          if (!aconf.value || !cconf.value) {{
              sbmt.style.display = "none";
          }}
      }}
      </script></html>'''), status=200)


@app.post('/register')
@deco.rqst_get('token', 'adminpw', 'checkoutpw', form=True)
async def register_location(rqst, token, adminpw, checkoutpw):
    """Have to *unpack these attrs because rqst.form returns single-item lists."""
    locname, lid, chk_usr, admin_usr = await Location.instate(rqst, *token, *adminpw, *checkoutpw)
    return sanic.response.html(cleandoc('''
      <html><head></head><body>
      <p style="font-family:monospace;font-size:20px"><strong>
      ''')
      + cleandoc('''
      ╔════════════════════════════════════════════════════════╗<br/>
      ║ PLEASE SCREENSHOT OR OTHERWISE SAVE THIS PAGE!!!       ║<br/>
      ║                                                        ║<br/>
      ║ The information given here, particularly the location  ║<br/>
      ║ ID and self-checkout account's username, are vital to  ║<br/>
      ║ logging in — but they will not be displayed anywhere   ║<br/>
      ║ else.                                                  ║<br/>
      ╚════════════════════════════════════════════════════════╝
      </strong></p>
      ''').replace(' ', '&nbsp;')  # otherwise the third line gets messed up
    + cleandoc(f'''
      <p style="font-family:sans-serif">
      Thanks! Your new library, <b>{locname}</b>, has been registered, with location ID <b>{lid}</b>.
      <br/><br/>
      Your admin account's username is <b>{admin_usr}</b>, and you can log into it along with the above location ID to start adding members and media.
      <br/>
      Your library's self-checkout account's username is <b>{chk_usr}</b>.'''
      ''' Your patrons (once they're registered too!) can check out from it as a convenience method, without needing to log in to their full accounts.
      <br/><br/>
      Have fun!
      </p>
      </body></html>
      '''),
      status=200)

if __name__ == '__main__':
    # more than 1 worker and I get too many DB connections for heroku :((
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=False, access_log=False, workers=1)
