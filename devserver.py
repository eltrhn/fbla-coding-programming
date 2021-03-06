"""
server.py but modified to work on my computer (my "development environment") so I can actually
test booksy
this should not be committed
"""
import asyncio
import os
from concurrent.futures import ProcessPoolExecutor
from glob import glob

import aiohttp
import aioredis
import asyncpg
import sanic
import urllib
import uvloop
import sanic_jwt as jwt
from sanic import Sanic

from backend import deco
from backend.typedef import Location, Role, MediaItem, MediaType, User
from backend.blueprints import bp

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())  # make it go faster <3

# Create a Sanic application for this file.
app = Sanic('Booksy')
# For checking API access later. (See function force_angular())
app.safe_segments = ('?', '.html', '.css', '.js', '.ts', '/auth', 'auth/', 'api/', 'stock/', '/verify', '/register')

# "Blueprints", i.e. separate files containing endpoint info to
# avoid clogging up this main file.
# They can be found in ./backend/blueprints
app.blueprint(bp)
app.config.TESTING = True

app.rtoken_cache = {}  # refresh-token dict


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
    bvalid = True  # await app.aexec(app.ppe, bcrypt.checkpw, password, pwhash)
    if False:
            # (we shouldn't specify which of pw/username is invalid lest an attacker
            # use the info to enumerate possible passwords/usernames)
            return False  # unverified
    return await User.from_identifiers(username=username, lid=lid, app=rqst.app)


async def retrieve_user(rqst, payload, *args, **kwargs):
    """/auth/me"""
    if payload:
        uid = payload.get('user_id')
        return await User(uid, rqst.app)


async def store_rtoken(user_id, refresh_token, *args, **kwargs):
    """/auth/refresh"""
    app.rtoken_cache[user_id] = refresh_token
    app.rtoken_cache[refresh_token] = user_id


async def retrieve_rtoken(user_id, *args, **kwargs):
    """/auth/refresh"""
    return app.rtoken_cache.get(user_id)


async def revoke_rtoken(user_id, *args, **kwargs):
    """/auth/logout"""
    try:
        app.rtoken_cache.pop(app.rtoken_cache.get(user_id))
        app.rtoken_cache.pop(user_id)
    except KeyError:
        pass


# Initialize with JSON Web Token (JWT) authentication for logins.
# First argument passed is the Sanic app object, and subsequent
# parameters are helper functions for authentication & security.
jwt.initialize(
  app,
  authenticate=authenticate,
  retrieve_user=retrieve_user,  # could probably be a lambda but meh
  store_refresh_token=store_rtoken,
  retrieve_refresh_token=retrieve_rtoken,
  revoke_refresh_token=revoke_rtoken
  )

# Config variables for JWT authentication. See sanic-jwt docs on GitLab
# for more info, or the README.md on my own fork bc I added some stuff
app.config.SANIC_JWT_REFRESH_TOKEN_ENABLED = True
app.config.SANIC_JWT_SECRET = os.environ['SANIC_JWT_SECRET']  # it's a secret to everybody!
app.config.SANIC_JWT_CLAIM_IAT = True  # perhaps for long sessions
# Store token in cookies instead of making the client webapp send them
# ...this may also open things up for XSRF. but I don't know enough about
# that to be sure regarding how to deal with or ameliorate it
app.config.SANIC_JWT_COOKIE_SET = True

# Get the filenames generated by Angular's AOT build
# (could probably slim this down, sort of just threw stuff together until it worked)
olddir = os.getcwd()
os.chdir('/home/hadi/booksy-db/dist')
# filenames = ('index.html', 'styles*.css', 'inline*.js', 'main*.js', 'polyfills*.js')
filenames = (
  'index.html',
  'inline*.js',
  'inline*.map',
  'main*.js',
  'main*.map',
  'polyfills*.js',
  'polyfills*.map',
  'styles*.js',
  'styles*.map',
  'scripts*.js',
  'scripts*.map',
  'vendor*.js',
  'vendor*.map'
  )
relative = [glob(i) for i in filenames]
os.chdir(olddir)
absolute = [glob(i) for i in map('/home/hadi/booksy-db/dist/'.__add__, filenames)]
for rel, absol in zip(relative, absolute):
    app.static(rel[0], absol[0])  # Route user requests to Angular's files


@app.listener('before_server_start')
async def set_up_dbs(app, loop):
    """
    Establishes a connection to the environment's Postgres and Redis DBs
    for use in (first) authenticating and (then) storing refresh tokens.
    """
    app.pg_pool = await asyncpg.create_pool(dsn=os.getenv('DATABASE_URL'), max_size=15, loop=loop)
    app.acquire = app.pg_pool.acquire
    # async with app.acquire() as conn:
    #     await setup.create_pg_tables(conn)
    
    app.session = aiohttp.ClientSession()
    app.sem = asyncio.Semaphore(4, loop=loop)  # limit concurrency of aiohttp requests to Google Books
    
    app.ppe = ProcessPoolExecutor(4)
    app.aexec = loop.run_in_executor    # ensure the aiolocks' being set up
    
    [i.do_imports() for i in [Location, Role, MediaType, MediaItem, User]]
    if os.getenv('REDIS_URL') is None:  # can't do nothin bout this
        app.config.SANIC_JWT_REFRESH_TOKEN_ENABLED = True  # bc using dict on this dev server
    else:
        app.rd_pool = await aioredis.create_pool(
          os.getenv('REDIS_URL'),
          minsize=5,
          maxsize=15,
          loop=loop
          )


@app.listener('after_server_start')
async def xxxxx(app, loop):
    # print(await (await Location(1, app)).add_members_from_csv(open('/home/hadi/Downloads/test.csv'), 2))
    pass


@app.listener('before_server_stop')
async def close_dbs(app, loop):
    """
    Gracefully close all acquired connections before closing.
    """
    await app.pg_pool.close()
    await app.session.close()
    print('Shutting down.')


@app.middleware('request')
async def force_angular(rqst):
    if not any(i in rqst.url for i in rqst.app.safe_segments):
        try:
            url = rqst.url[3+rqst.url.find('://'):]
            return sanic.response.redirect('/index.html/?redirect=' + urllib.parse.quote(url.split('/', 1)[1]))
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
    return sanic.response.html(f'''
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
    <h3>Your password:</h3>
    <input type="password" id="adminpw" name="adminpw" placeholder="Admin account password"/>
    <input type="password" id="aconf" placeholder="Confirm admin password">
    <br/>
    <input type="password" id="checkoutpw" name="checkoutpw" placeholder="Self-checkout account password"/>
    <input type="password" id="cconf" placeholder="Confirm checkout account password">
    <br/>
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
}}
</script></html>''', status=200)


@app.post('/register')
@deco.rqst_get('token', 'adminpw', 'checkoutpw', form=True)
async def register_location(rqst, token, adminpw, checkoutpw):
    """Have to *unpack because rqst.form returns one-item lists"""
    locname, lid, chk_usr, admin_usr = await Location.instate(rqst, *token, *adminpw, *checkoutpw)
    return sanic.response.html('''
    <html><head></head><body>
    <p style="font-family:monospace;font-size:20px"><strong>'''
    + '''
    ╔════════════════════════════════════════════════════════╗<br/>
    ║ PLEASE SCREENSHOT OR OTHERWISE SAVE THIS PAGE!!!       ║<br/>
    ║                                                        ║<br/>
    ║ The information given here, particularly the location  ║<br/>
    ║ ID and self-checkout account's username, are vital to  ║<br/>
    ║ logging in — but they will not be displayed anywhere   ║<br/>
    ║ else.                                                  ║<br/>
    ╚════════════════════════════════════════════════════════╝
    </strong></p>
    '''.replace(' ', '&nbsp;')  # otherwise the third line gets screwed with
    + f'''
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
    ''', status=200)


# more than 1 worker and you get too many DB connections :((
app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=True, access_log=True, workers=1)
