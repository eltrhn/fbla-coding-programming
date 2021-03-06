"""/api/member"""
import sanic
from sanic_jwt import decorators as jwtdec

from . import uid_get, rqst_get
from . import User

mbr = sanic.Blueprint('member_api', url_prefix='/member')


@mbr.post('/self')
@rqst_get('user', 'fullname', 'newpass', 'curpass')
@jwtdec.protected()
async def edit_self(rqst, user, *, fullname, newpass, curpass):
    """Full name; new password; current password to verify"""
    if not await user.verify_pw(curpass):
        return sanic.exceptions.abort(403, "Incorrectly-entered password. Please try again.")
    await user.edit_self(name=fullname, pw=newpass)
    return sanic.response.raw(b'', status=204)


@mbr.get('/notifications')
@rqst_get('location', 'username')
@jwtdec.protected()
async def get_notifs(rqst, location, *, username):
    """
    Serves user's notifications -- overdue items, readied holds, etc.
    """
    user = await User.from_identifiers(username, location, app=rqst.app)
    return sanic.response.json(await user.notifs(), status=200)


@mbr.get('/suggest')
@uid_get('location', 'recent')
@jwtdec.protected()
async def get_recent(rqst, location, *, recent):
    """
    Serves what's shown in the 'based on your most-recent checkout'
    section of the 'Find Media' page, given a genre to match for.
    """
    return sanic.response.json({'items': await location.search(genre=recent, max_results=2)}, status=200)


@mbr.get('/checked-out')
@rqst_get('user', 'member')
@jwtdec.protected()
async def get_user_items(rqst, user, *, member):
    """
    Serves user's currently-checked-out items.
    """
    member = await User(member, rqst.app)
    if not user.beats(member, and_has='manage_accounts'):
        sanic.exceptions.abort(403, "You aren't allowed to view this member's items.")
    return sanic.response.json(await member.items(), status=200)


@mbr.get('/held')
@rqst_get('user', 'member')
@jwtdec.protected()
async def get_user_holds(rqst, user, *, member):
    """
    Serves user's currently-active holds.
    """
    member = await User(member, rqst.app)
    if not user.beats(member, and_has='manage_accounts'):
        sanic.exceptions.abort(403, "You aren't allowed to view this member's holds.")
    return sanic.response.json(await member.held(), status=200)


@mbr.post('/clear-hold')
@rqst_get('user', 'item')
@jwtdec.protected()
async def clear_hold(rqst, user, *, item):
    """
    Clears a hold the user has on an item.
    """
    await user.clear_hold(item)
    return sanic.response.raw(b'', status=204)


@mbr.post('/edit')
@rqst_get('user', 'member')  # ('requester', 'user to edit')
@jwtdec.protected()
async def edit_member(rqst, user, *, member):
    """
    Edits user's information.
    """
    changing = await User(member['user_id'], rqst.app)
    if not user.beats(changing, and_has='manage_accounts'):
        sanic.exceptions.abort(403, "You aren't allowed to modify this member's info.")
    await changing.edit(username=member['username'], rid=member['rid'], fullname=member['name'])
    return sanic.response.raw(b'', status=204)


@mbr.get('/check-perms')
@uid_get('perms')
@jwtdec.protected()
async def check_perms(rqst, *, perms):
    def toCamelCase(inp):
        """
        Converts so I can access perms idiomatically in TypeScript,
        using TS-conventional camelCase instead of Python snake_case.
        e.g. perms.can_check_out in Python, but perms.canCheckOut in TS
        """
        return 'can' + ''.join(map(str.capitalize, inp.split('_')))
    perms.namemap = {toCamelCase(k): v for k, v in perms.namemap.items()}
    return sanic.response.json({'perms': perms.props, 'raw': perms.raw}, status=200)
